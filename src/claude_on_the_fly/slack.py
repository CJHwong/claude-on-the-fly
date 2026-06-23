"""Slack frontend. Replies as you via user token + Socket Mode."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import deque
from pathlib import Path

import aiohttp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from typing import Awaitable, Callable

from claude_on_the_fly.agent import Response, footer_parts
from claude_on_the_fly.protocol import Frontend
from claude_on_the_fly.slack_mrkdwn import to_mrkdwn

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
SLACK_BLOCK_LIMIT = 3000
_ALLOWED_SUBTYPES = {"file_share"}
_FALLBACK_ERRORS = frozenset({"not_in_channel", "is_archived", "channel_not_found"})


def _split_blocks(text: str) -> list[str]:
    """Split text into chunks that fit Slack's block text limit, on line boundaries."""
    chunks: list[str] = []
    chunk = ""
    for line in text.split("\n"):
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > SLACK_BLOCK_LIMIT:
            if chunk:
                chunks.append(chunk)
            chunk = line[:SLACK_BLOCK_LIMIT]
        else:
            chunk = candidate
    if chunk:
        chunks.append(chunk)
    return chunks or [""]


def _build_response_blocks(body: str, response: Response) -> list[dict]:
    """Render a Response as Slack block-kit: section chunks + stats/tools context."""
    blocks: list[dict] = []
    for chunk in _split_blocks(to_mrkdwn(body)):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
    stats, tools = footer_parts(response, "slack")
    if stats:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": stats}]}
        )
    if tools:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": tools}]}
        )
    return blocks


def _session_key(channel: str, thread_ts: str | None) -> int:
    raw = f"{channel}:{thread_ts or 'root'}"
    return int(hashlib.sha256(raw.encode()).hexdigest()[:16], 16)


def _flatten_rich_elements(elements: list[dict]) -> str:
    """Flatten rich_text sub-elements into plain text."""
    parts: list[str] = []
    for el in elements or []:
        t = el.get("type")
        if t == "text":
            parts.append(el.get("text", ""))
        elif t == "user":
            parts.append(f"<@{el.get('user_id', '')}>")
        elif t == "link":
            parts.append(el.get("url", ""))
        elif t == "channel":
            parts.append(f"<#{el.get('channel_id', '')}>")
        elif "elements" in el:
            parts.append(_flatten_rich_elements(el.get("elements") or []))
    return "".join(parts)


def _text_from_blocks(blocks: list[dict]) -> str:
    """Extract plain text from block-kit blocks (sections, rich_text)."""
    parts: list[str] = []
    for block in blocks or []:
        btype = block.get("type")
        if btype == "rich_text":
            for element in block.get("elements") or []:
                if "elements" in element:
                    parts.append(_flatten_rich_elements(element.get("elements") or []))
        elif btype == "section":
            txt = block.get("text") or {}
            if txt.get("text"):
                parts.append(txt["text"])
    return "\n".join(p for p in parts if p)


def _text_from_context_block(block: dict) -> str:
    """Extract text from a context block's elements (plain_text/mrkdwn)."""
    parts: list[str] = []
    for el in block.get("elements") or []:
        if el.get("type") in ("plain_text", "mrkdwn"):
            txt = el.get("text")
            if txt:
                parts.append(txt)
    return " ".join(parts)


def _text_from_primary_blocks(blocks: list[dict]) -> str:
    """Extract text from non-rich_text blocks (section, header, context).

    rich_text blocks are skipped because they typically duplicate event.text
    in regular user messages. App posts and rich-block messages use the
    other block types, which is what we want to surface.
    """
    parts: list[str] = []
    for block in blocks or []:
        btype = block.get("type")
        if btype in ("section", "header"):
            txt = (block.get("text") or {}).get("text") or ""
            if txt:
                parts.append(txt)
        elif btype == "context":
            txt = _text_from_context_block(block)
            if txt:
                parts.append(txt)
    return "\n".join(parts)


def _is_forward_attachment(att: dict) -> bool:
    return bool(att.get("is_msg_unfurl")) or bool(
        att.get("channel_id") and att.get("ts")
    )


def _render_attachment(att: dict) -> str:
    """Render a non-forward attachment (app post, link preview) as plain text."""
    lines: list[str] = []
    if att.get("pretext"):
        lines.append(att["pretext"])
    if att.get("title"):
        lines.append(att["title"])
    if att.get("text"):
        lines.append(att["text"])
    if att.get("blocks") and not att.get("text"):
        block_text = _text_from_primary_blocks(att["blocks"])
        if block_text:
            lines.append(block_text)
    for field in att.get("fields") or []:
        title = field.get("title") or ""
        value = field.get("value") or ""
        if title and value:
            lines.append(f"{title}: {value}")
        elif value:
            lines.append(value)
    return "\n".join(lines)


def _flatten_primary_content(event: dict) -> str:
    """Capture block-kit / attachment content from the primary message.

    Surfaces app-bot posts, link unfurls, and rich-block messages that would
    otherwise be lost because event.text is empty or a degraded fallback.
    Skips attachments handled by _extract_forwards to avoid double-rendering.
    """
    parts: list[str] = []
    blocks_text = _text_from_primary_blocks(event.get("blocks") or [])
    if blocks_text:
        parts.append(blocks_text)
    for att in event.get("attachments") or []:
        if _is_forward_attachment(att):
            continue
        rendered = _render_attachment(att)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def _extract_forwards(event: dict) -> list[dict]:
    """Collect forwarded/quoted messages from event attachments and blocks.

    Returns a list of dicts with keys: channel_id, channel_name, ts,
    author_name, author_id, text. Missing fields default to empty strings.
    """
    forwards: list[dict] = []

    # Shape A: legacy attachments[] from "Share message to..." and permalink unfurls.
    for att in event.get("attachments") or []:
        is_unfurl = bool(att.get("is_msg_unfurl"))
        has_ref = bool(att.get("channel_id") and att.get("ts"))
        if not (is_unfurl or has_ref):
            continue
        body = att.get("text") or ""
        if not body and att.get("blocks"):
            body = _text_from_blocks(att["blocks"])
        if not body:
            continue
        forwards.append(
            {
                "channel_id": att.get("channel_id", ""),
                "channel_name": att.get("channel_name", ""),
                "ts": att.get("ts", ""),
                "author_name": att.get("author_name", ""),
                "author_id": att.get("author_id", ""),
                "text": body,
            }
        )

    # Shape B: top-level blocks[] with rich_text_quote elements.
    for block in event.get("blocks") or []:
        if block.get("type") != "rich_text":
            continue
        for element in block.get("elements") or []:
            if element.get("type") != "rich_text_quote":
                continue
            body = _flatten_rich_elements(element.get("elements") or [])
            if not body:
                continue
            forwards.append(
                {
                    "channel_id": "",
                    "channel_name": "",
                    "ts": "",
                    "author_name": "",
                    "author_id": "",
                    "text": body,
                }
            )

    return forwards


def _render_forward(fwd: dict) -> str:
    """Render a forwarded-message dict as a labeled XML block for the prompt."""
    lines: list[str] = ["<forwarded_message>"]
    src_bits: list[str] = []
    if fwd.get("channel_name"):
        src_bits.append(f"#{fwd['channel_name']}")
    if fwd.get("author_name"):
        src_bits.append(f"@{fwd['author_name']}")
    if fwd.get("ts"):
        src_bits.append(fwd["ts"])
    if src_bits:
        lines.append(f"  <source>{' · '.join(src_bits)}</source>")
    if fwd.get("channel_id"):
        lines.append(f"  <channel_id>{fwd['channel_id']}</channel_id>")
    if fwd.get("ts"):
        lines.append(f"  <thread_ts>{fwd['ts']}</thread_ts>")
    lines.append("  <body>")
    lines.append(fwd.get("text", ""))
    lines.append("  </body>")
    lines.append("</forwarded_message>")
    return "\n".join(lines)


class SlackFrontend(Frontend):
    def __init__(
        self,
        app_token: str,
        user_token: str,
        user_id: str,
        allowed_user_ids: set[str] | None = None,
        blocked_user_ids: set[str] | None = None,
        allowed_bot_ids: set[str] | None = None,
        suppress_bot_replies: bool = True,
    ) -> None:
        self._app_token = app_token
        self._user_id = user_id
        self._allowed_user_ids = allowed_user_ids or set()
        self._allowed_user_ids.add(user_id)
        self._allow_all_senders = "*" in self._allowed_user_ids
        self._blocked_user_ids = blocked_user_ids or set()
        self._allowed_bot_ids = allowed_bot_ids or set()
        self._suppress_bot_replies = suppress_bot_replies
        logger.debug(
            "init: user_id=%s, allowed_user_ids=%s, allow_all=%s, blocked_user_ids=%s, allowed_bot_ids=%s, suppress_bot_replies=%s",
            user_id,
            self._allowed_user_ids,
            self._allow_all_senders,
            self._blocked_user_ids,
            self._allowed_bot_ids,
            self._suppress_bot_replies,
        )
        self._app = AsyncApp(token=user_token, ignoring_self_events_enabled=False)
        self._handler: AsyncSocketModeHandler | None = None
        self._on_message: Callable[[int, str], Awaitable[None]] | None = None
        self._orchestrator: object | None = None
        self._sessions: dict[int, tuple[str, str | None]] = {}
        self._our_sent_timestamps: deque[str] = deque(maxlen=500)
        self._processed_ts: deque[str] = deque(maxlen=1000)
        self._active_channels: dict[str, str] = {}  # channel_id -> last event_ts
        self._channel_types: dict[str, str] = {}  # channel_id -> channel_type
        self._connected_once = False
        self._workspace_names: dict[int, str] = {}
        self._sender_names: dict[int, str] = {}
        self._channel_contexts: dict[int, str] = {}
        self._user_name_cache: dict[str, str] = {}  # slack user_id -> display name
        self._session_sender_ids: dict[int, str] = {}  # session -> slack user_id
        self._dm_channels: dict[str, str] = {}  # slack user_id -> im channel id
        self._pending_msg: dict[
            int, deque[tuple[str, str]]
        ] = {}  # session -> FIFO of (channel, ts)
        self._pending_reply_suppressed: dict[int, deque[bool]] = {}
        self._in_flight: dict[int, tuple[str, str]] = {}
        self._in_flight_reply_suppressed: dict[int, bool] = {}

    def set_orchestrator(self, orchestrator: object) -> None:
        self._orchestrator = orchestrator

    def workspace_name(self, chat_id: int) -> str:
        return f"slack/{self._workspace_names.get(chat_id, str(chat_id))}"

    def sender_name(self, chat_id: int) -> str:
        return self._sender_names.get(chat_id, "unknown")

    def channel_context(self, chat_id: int) -> str:
        return self._channel_contexts.get(chat_id, "dm")

    def describe(self) -> dict[str, str]:
        from claude_on_the_fly.orchestrator import _redact_token

        allowed = (
            "*" if self._allow_all_senders else ",".join(sorted(self._allowed_user_ids))
        )
        return {
            "app_token": _redact_token(self._app_token),
            "user_id": self._user_id,
            "allowed_users": allowed or "<none>",
            "blocked_users": ",".join(sorted(self._blocked_user_ids)) or "<none>",
            "allowed_bots": ",".join(sorted(self._allowed_bot_ids)) or "<none>",
            "suppress_bot_replies": str(self._suppress_bot_replies).lower(),
        }

    # --- Lifecycle ---

    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        self._on_message = on_message

        @self._app.event({"type": "message"})
        async def handle_message(event, say):
            logger.debug("raw slack event: %s", event)
            await self._ingest_event(event)

        self._app.event("hello")(self._on_hello)

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._handler.start_async()
        logger.info("Slack connected via Socket Mode (user_id=%s)", self._user_id)

    async def _on_hello(self, event, say):
        if not self._connected_once:
            self._connected_once = True
            logger.info("Socket Mode: initial connection")
            return
        logger.info("Socket Mode: reconnected, running catch-up")
        await self._catchup()

    async def _ingest_event(self, event: dict) -> None:
        subtype = event.get("subtype")
        bot_id = event.get("bot_id", "")
        # App/bot posts (HubSpot, Jira, etc.) arrive as subtype=bot_message with
        # no user field. Only let through bots explicitly trusted by bot_id.
        is_trusted_bot = subtype == "bot_message" and bot_id in self._allowed_bot_ids
        if subtype == "bot_message":
            if not is_trusted_bot:
                logger.debug("skipped: untrusted bot_message bot_id=%s", bot_id)
                return
            logger.info("trusted bot_message accepted: bot_id=%s", bot_id)
        elif subtype and subtype not in _ALLOWED_SUBTYPES:
            logger.debug("skipped: subtype=%s", subtype)
            return
        ts = event.get("ts", "")
        sender_id = event.get("user", "")
        if ts in self._our_sent_timestamps:
            logger.debug("skipped: our own message ts=%s", ts)
            return
        if ts in self._processed_ts:
            logger.debug("skipped: already processed ts=%s", ts)
            return
        text = event.get("text", "")
        channel: str = event.get("channel", "")
        if not channel:
            logger.debug("skipped: no channel in event")
            return
        thread_ts: str = event.get("thread_ts") or ts
        channel_type: str = event.get("channel_type") or self._channel_types.get(
            channel, ""
        )
        forwards = _extract_forwards(event)
        extra_content = _flatten_primary_content(event)
        fwd_refs = ",".join(
            f"{f.get('channel_id') or '?'}/{f.get('ts') or '?'}" for f in forwards
        )
        logger.debug(
            "parsed: sender=%s channel=%s channel_type=%s thread_ts=%s text=%s forwards=%d forward_refs=%s",
            sender_id,
            channel,
            channel_type,
            thread_ts,
            text[:80],
            len(forwards),
            fwd_refs,
        )

        # Trusted bots are already authorized by bot_id and carry no user field,
        # so the human allow/block and @mention gates don't apply to them.
        if not is_trusted_bot:
            # Blocklist wins over the allowlist, so "*" can allow all but deny a few.
            if sender_id in self._blocked_user_ids:
                logger.debug("skipped: sender %s in blocked_user_ids", sender_id)
                return

            # Allowlist applies to all channel types, including DMs and group DMs.
            if not self._allow_all_senders and sender_id not in self._allowed_user_ids:
                logger.debug("skipped: sender %s not in allowed_user_ids", sender_id)
                return

            # Channels and groups additionally require an @mention.
            if channel_type in ("channel", "group"):
                mention = f"<@{self._user_id}>"
                if mention not in text:
                    logger.debug("skipped: no mention of %s in text", self._user_id)
                    return
                text = re.sub(f"<@{self._user_id}>\\s*", "", text).strip()

        session_id = _session_key(channel, thread_ts)
        is_new_session = session_id not in self._sessions
        is_mid_thread = bool(event.get("thread_ts")) and event["thread_ts"] != ts
        self._sessions[session_id] = (channel, thread_ts)
        logger.debug(
            "session: id=%s channel=%s thread_ts=%s", session_id, channel, thread_ts
        )

        thread_context = ""
        if is_new_session and is_mid_thread:
            thread_context = await self._fetch_thread_context(channel, thread_ts, ts)

        if is_trusted_bot:
            sender = event.get("username") or bot_id or "bot"
        else:
            sender = await self._resolve_sender(event.get("user", "unknown"))
        self._sender_names[session_id] = sender
        await self._resolve_session_metadata(
            session_id, sender, channel, channel_type, thread_ts
        )

        files = event.get("files") or []
        file_lines: list[str] = []
        if files:
            file_lines = await self._save_files(session_id, files)

        if not text and not forwards and not file_lines and not extra_content:
            logger.debug("skipped: empty text after processing")
            return

        self._session_sender_ids[session_id] = sender_id
        self._processed_ts.append(ts)
        self._active_channels[channel] = ts
        if channel_type:
            self._channel_types[channel] = channel_type

        cover_parts: list[str] = []
        if file_lines:
            cover_parts.extend(file_lines)
        if text:
            cover_parts.append(text)
        if extra_content:
            cover_parts.append(extra_content)
        cover = "\n".join(cover_parts)

        segments: list[str] = []
        if thread_context:
            segments.append(thread_context)
        segments.extend(_render_forward(f) for f in forwards)
        if cover:
            segments.append(f"[from: {sender}] {cover}")
        elif forwards:
            segments.append(f"[from: {sender}]")
        final_text = "\n\n".join(segments)

        self._pending_msg.setdefault(session_id, deque()).append((channel, ts))
        self._pending_reply_suppressed.setdefault(session_id, deque()).append(
            is_trusted_bot and self._suppress_bot_replies
        )
        preview = text[:80] if text else "(forward only)"
        fwd_marker = f" (+{len(forwards)} fwd)" if forwards else ""
        logger.info(
            "slack %s/%s: [from: %s] %s%s",
            channel,
            thread_ts,
            sender,
            preview,
            fwd_marker,
        )
        if self._on_message:
            await self._on_message(session_id, final_text)

    async def _catchup(self) -> None:
        """Fetch recent messages from active channels to recover missed events."""
        if not self._active_channels:
            return
        for channel, last_ts in list(self._active_channels.items()):
            try:
                resp = await self._app.client.conversations_history(
                    channel=channel, oldest=last_ts, inclusive=False, limit=20
                )
            except Exception as exc:
                logger.warning(
                    "catch-up: failed to fetch history for %s: %s", channel, exc
                )
                continue
            messages = resp.get("messages", [])
            if not messages:
                continue
            logger.info("catch-up: %d new messages in %s", len(messages), channel)
            cached_type = self._channel_types.get(channel, "")
            for msg in sorted(messages, key=lambda m: m.get("ts", "")):
                if "channel" not in msg:
                    msg["channel"] = channel
                if "channel_type" not in msg:
                    msg["channel_type"] = cached_type
                await self._ingest_event(msg)

    async def stop(self) -> None:
        if self._handler:
            await self._handler.close_async()

    # --- Sending ---

    async def send(self, chat_id: int, response: Response) -> list[Path] | None:
        route = self._sessions.get(chat_id)
        if not route:
            logger.error("No channel found for session %s", chat_id)
            return []
        channel, thread_ts = route
        if self._in_flight_reply_suppressed.get(chat_id, False):
            logger.info(
                "slack %s/%s => reply omitted for trusted bot message",
                channel,
                thread_ts,
            )
            return response.attachments
        logger.info("slack %s/%s => %s", channel, thread_ts, response.body[:80])

        blocks = _build_response_blocks(response.body, response)

        try:
            resp = await self._app.client.chat_postMessage(
                channel=channel,
                text=response.body,
                blocks=blocks,
                thread_ts=thread_ts,
            )
        except SlackApiError as exc:
            error = exc.response.get("error", "unknown_error")
            logger.error("send: slack api error %s: %s", error, exc)
            if error in _FALLBACK_ERRORS:
                return await self._fallback_dm(chat_id, response, channel, error)
            return []
        except Exception as exc:
            logger.error("send: failed to post message: %s", exc)
            return []

        if resp.get("ok"):
            self._our_sent_timestamps.append(resp["ts"])
            logger.debug("send: ok ts=%s", resp["ts"])
            await self._upload_attachments(channel, thread_ts, response.attachments)
            return response.attachments
        error = resp.get("error", "unknown_error")
        logger.warning("send: slack responded not ok: %s", resp)
        if error in _FALLBACK_ERRORS:
            return await self._fallback_dm(chat_id, response, channel, error)
        return []

    async def _upload_attachments(
        self, channel: str, thread_ts: str | None, attachments: list[Path]
    ) -> None:
        """Upload outbox files into the same thread. On a per-file failure, log
        and continue so one bad file doesn't drop the rest, then post one
        in-thread heads-up so the user isn't left guessing. A missing files:write
        scope surfaces here as `missing_scope` and is shown to the user."""
        failures: list[str] = []
        for path in attachments:
            try:
                data = await asyncio.to_thread(path.read_bytes)
                resp = await self._app.client.files_upload_v2(
                    channel=channel,
                    thread_ts=thread_ts,
                    file=data,
                    filename=path.name,
                )
            except SlackApiError as exc:
                code = exc.response.get("error", "unknown_error")
                logger.error("upload: failed to send %s: %s", path.name, code)
                failures.append(f"{path.name} (`{code}`)")
                continue
            except Exception as exc:
                logger.error("upload: failed to send %s: %s", path.name, exc)
                failures.append(path.name)
                continue
            self._record_upload_ts(resp)
            logger.info("uploaded %s to %s", path.name, channel)
        if failures:
            await self._notify_upload_failure(channel, thread_ts, failures)

    async def _notify_upload_failure(
        self, channel: str, thread_ts: str | None, failures: list[str]
    ) -> None:
        """Tell the user in-thread which files couldn't be attached and why,
        since they can't see the daemon log."""
        note = "_(couldn't attach " + ", ".join(failures) + ")_"
        try:
            resp = await self._app.client.chat_postMessage(
                channel=channel, text=note, thread_ts=thread_ts
            )
        except Exception as exc:
            logger.error("upload: failed to post failure notice: %s", exc)
            return
        if resp.get("ok"):
            self._our_sent_timestamps.append(resp["ts"])

    def _record_upload_ts(self, resp: AsyncSlackResponse) -> None:
        """Record the ts of file-share messages we just posted so our own upload
        isn't re-ingested as an inbound message (we reply via the user token)."""
        for f in resp.get("files") or []:
            for visibility in (f.get("shares") or {}).values():
                for share_list in visibility.values():
                    for share in share_list:
                        ts = share.get("ts")
                        if ts:
                            self._our_sent_timestamps.append(ts)

    async def send_typing(self, chat_id: int) -> None:
        pass

    async def notify_queued(self, chat_id: int, position: int) -> None:
        """React with hourglass on the most recently ingested message."""
        pending = self._pending_msg.get(chat_id)
        if not pending:
            logger.debug("notify_queued: no pending msg for chat_id=%s", chat_id)
            return
        channel, ts = pending[-1]
        await self._react(channel, ts, "hourglass_flowing_sand")

    async def notify_start(self, chat_id: int) -> None:
        """Transition the oldest pending message from hourglass to eyes."""
        pending = self._pending_msg.get(chat_id)
        if not pending:
            logger.debug("notify_start: no pending msg for chat_id=%s", chat_id)
            return
        channel, ts = pending.popleft()
        suppressed_queue = self._pending_reply_suppressed.get(chat_id)
        suppress_reply = suppressed_queue.popleft() if suppressed_queue else False
        if suppressed_queue is not None and not suppressed_queue:
            self._pending_reply_suppressed.pop(chat_id, None)
        await self._unreact(channel, ts, "hourglass_flowing_sand")
        await self._react(channel, ts, "eyes")
        self._in_flight[chat_id] = (channel, ts)
        self._in_flight_reply_suppressed[chat_id] = suppress_reply

    async def notify_complete(self, chat_id: int) -> None:
        """Remove :eyes: from the in-flight message."""
        in_flight = self._in_flight.pop(chat_id, None)
        self._in_flight_reply_suppressed.pop(chat_id, None)
        if not in_flight:
            logger.debug("notify_complete: no in-flight msg for chat_id=%s", chat_id)
            return
        channel, ts = in_flight
        await self._unreact(channel, ts, "eyes")

    # --- Helpers ---

    async def _react(self, channel: str, timestamp: str, emoji: str) -> None:
        try:
            await self._app.client.reactions_add(
                channel=channel, timestamp=timestamp, name=emoji
            )
        except Exception as exc:
            logger.warning("react: failed to add :%s: to %s: %s", emoji, timestamp, exc)

    async def _unreact(self, channel: str, timestamp: str, emoji: str) -> None:
        """Remove a reaction. Silently ignores 'no_reaction' (it wasn't there)."""
        try:
            await self._app.client.reactions_remove(
                channel=channel, timestamp=timestamp, name=emoji
            )
        except Exception as exc:
            if "no_reaction" not in str(exc):
                logger.warning(
                    "unreact: failed to remove :%s: from %s: %s", emoji, timestamp, exc
                )

    async def _open_dm_channel(self, user_id: str) -> str | None:
        """Resolve a user_id to their IM channel id. Cached after first call."""
        cached = self._dm_channels.get(user_id)
        if cached:
            return cached
        try:
            dm = await self._app.client.conversations_open(users=user_id)
        except Exception as exc:
            logger.error("open_dm: cannot open DM with %s: %s", user_id, exc)
            return None
        channel_id = dm["channel"]["id"]
        self._dm_channels[user_id] = channel_id
        return channel_id

    async def _fallback_dm(
        self, chat_id: int, response: Response, channel: str, error: str
    ) -> list[Path]:
        """Deliver a response via DM when the original channel post fails.
        Returns the attachments actually handed off (empty if the DM failed)."""
        sender_id = self._session_sender_ids.get(chat_id)
        if not sender_id:
            logger.warning(
                "fallback_dm: no sender_id for session %s, response lost", chat_id
            )
            return []
        dm_channel = await self._open_dm_channel(sender_id)
        if not dm_channel:
            return []

        prefix = (
            f"_(I couldn't post my reply in <#{channel}>: `{error}`. "
            f"Here it is via DM instead.)_\n\n"
        )
        body = prefix + response.body
        blocks = _build_response_blocks(body, response)
        try:
            resp = await self._app.client.chat_postMessage(
                channel=dm_channel,
                text=body,
                blocks=blocks,
            )
        except Exception as exc:
            logger.error("fallback_dm: DM to %s failed: %s", sender_id, exc)
            return []
        if resp.get("ok"):
            self._our_sent_timestamps.append(resp["ts"])
            await self._upload_attachments(dm_channel, None, response.attachments)
            logger.info(
                "fallback_dm: delivered response to %s for session %s",
                sender_id,
                chat_id,
            )
            return response.attachments
        logger.error("fallback_dm: DM post failed for %s: %s", sender_id, resp)
        return []

    def _workspace_path(self, session_id: int) -> Path:
        return DATA_DIR / "workspaces" / self.workspace_name(session_id)

    async def _save_files(self, session_id: int, files: list[dict]) -> list[str]:
        """Download Slack files to workspace. Returns '[File saved: name]' lines."""
        workspace = self._workspace_path(session_id)
        workspace.mkdir(parents=True, exist_ok=True)
        token: str = self._app.client.token or ""
        lines: list[str] = []
        for f in files:
            url = f.get("url_private_download")
            name = f.get("name") or f"file_{f.get('id', 'unknown')}"
            if not url:
                logger.warning("file %s has no url_private_download, skipping", name)
                continue
            dest = workspace / Path(name).name
            try:
                await self._download_file(url, dest, token)
                lines.append(f"[File saved: {dest.name}]")
                logger.info("saved file %s for session %s", dest.name, session_id)
            except Exception as exc:
                logger.warning("failed to download file %s: %s", name, exc)
        return lines

    @staticmethod
    async def _download_file(url: str, dest: Path, token: str) -> None:
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                resp.raise_for_status()
                content_type = resp.content_type or ""
                if content_type.startswith("text/html"):
                    raise RuntimeError(
                        f"got HTML instead of file data (likely auth issue): {url}"
                    )
                data = await resp.read()
                if not data:
                    raise RuntimeError(f"empty response body: {url}")
                dest.write_bytes(data)
                logger.debug(
                    "downloaded %s: %d bytes, content-type=%s",
                    dest.name,
                    len(data),
                    content_type,
                )

    async def _resolve_message_author(self, msg: dict) -> str:
        """Best-effort author display name for a thread-replay message."""
        user_id = msg.get("user")
        if user_id:
            return await self._resolve_sender(user_id)
        return msg.get("username") or msg.get("bot_id") or "unknown"

    async def _fetch_thread_context(
        self, channel: str, thread_ts: str, current_ts: str
    ) -> str:
        """Fetch prior messages in this thread and render them as a context block.

        Called only on the first time the bot sees this session_id when the
        triggering message is a reply in an existing thread. Skips the current
        message itself and degrades to an empty string on API failure.
        """
        try:
            resp = await self._app.client.conversations_replies(
                channel=channel, ts=thread_ts, limit=50
            )
        except Exception as exc:
            logger.warning(
                "thread-context: conversations.replies failed for %s/%s: %s",
                channel,
                thread_ts,
                exc,
            )
            return ""

        messages = resp.get("messages") or []
        if not messages:
            return ""

        lines: list[str] = ["<thread_context>"]
        rendered = 0
        for msg in messages:
            msg_ts = msg.get("ts", "")
            if msg_ts == current_ts:
                continue
            author = await self._resolve_message_author(msg)
            body_parts: list[str] = []
            body = msg.get("text") or ""
            if body:
                body_parts.append(body)
            extra = _flatten_primary_content(msg)
            if extra:
                body_parts.append(extra)
            body_text = "\n".join(body_parts).strip()
            if not body_text:
                continue
            lines.append(
                f'  <message author="{author}" ts="{msg_ts}">{body_text}</message>'
            )
            rendered += 1
        if rendered == 0:
            return ""
        lines.append("</thread_context>")
        logger.info(
            "thread-context: included %d prior messages for %s/%s",
            rendered,
            channel,
            thread_ts,
        )
        return "\n".join(lines)

    async def _resolve_sender(self, user_id: str) -> str:
        """Look up Slack user ID to display name. Cached on success only."""
        if user_id not in self._user_name_cache:
            try:
                info = await self._app.client.users_info(user=user_id)
                self._user_name_cache[user_id] = info["user"]["name"]
            except Exception as exc:
                logger.warning("Failed to resolve Slack user %s: %s", user_id, exc)
                return user_id
        return self._user_name_cache[user_id]

    async def _resolve_session_metadata(
        self,
        session_id: int,
        sender: str,
        channel: str,
        channel_type: str,
        thread_ts: str,
    ) -> None:
        if session_id in self._workspace_names:
            return

        short_ts = thread_ts.split(".")[0] if thread_ts else "root"

        if channel_type == "im":
            self._workspace_names[session_id] = f"dm-{sender}-{short_ts}"
            self._channel_contexts[session_id] = "dm (private)"
            return

        try:
            info = await self._app.client.conversations_info(channel=channel)
            ch = info["channel"]
            name = ch["name"]
        except Exception as exc:
            logger.warning("Failed to resolve channel %s: %s", channel, exc)
            self._workspace_names[session_id] = f"{channel}-{short_ts}"
            self._channel_contexts[session_id] = f"channel:{channel}"
            return

        if ch.get("is_mpim"):
            members = await self._resolve_mpim_members(channel)
            self._workspace_names[session_id] = f"{name}-{short_ts}"
            context = f"group-dm (private)\nParticipants: {', '.join(members)}"
            self._channel_contexts[session_id] = context
        else:
            visibility = "private" if ch.get("is_private") else "public"
            self._workspace_names[session_id] = f"{name}-{short_ts}"
            self._channel_contexts[session_id] = (
                f"channel:#{name} ({visibility}) id:{channel}"
            )

    async def _resolve_mpim_members(self, channel: str) -> list[str]:
        """Resolve display names of all members in a group DM."""
        try:
            resp = await self._app.client.conversations_members(channel=channel)
            member_ids = resp.get("members", [])
        except Exception as exc:
            logger.warning("Failed to list mpim members for %s: %s", channel, exc)
            return ["unknown"]
        names = []
        for uid in member_ids:
            if uid == self._user_id:
                continue
            names.append(await self._resolve_sender(uid))
        return names or ["unknown"]


def main() -> None:  # pragma: no cover
    from dotenv import load_dotenv

    from claude_on_the_fly.orchestrator import run
    from claude_on_the_fly.preflight import run_slack

    load_dotenv()
    (
        app_token,
        user_token,
        user_id,
        allowed_user_ids,
        blocked_user_ids,
        allowed_bot_ids,
        suppress_bot_replies,
    ) = run_slack()
    frontend = SlackFrontend(
        app_token=app_token,
        user_token=user_token,
        user_id=user_id,
        allowed_user_ids=allowed_user_ids,
        blocked_user_ids=blocked_user_ids,
        allowed_bot_ids=allowed_bot_ids,
        suppress_bot_replies=suppress_bot_replies,
    )
    asyncio.run(run(frontend, platform="slack"))


if __name__ == "__main__":  # pragma: no cover
    main()
