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

from typing import Awaitable, Callable

from claude_on_the_fly.agent import Response, footer_parts
from claude_on_the_fly.protocol import Frontend

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
SLACK_BLOCK_LIMIT = 3000
_ALLOWED_SUBTYPES = {"file_share"}


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
    ) -> None:
        self._app_token = app_token
        self._user_id = user_id
        self._allowed_user_ids = allowed_user_ids or set()
        self._allowed_user_ids.add(user_id)
        self._allow_all_senders = "*" in self._allowed_user_ids
        logger.debug(
            "init: user_id=%s, allowed_user_ids=%s, allow_all=%s",
            user_id,
            self._allowed_user_ids,
            self._allow_all_senders,
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
        self._pending_msg: dict[
            int, deque[tuple[str, str]]
        ] = {}  # session -> FIFO of (channel, ts)

    def set_orchestrator(self, orchestrator: object) -> None:
        self._orchestrator = orchestrator

    def workspace_name(self, chat_id: int) -> str:
        return f"slack/{self._workspace_names.get(chat_id, str(chat_id))}"

    def sender_name(self, chat_id: int) -> str:
        return self._sender_names.get(chat_id, "unknown")

    def channel_context(self, chat_id: int) -> str:
        return self._channel_contexts.get(chat_id, "dm")

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
        if subtype and subtype not in _ALLOWED_SUBTYPES:
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

        # Channels: only allowed users, only @mentions
        if channel_type in ("channel", "group"):
            if not self._allow_all_senders and sender_id not in self._allowed_user_ids:
                logger.debug("skipped: sender %s not in allowed_user_ids", sender_id)
                return
            mention = f"<@{self._user_id}>"
            if mention not in text:
                logger.debug("skipped: no mention of %s in text", self._user_id)
                return
            text = re.sub(f"<@{self._user_id}>\\s*", "", text).strip()

        session_id = _session_key(channel, thread_ts)
        self._sessions[session_id] = (channel, thread_ts)
        logger.debug(
            "session: id=%s channel=%s thread_ts=%s", session_id, channel, thread_ts
        )

        sender = await self._resolve_sender(event.get("user", "unknown"))
        self._sender_names[session_id] = sender
        await self._resolve_session_metadata(
            session_id, sender, channel, channel_type, thread_ts
        )

        files = event.get("files") or []
        file_lines: list[str] = []
        if files:
            file_lines = await self._save_files(session_id, files)

        if not text and not forwards and not file_lines:
            logger.debug("skipped: empty text after processing")
            return

        self._processed_ts.append(ts)
        self._active_channels[channel] = ts
        if channel_type:
            self._channel_types[channel] = channel_type

        cover_parts: list[str] = []
        if file_lines:
            cover_parts.extend(file_lines)
        if text:
            cover_parts.append(text)
        cover = "\n".join(cover_parts)

        segments: list[str] = [_render_forward(f) for f in forwards]
        if cover:
            segments.append(f"[from: {sender}] {cover}")
        elif forwards:
            segments.append(f"[from: {sender}]")
        final_text = "\n\n".join(segments)

        self._pending_msg.setdefault(session_id, deque()).append((channel, ts))
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

    async def send(self, chat_id: int, response: Response) -> None:
        route = self._sessions.get(chat_id)
        if not route:
            logger.error("No channel found for session %s", chat_id)
            return
        channel, thread_ts = route
        logger.debug(
            "send: session=%s channel=%s thread_ts=%s", chat_id, channel, thread_ts
        )

        blocks = []
        for chunk in _split_blocks(response.body):
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
            )
        stats, tools = footer_parts(response, "slack")
        if stats:
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": stats}]}
            )
        if tools:
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": tools}]}
            )

        try:
            resp = await self._app.client.chat_postMessage(
                channel=channel,
                text=response.body,
                blocks=blocks,
                thread_ts=thread_ts,
            )
            if resp.get("ok"):
                self._our_sent_timestamps.append(resp["ts"])
                logger.debug("send: ok ts=%s", resp["ts"])
            else:
                logger.warning("send: slack responded not ok: %s", resp)
        except Exception as exc:
            logger.error("send: failed to post message: %s", exc)

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
        await self._unreact(channel, ts, "hourglass_flowing_sand")
        await self._react(channel, ts, "eyes")

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
            self._channel_contexts[session_id] = f"channel:#{name} ({visibility})"

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


def main() -> None:
    from dotenv import load_dotenv

    from claude_on_the_fly.orchestrator import run
    from claude_on_the_fly.preflight import run_slack

    load_dotenv()
    app_token, user_token, user_id, allowed_user_ids = run_slack()
    frontend = SlackFrontend(
        app_token=app_token,
        user_token=user_token,
        user_id=user_id,
        allowed_user_ids=allowed_user_ids,
    )
    asyncio.run(run(frontend, platform="slack"))


if __name__ == "__main__":
    main()
