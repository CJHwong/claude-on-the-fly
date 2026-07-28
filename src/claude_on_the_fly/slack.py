"""Slack frontend over Socket Mode. Replies as you (user token) or as the app
(bot token) — the kind is inferred from the token prefix."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import time
from collections import OrderedDict, deque
from pathlib import Path
from uuid import uuid4

import aiohttp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from typing import TYPE_CHECKING, Awaitable, Callable

from claude_on_the_fly.agent import Response, cached_skills, footer_parts, get_backend
from claude_on_the_fly.jobs.core import Job, JobQueue
from claude_on_the_fly.jobs.registry import make_queue
from claude_on_the_fly.protocol import Frontend
from claude_on_the_fly.slack_mrkdwn import to_mrkdwn
from claude_on_the_fly.slack_mrkdwn import split_blocks as _split_blocks

if TYPE_CHECKING:
    from claude_on_the_fly.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
# Soft cap on agent replies per thread. Once reached, inbound messages are
# gated (no agent run) until the user sends CONTINUE_COMMAND, which resets the
# counter. Overridable via env for chattier or stricter threads.
SLACK_REPLY_SOFT_LIMIT = int(os.environ.get("SLACK_REPLY_SOFT_LIMIT", "10"))
CONTINUE_COMMAND = "$continue"
# Abort the in-flight turn. A plain-text prefix (not a slash command) so it
# works inside threads, where Slack blocks custom slash commands.
STOP_COMMAND = "$stop"
# Background-job trigger, opt-in: unset means the feature does not exist for
# this install, and a message that would have queued a job is answered as an
# ordinary one instead. When set, the message tail is queued as a job that
# survives this chat turn — the worker (claude-jobs) runs it in a fresh session
# and replies into this thread when done. A plain-text prefix, same rationale as
# $stop: it works inside threads, where Slack blocks custom slash commands.
# `checks._job_command_error` rejects the values that don't work as a trigger.
# The trigger itself is resolved per-instance in `SlackFrontend.__init__` from
# `SLACK_JOB_COMMAND` (or an explicit argument), deliberately not bound here:
# an import-time constant cannot see a value that only `load_dotenv()` puts in
# the environment, and it made the constructor's job_queue seam unusable on its
# own.
# Bot-token-only slash command, opt-in: unset registers no command at all, and
# the skill picker is reached from a message's "..." shortcut instead. When set
# it must match the command in the Slack app manifest — Slack does not namespace
# slash commands, so two installs sharing one command means the newest install
# wins and the older silently stops firing. `claude-slack --manifest` renders a
# manifest that agrees with this value. Under a user token the command is never
# received, so the $ prefixes above stay the only control surface.
SLASH_COMMAND = os.environ.get("SLACK_SLASH_COMMAND") or None
# The 185 default spinner verbs Claude Code ships with. Rendered by
# assistant.threads.setStatus as "<bot> is <verb>… (Ns)" while a turn runs, so
# the status is alive rather than a frozen "thinking". Source:
# github.com/wynandw87/claude-code-spinner-verbs (built-in defaults).
SPINNER_VERBS = (
    "Accomplishing",
    "Actioning",
    "Actualizing",
    "Architecting",
    "Baking",
    "Beaming",
    "Beboppin'",
    "Befuddling",
    "Billowing",
    "Blanching",
    "Bloviating",
    "Boogieing",
    "Boondoggling",
    "Booping",
    "Bootstrapping",
    "Brewing",
    "Burrowing",
    "Calculating",
    "Canoodling",
    "Caramelizing",
    "Cascading",
    "Catapulting",
    "Cerebrating",
    "Channeling",
    "Channelling",
    "Choreographing",
    "Churning",
    "Clauding",
    "Coalescing",
    "Cogitating",
    "Combobulating",
    "Composing",
    "Computing",
    "Concocting",
    "Considering",
    "Contemplating",
    "Cooking",
    "Crafting",
    "Creating",
    "Crunching",
    "Crystallizing",
    "Cultivating",
    "Deciphering",
    "Deliberating",
    "Determining",
    "Dilly-dallying",
    "Discombobulating",
    "Doing",
    "Doodling",
    "Drizzling",
    "Ebbing",
    "Effecting",
    "Elucidating",
    "Embellishing",
    "Enchanting",
    "Envisioning",
    "Evaporating",
    "Fermenting",
    "Fiddle-faddling",
    "Finagling",
    "Flambeing",
    "Flibbertigibbeting",
    "Flowing",
    "Flummoxing",
    "Fluttering",
    "Forging",
    "Forming",
    "Frolicking",
    "Frosting",
    "Gallivanting",
    "Galloping",
    "Garnishing",
    "Generating",
    "Germinating",
    "Gitifying",
    "Grooving",
    "Gusting",
    "Harmonizing",
    "Hashing",
    "Hatching",
    "Herding",
    "Honking",
    "Hullaballooing",
    "Hyperspacing",
    "Ideating",
    "Imagining",
    "Improvising",
    "Incubating",
    "Inferring",
    "Infusing",
    "Ionizing",
    "Jitterbugging",
    "Julienning",
    "Kneading",
    "Leavening",
    "Levitating",
    "Lollygagging",
    "Manifesting",
    "Marinating",
    "Meandering",
    "Metamorphosing",
    "Misting",
    "Moonwalking",
    "Moseying",
    "Mulling",
    "Mustering",
    "Musing",
    "Nebulizing",
    "Nesting",
    "Newspapering",
    "Noodling",
    "Nucleating",
    "Orbiting",
    "Orchestrating",
    "Osmosing",
    "Perambulating",
    "Percolating",
    "Perusing",
    "Philosophising",
    "Photosynthesizing",
    "Pollinating",
    "Pondering",
    "Pontificating",
    "Pouncing",
    "Precipitating",
    "Prestidigitating",
    "Processing",
    "Proofing",
    "Propagating",
    "Puttering",
    "Puzzling",
    "Quantumizing",
    "Razzle-dazzling",
    "Razzmatazzing",
    "Recombobulating",
    "Reticulating",
    "Roosting",
    "Ruminating",
    "Sauteing",
    "Scampering",
    "Schlepping",
    "Scurrying",
    "Seasoning",
    "Shenaniganing",
    "Shimmying",
    "Simmering",
    "Skedaddling",
    "Sketching",
    "Slithering",
    "Smooshing",
    "Sock-hopping",
    "Spelunking",
    "Spinning",
    "Sprouting",
    "Stewing",
    "Sublimating",
    "Swirling",
    "Swooping",
    "Symbioting",
    "Synthesizing",
    "Tempering",
    "Thinking",
    "Thundering",
    "Tinkering",
    "Tomfoolering",
    "Topsy-turvying",
    "Transfiguring",
    "Transmuting",
    "Twisting",
    "Undulating",
    "Unfurling",
    "Unravelling",
    "Vibing",
    "Waddling",
    "Wandering",
    "Warping",
    "Whatchamacalliting",
    "Whirlpooling",
    "Whirring",
    "Whisking",
    "Wibbling",
    "Working",
    "Wrangling",
    "Zesting",
    "Zigzagging",
)
# Max live threads whose per-session state we retain. Past this, the
# least-recently-active thread is evicted; it re-hydrates from scratch if it
# ever sees another message. Bounds memory in a long-running daemon.
SLACK_SESSION_CAP = int(os.environ.get("SLACK_SESSION_CAP", "1000"))
# Seconds of elapsed time between spinner-verb changes. The order is shuffled
# once per turn (at message-in); ticks just index into it by elapsed time.
STATUS_VERB_ROTATE_SECS = 4
_ALLOWED_SUBTYPES = {"file_share"}
_FALLBACK_ERRORS = frozenset({"not_in_channel", "is_archived", "channel_not_found"})


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


# Slack only shows a short single line for an option description; keep it well
# under the 75-char hard cap and truncate on a word boundary with an ellipsis.
SKILL_DESC_MAXLEN = 72


def _one_line(text: str) -> str:
    """Collapse whitespace and truncate to one short line for Slack display."""
    text = " ".join(text.split())
    if len(text) <= SKILL_DESC_MAXLEN:
        return text
    return (
        text[:SKILL_DESC_MAXLEN].rsplit(" ", 1)[0] or text[:SKILL_DESC_MAXLEN]
    ) + "…"


def _skill_option_groups(skills: list[tuple[str, str]]) -> list[dict]:
    """Group (name, description) skills by plugin namespace into Block Kit
    option_groups (label = plugin, or 'user' for un-namespaced names).

    A static_select shows these browsable on open, and option_groups lift the
    flat 100-option cap (up to 100 groups x 100 options), so the whole list is
    reachable without typing. Value stays the full `plugin:skill` name so the
    forward matches what the agent expects.
    """
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for name, desc in skills:
        plugin, sep, short = name.partition(":")
        if not sep:
            plugin, short = "user", name
        grouped.setdefault(plugin, []).append((name, short, desc))
    groups: list[dict] = []
    for plugin in sorted(grouped):
        options = []
        for name, short, desc in sorted(grouped[plugin], key=lambda t: t[1])[:100]:
            option = {
                "text": {"type": "plain_text", "text": short[:75]},
                "value": name[:75],
            }
            if desc:
                option["description"] = {"type": "plain_text", "text": _one_line(desc)}
            options.append(option)
        groups.append(
            {"label": {"type": "plain_text", "text": plugin[:75]}, "options": options}
        )
    return groups[:100]


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
        token: str,
        user_id: str,
        allowed_user_ids: set[str] | None = None,
        blocked_senders: set[str] | None = None,
        allowed_bot_ids: set[str] | None = None,
        silent_sender_ids: set[str] | None = None,
        job_command: str | None = None,
        job_queue: JobQueue | None = None,
    ) -> None:
        self._app_token = app_token
        self._user_id = user_id
        # Producer side of the background-jobs bridge. Trigger and queue resolve
        # together, here rather than at import: injecting a queue alone could
        # not switch the feature on, since the branch gated on the module
        # global, and a caller had to reach in and patch that too. Resolving at
        # construction also means a SLACK_JOB_COMMAND that only exists in .env
        # works — `main` calls load_dotenv() before this runs, whereas the
        # import-time binding needed the value already in the environment.
        self._job_command = job_command or os.environ.get("SLACK_JOB_COMMAND") or None
        # Defaults to the same file queue the worker reads (make_queue), so the
        # trigger and claude-jobs agree on one inbox. Built only when the
        # trigger is set: make_queue() raises ValueError on an unknown
        # JOBS_QUEUE_KIND, so without this gate a typo in the jobs env kills the
        # *Slack* daemon for someone who never enabled jobs at all.
        self._job_queue: JobQueue | None = job_queue or (
            make_queue() if self._job_command else None
        )
        self._allowed_user_ids = allowed_user_ids or set()
        self._allowed_user_ids.add(user_id)
        self._allow_all_senders = "*" in self._allowed_user_ids
        # Blocks both humans (U…) and bots (B…) — a single sender denylist.
        self._blocked_senders = blocked_senders or set()
        self._allowed_bot_ids = allowed_bot_ids or set()
        self._silent_sender_ids = silent_sender_ids or set()
        logger.debug(
            "init: user_id=%s, allowed_user_ids=%s, allow_all=%s, blocked_senders=%s, allowed_bot_ids=%s, silent_sender_ids=%s",
            user_id,
            self._allowed_user_ids,
            self._allow_all_senders,
            self._blocked_senders,
            self._allowed_bot_ids,
            self._silent_sender_ids,
        )
        # A bot token (xoxb-) replies as the app, so Bolt's own self-event
        # filter correctly drops our reply echoes — let it. A user token (xoxp-)
        # replies as the human, and we deliberately keep self-events so messages
        # typed from another Slack client still reach the agent; dedup of our own
        # replies then falls to _our_sent_timestamps (which _catchup relies on
        # regardless, since fetched history bypasses Bolt's filter).
        self._is_bot_token = token.startswith("xoxb-")
        self._app = AsyncApp(
            token=token, ignoring_self_events_enabled=self._is_bot_token
        )
        self._handler: AsyncSocketModeHandler | None = None
        self._on_message: Callable[[int, str], Awaitable[None]] | None = None
        self._orchestrator: Orchestrator | None = None
        self._warm_task: asyncio.Task | None = None
        self._sessions: OrderedDict[int, tuple[str, str | None]] = OrderedDict()
        # Slash commands have a channel but no thread timestamp. Remember the
        # most recent registered session in each channel, plus the one that is
        # currently running, so a slash command targets a real message or
        # command-anchor session instead of an unregistered root.
        self._our_sent_timestamps: deque[str] = deque(maxlen=500)
        self._processed_ts: deque[str] = deque(maxlen=1000)
        self._active_channels: dict[str, str] = {}  # channel_id -> last event_ts
        self._channel_types: dict[str, str] = {}  # channel_id -> channel_type
        self._own_dm: dict[str, bool] = {}  # channel_id -> is a DM the bot is in
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
        self._reply_counts: dict[int, int] = {}  # session -> agent replies sent
        self._status_started: dict[int, float] = {}  # session -> turn start (mono)
        self._status_verbs: dict[int, list[str]] = {}  # session -> shuffled verbs

    def set_orchestrator(self, orchestrator: object) -> None:
        from claude_on_the_fly.orchestrator import Orchestrator

        if not isinstance(orchestrator, Orchestrator):
            raise TypeError(f"Expected Orchestrator, got {type(orchestrator)}")
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
            "token_kind": "bot" if self._is_bot_token else "user",
            "user_id": self._user_id,
            "allowed_users": allowed or "<none>",
            "blocked_senders": ",".join(sorted(self._blocked_senders)) or "<none>",
            "allowed_bots": ",".join(sorted(self._allowed_bot_ids)) or "<none>",
            "silent_senders": ",".join(sorted(self._silent_sender_ids)) or "<none>",
        }

    def _evict_stale_sessions(self) -> None:
        """Drop the least-recently-active threads once over SLACK_SESSION_CAP.

        _sessions is an OrderedDict moved-to-end on every message, so the front
        is the oldest by last activity. Active threads (in-flight, or replied to
        recently) sit at the back and are never the eviction candidate.
        """
        while len(self._sessions) > SLACK_SESSION_CAP:
            oldest_id = next(iter(self._sessions))
            self._forget_session(oldest_id)
            logger.debug("evicted stale session %s", oldest_id)

    def _forget_session(self, session_id: int) -> None:
        """Drop every per-session dict entry for one thread. All of this state
        is reconstructable, so a re-hydrating thread just re-resolves it."""
        self._sessions.pop(session_id, None)
        self._workspace_names.pop(session_id, None)
        self._sender_names.pop(session_id, None)
        self._channel_contexts.pop(session_id, None)
        self._session_sender_ids.pop(session_id, None)
        self._reply_counts.pop(session_id, None)
        self._pending_msg.pop(session_id, None)
        self._pending_reply_suppressed.pop(session_id, None)
        self._in_flight.pop(session_id, None)
        self._in_flight_reply_suppressed.pop(session_id, None)

    # --- Lifecycle ---

    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        self._on_message = on_message

        @self._app.event({"type": "message"})
        async def handle_message(event):
            logger.debug("raw slack event: %s", event)
            await self._ingest_event(event)

        self._app.event("hello")(self._on_hello)

        # Slash commands + the skill picker are an app interaction: they only
        # reach a bot-token install (commands/interactivity scopes). A user
        # token never receives them, so registering would be dead weight.
        if self._is_bot_token:
            self._register_app_interactions()
            self._warm_task = asyncio.create_task(self._warm_skills())

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._handler.start_async()
        logger.info("Slack connected via Socket Mode (user_id=%s)", self._user_id)

    def _register_app_interactions(self) -> None:
        """Register the bot-token-only surface: picker, shortcut, and (when
        SLACK_SLASH_COMMAND is set) the slash command.

        The view and shortcut callback ids are app-scoped, so they can't collide
        with another install and register unconditionally. The slash command is
        workspace-global and therefore opt-in."""
        app = self._app

        if SLASH_COMMAND:

            @app.command(SLASH_COMMAND)
            async def handle_command(ack, command, body, respond):
                await self._handle_slash_command(ack, command, body, respond)

        @app.view("cof_picker")
        async def handle_picker_submit(ack, view):
            await self._handle_picker_submit(ack, view)

        @app.shortcut("cof_run_skill")
        async def handle_run_skill_shortcut(ack, shortcut):
            await self._handle_run_skill_shortcut(ack, shortcut)

        logger.info(
            "slack: skill picker registered (slash command: %s)",
            SLASH_COMMAND or "off, use the message shortcut",
        )

    async def _warm_skills(self) -> None:
        """Populate the backend skill cache before the first picker opens.

        Slack gives an options request 3s; a cold probe spawns the CLI and can
        blow that, so the first picker would show an empty menu. Warming at
        startup makes the first real request hit the cache.
        """
        try:
            # force=True so a restart re-probes and picks up plugin changes,
            # rather than serving a stale (but within-TTL) cached list.
            names = await cached_skills(get_backend(), force=True)
            logger.info("slack: warmed %d skills for picker", len(names))
        except Exception:
            logger.exception("slack: skill warm failed")

    def _is_allowed(self, sender_id: str) -> bool:
        """Single dispatch gate: only allowed senders reach the agent.

        Mirrors the message-path check in _ingest_event so every entry point
        (messages, slash command, shortcut, picker) enforces the same policy.
        """
        if sender_id in self._blocked_senders:
            return False
        return self._allow_all_senders or sender_id in self._allowed_user_ids

    async def _handle_slash_command(self, ack, command, body, respond) -> None:
        text = (command.get("text") or "").strip()
        channel = command.get("channel_id", "")
        user_id = command.get("user_id", "")
        logger.info("slack: slash command text=%r channel=%s", text, channel)
        if not self._is_allowed(user_id):
            logger.info("slack: slash command from %s denied (not allowed)", user_id)
            await ack()
            await respond("Not authorized.")
            return
        # A bare command opens the picker; anything else is forwarded verbatim as a
        # `/skill args` prompt. Turn control (stop/continue) is the $stop/$continue
        # text prefix instead, since it must work inside threads where slash
        # commands can't run.
        if not text:
            await ack()
            await self._open_skill_picker(body.get("trigger_id", ""), channel, user_id)
            return
        await ack()
        prompt = f"/{text}"
        thread_ts = await self._anchor_run(channel, None, f"Running `{prompt}`…")
        session_id = await self._enter_command_session(channel, user_id, thread_ts)
        if self._on_message:
            await self._on_message(session_id, prompt)

    async def _handle_run_skill_shortcut(self, ack, shortcut) -> None:
        await ack()
        channel = (shortcut.get("channel") or {}).get("id", "")
        user_id = (shortcut.get("user") or {}).get("id", "")
        if not self._is_allowed(user_id):
            logger.info(
                "slack: run-skill shortcut from %s denied (not allowed)", user_id
            )
            return
        message = shortcut.get("message") or {}
        # A message shortcut carries the message it fired on; use its thread so
        # the run continues that thread (thread_ts for a reply, else its own ts).
        thread_ts = message.get("thread_ts") or message.get("ts")
        logger.info(
            "slack: run-skill shortcut channel=%s thread_ts=%s", channel, thread_ts
        )
        await self._open_skill_picker(
            shortcut.get("trigger_id", ""), channel, user_id, thread_ts
        )

    async def _open_skill_picker(
        self, trigger_id: str, channel: str, user_id: str, thread_ts: str | None = None
    ) -> None:
        if not trigger_id:
            return
        try:
            skills = await cached_skills(get_backend())
        except Exception:
            logger.exception("skill picker: failed to load skills")
            skills = []
        groups = _skill_option_groups(skills)
        # static_select shows the full grouped list on open (no typing). When
        # there are no skills, drop the select so the modal still renders.
        skill_block = (
            {
                "type": "input",
                "block_id": "skill",
                "label": {"type": "plain_text", "text": "Skill"},
                "element": {
                    "type": "static_select",
                    "action_id": "cof_skill",
                    "placeholder": {"type": "plain_text", "text": "pick a skill"},
                    "option_groups": groups,
                },
            }
            if groups
            else {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "No skills available."},
            }
        )
        view = {
            "type": "modal",
            "callback_id": "cof_picker",
            "private_metadata": f"{channel}:{user_id}:{thread_ts or ''}",
            "title": {"type": "plain_text", "text": "Run a skill"},
            "submit": {"type": "plain_text", "text": "Run"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                skill_block,
                {
                    "type": "input",
                    "block_id": "args",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Arguments"},
                    "element": {"type": "plain_text_input", "action_id": "cof_args"},
                },
            ],
        }
        try:
            await self._app.client.views_open(trigger_id=trigger_id, view=view)
        except SlackApiError as exc:
            logger.error("views_open failed: %s", exc)

    async def _handle_picker_submit(self, ack, view) -> None:
        await ack()
        values = view.get("state", {}).get("values", {})
        selected = (
            values.get("skill", {}).get("cof_skill", {}).get("selected_option") or {}
        )
        skill = selected.get("value")
        args = values.get("args", {}).get("cof_args", {}).get("value") or ""
        channel, _, rest = (view.get("private_metadata") or "").partition(":")
        user_id, _, thread_ts = rest.partition(":")
        if not skill or not channel:
            return
        if not self._is_allowed(user_id):
            logger.info("slack: picker submit from %s denied (not allowed)", user_id)
            return
        prompt = f"/{skill} {args}".strip()
        thread_ts = await self._anchor_run(
            channel, thread_ts or None, f"Running `{prompt}`…"
        )
        session_id = await self._enter_command_session(channel, user_id, thread_ts)
        if self._on_message:
            await self._on_message(session_id, prompt)

    async def _enter_command_session(
        self, channel: str, user_id: str, thread_ts: str | None = None
    ) -> int:
        """Register the session a command/shortcut forward targets.

        A slash command carries no thread_ts (channel/DM root); a message
        shortcut does, so it continues that thread. Mirrors _ingest_event's
        session bookkeeping so send() can route the reply and the agent gets a
        workspace + context.
        """
        session_id = _session_key(channel, thread_ts)
        self._sessions[session_id] = (channel, thread_ts)
        self._sessions.move_to_end(session_id)
        self._evict_stale_sessions()
        sender = await self._resolve_sender(user_id) if user_id else "unknown"
        self._sender_names[session_id] = sender
        if user_id:
            self._session_sender_ids[session_id] = user_id
        channel_type = await self._channel_type(channel)
        await self._resolve_session_metadata(
            session_id, sender, channel, channel_type, thread_ts or ""
        )
        return session_id

    async def _channel_type(self, channel: str) -> str:
        cached = self._channel_types.get(channel)
        if cached:
            return cached
        try:
            info = await self._app.client.conversations_info(channel=channel)
            ch = info["channel"]
        except Exception as exc:
            logger.warning("channel_type: failed for %s: %s", channel, exc)
            return ""
        if ch.get("is_im"):
            kind = "im"
        elif ch.get("is_mpim"):
            kind = "mpim"
        elif ch.get("is_group") or ch.get("is_private"):
            kind = "group"
        else:
            kind = "channel"
        self._channel_types[channel] = kind
        return kind

    async def _is_bot_conversation(self, channel: str) -> bool:
        """Whether `channel` is a DM/group-DM the bot itself is in.

        A bot-token app that also holds the user-token grant receives the
        authorizing user's *other* DMs (with third parties) too, which are not
        addressed to the bot. conversations.info on the bot token resolves the
        bot's own DMs and returns channel_not_found / not_in_channel for ones it
        isn't in. Cached per channel; fail-open (only a definitive not-a-member
        error is False) so it can never drop the bot's own DMs.
        """
        cached = self._own_dm.get(channel)
        if cached is not None:
            return cached
        result = True
        try:
            info = await self._app.client.conversations_info(channel=channel)
            ch = info["channel"]
            result = bool(ch.get("is_im") or ch.get("is_mpim"))
        except SlackApiError as exc:
            if exc.response.get("error") in ("channel_not_found", "not_in_channel"):
                result = False
        except Exception as exc:
            logger.warning("is_bot_conversation: %s: %s", channel, exc)
        self._own_dm[channel] = result
        return result

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
        # no user field. Only let through bots trusted by bot_id and not blocked.
        is_trusted_bot = (
            subtype == "bot_message"
            and bot_id in self._allowed_bot_ids
            and bot_id not in self._blocked_senders
        )
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
            if sender_id in self._blocked_senders:
                logger.debug("skipped: sender %s in blocked_senders", sender_id)
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

            # Under a bot token the app also receives the authorizing user's own
            # DMs with third parties (via the user-token grant); those aren't
            # addressed to the bot. Only act on DMs the bot itself is in.
            if (
                self._is_bot_token
                and channel_type in ("im", "mpim")
                and not await self._is_bot_conversation(channel)
            ):
                logger.debug("skipped: %s is not a DM the bot is in", channel)
                return

        session_id = _session_key(channel, thread_ts)
        is_new_session = session_id not in self._sessions
        is_mid_thread = bool(event.get("thread_ts")) and event["thread_ts"] != ts
        self._sessions[session_id] = (channel, thread_ts)
        self._sessions.move_to_end(session_id)
        self._evict_stale_sessions()
        logger.debug(
            "session: id=%s channel=%s thread_ts=%s", session_id, channel, thread_ts
        )

        # $stop aborts this thread's in-flight turn. Checked before the soft-limit
        # gate so a stop lands even when the thread is over budget. Works in
        # threads (it's a message, not a slash command).
        if text.strip() == STOP_COMMAND:
            logger.info("slack %s/%s: %s", channel, thread_ts, STOP_COMMAND)
            stopped = False
            if self._orchestrator is not None:
                stopped = await self._orchestrator.abort(session_id)
            await self._post_notice(
                channel,
                thread_ts,
                "Stopped the current turn." if stopped else "Nothing was running.",
            )
            return

        # The job trigger queues a background job that outlives this chat turn;
        # the worker replies into this thread when done. Opt-in, so the whole
        # branch is skipped unless the trigger is set and a queue was built —
        # with the feature off the message falls through to the normal path and
        # is answered as ordinary text. Placed before the soft-limit
        # gate so enqueue+ack isn't blocked by the reply budget, and before the
        # message reaches self._pending_msg (slack.py) so there's no orphaned
        # pending entry / hourglass. sender_id (raw id) is already resolved and
        # authorized at the allow/block gate above; the resolved display `sender`
        # isn't assigned yet — and the notifier reads neither, so origin carries
        # sender_id.
        job_command = self._job_command
        job_text = text.strip()
        if (
            job_command
            and self._job_queue is not None
            and (job_text == job_command or job_text.startswith(job_command + " "))
        ):
            task = job_text[len(job_command) :].strip()
            # Unlike $stop, $job is not idempotent — a catchup re-ingest on
            # reconnect would enqueue a second job. Mirror the normal path's
            # catch-up bookkeeping (which this branch returns before reaching):
            # mark the ts processed (dedup), record the channel + watermark so
            # _catchup re-fetches it after a disconnect, and cache the channel
            # type so the recovered messages gate identically to the live path.
            self._processed_ts.append(ts)
            self._active_channels[channel] = ts
            if channel_type:
                self._channel_types[channel] = channel_type
            if not task:
                await self._post_notice(
                    channel,
                    thread_ts,
                    f"Usage: `{job_command} <task>` — I'll run it in the "
                    "background and reply here when it's done.",
                )
                return
            job = Job(
                id=f"{time.time_ns()}-{uuid4().hex[:8]}",
                prompt=task,
                origin={
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "sender_id": sender_id,
                },
            )
            try:
                self._job_queue.enqueue(job)
            except Exception as exc:
                logger.exception(
                    "slack %s/%s: job enqueue failed: %s", channel, thread_ts, exc
                )
                await self._post_notice(
                    channel,
                    thread_ts,
                    "Couldn't queue that job — check the worker logs.",
                )
                return
            logger.info("slack %s/%s: queued job %s", channel, thread_ts, job.id)
            await self._post_notice(
                channel,
                thread_ts,
                f"Queued job `{job.id}` — I'll reply here when it's done.",
            )
            return

        # Reply soft-limit gate. CONTINUE_COMMAND resets the counter and any
        # trailing text is processed as the next turn; otherwise, once the
        # thread is over budget we post the warning and stop here (no agent run).
        stripped = text.strip()
        if stripped == CONTINUE_COMMAND or stripped.startswith(CONTINUE_COMMAND + " "):
            self._reply_counts[session_id] = 0
            text = stripped[len(CONTINUE_COMMAND) :].strip()
            logger.info(
                "slack %s/%s: reply count reset via continue", channel, thread_ts
            )
        elif self._reply_counts.get(session_id, 0) >= SLACK_REPLY_SOFT_LIMIT:
            logger.info(
                "slack %s/%s: reply soft-limit %d reached, gating message",
                channel,
                thread_ts,
                SLACK_REPLY_SOFT_LIMIT,
            )
            await self._warn_reply_limit(channel, thread_ts)
            return

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
        silent = bool(bot_id and bot_id in self._silent_sender_ids) or (
            sender_id in self._silent_sender_ids
        )
        self._pending_reply_suppressed.setdefault(session_id, deque()).append(silent)
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
                "slack %s/%s => reply omitted for silenced sender",
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
            self._reply_counts[chat_id] = self._reply_counts.get(chat_id, 0) + 1
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

    async def _warn_reply_limit(self, channel: str, thread_ts: str | None) -> None:
        """Tell the user the thread hit the reply soft-limit and how to resume."""
        note = (
            f"Hit the {SLACK_REPLY_SOFT_LIMIT}-message limit for this thread. "
            f"Reply `{CONTINUE_COMMAND}` to keep going, or "
            f"`{CONTINUE_COMMAND} <your next message>` to continue and ask in one go."
        )
        try:
            resp = await self._app.client.chat_postMessage(
                channel=channel, text=note, thread_ts=thread_ts
            )
        except Exception as exc:
            logger.error("reply-limit: failed to post warning: %s", exc)
            return
        if resp.get("ok"):
            self._our_sent_timestamps.append(resp["ts"])

    async def _anchor_run(
        self, channel: str, thread_ts: str | None, label: str
    ) -> str | None:
        """Return the thread a command/picker run should live in.

        A message shortcut already carries a thread, so reuse it. A slash
        command / bare picker has none, so post a real anchor message and thread
        the run under it — that both contains the run and gives the progress
        status a thread to attach to (setStatus is thread-scoped). Falls back to
        channel-root (no status) if the anchor post fails.
        """
        if thread_ts:
            return thread_ts
        return await self._post_anchor(channel, label)

    async def _post_anchor(self, channel: str, text: str) -> str | None:
        try:
            resp = await self._app.client.chat_postMessage(channel=channel, text=text)
        except Exception as exc:
            logger.error("anchor: failed to post %r: %s", text, exc)
            return None
        if resp.get("ok"):
            ts = resp["ts"]
            self._our_sent_timestamps.append(ts)
            return ts
        return None

    async def _post_notice(
        self, channel: str, thread_ts: str | None, text: str
    ) -> None:
        """Post a short control-command acknowledgement into the thread.

        Records the ts so a user-token deploy doesn't re-ingest our own notice
        as an inbound message (same echo guard as send/_warn_reply_limit)."""
        try:
            resp = await self._app.client.chat_postMessage(
                channel=channel, text=text, thread_ts=thread_ts
            )
        except Exception as exc:
            logger.error("post_notice: failed to post %r: %s", text, exc)
            return
        if resp.get("ok"):
            self._our_sent_timestamps.append(resp["ts"])

    def _record_upload_ts(self, resp: AsyncSlackResponse) -> None:
        """Record the ts of file-share messages we just posted so our own upload
        isn't re-ingested as an inbound message."""
        for f in resp.get("files") or []:
            for visibility in (f.get("shares") or {}).values():
                for share_list in visibility.values():
                    for share in share_list:
                        ts = share.get("ts")
                        if ts:
                            self._our_sent_timestamps.append(ts)

    async def send_typing(self, chat_id: int) -> None:
        """Live progress tick. The orchestrator calls this every ~4s while a
        turn runs; repurpose it to refresh the bot status with a rotating verb
        and elapsed seconds, so a long turn visibly stays alive."""
        start = self._status_started.get(chat_id)
        seq = self._status_verbs.get(chat_id)
        if start is None or not seq:
            return
        elapsed = int(time.monotonic() - start)
        verb = seq[(elapsed // STATUS_VERB_ROTATE_SECS) % len(seq)]
        await self._set_status(chat_id, f"is {verb}… ({elapsed}s)")

    async def notify_queued(self, chat_id: int, position: int) -> None:
        """React with hourglass on the most recently ingested message."""
        pending = self._pending_msg.get(chat_id)
        if not pending:
            logger.debug("notify_queued: no pending msg for chat_id=%s", chat_id)
            return
        channel, ts = pending[-1]
        await self._react(channel, ts, "hourglass_flowing_sand")

    async def notify_start(self, chat_id: int) -> None:
        """Start the live status, then (for message-driven turns) flip the
        hourglass reaction to eyes.

        The status runs for every path: slash commands and picker runs have no
        pending reaction message but still get a status via their session's
        thread, so it must not sit behind the pending-msg guard.
        """
        self._status_started[chat_id] = time.monotonic()
        # Shuffle the verb order once here; ticks walk it by elapsed time.
        seq = [v.lower() for v in random.sample(SPINNER_VERBS, len(SPINNER_VERBS))]
        self._status_verbs[chat_id] = seq
        await self._set_status(chat_id, f"is {seq[0]}…")
        pending = self._pending_msg.get(chat_id)
        if not pending:
            logger.debug(
                "notify_start: no pending reaction msg for chat_id=%s", chat_id
            )
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
        """Remove :eyes: from the in-flight message and clear the status."""
        self._status_started.pop(chat_id, None)
        self._status_verbs.pop(chat_id, None)
        await self._set_status(chat_id, "")
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

    async def _set_status(self, chat_id: int, status: str) -> None:
        """Bot-only 'is thinking…' indicator via assistant.threads.setStatus.

        No-op under a user token (the method is bot-only) and when the session
        has no thread to attach to. Guarded: if Slack rejects it (e.g. the
        thread isn't an assistant thread), it degrades silently to the emoji
        reaction that's already shown. Pass "" to clear.
        """
        if not self._is_bot_token:
            return
        route = self._sessions.get(chat_id)
        if not route:
            return
        channel, thread_ts = route
        if not thread_ts:
            return
        try:
            await self._app.client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=status
            )
        except Exception as exc:
            logger.warning(
                "set_status: %r not applied (%s/%s): %s",
                status,
                channel,
                thread_ts,
                exc,
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
            self._reply_counts[chat_id] = self._reply_counts.get(chat_id, 0) + 1
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
    import argparse

    from dotenv import load_dotenv

    from claude_on_the_fly import slack_manifest
    from claude_on_the_fly.orchestrator import run
    from claude_on_the_fly.preflight import run_slack

    parser = argparse.ArgumentParser(prog="claude-slack")
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="generate a Slack app manifest for this install, then exit",
    )
    parser.add_argument(
        "--mode",
        choices=slack_manifest.MODES,
        help="manifest token kind; asked interactively when omitted",
    )
    parser.add_argument("--name", help="app name as it appears in Slack")
    parser.add_argument(
        "--command",
        help="slash command to declare, e.g. /cof-yourname (bot mode only)",
    )
    parser.add_argument(
        "--out", help="write the manifest here instead of stdout (flag mode)"
    )
    args = parser.parse_args()

    if args.manifest:
        raise SystemExit(
            slack_manifest.generate(
                mode=args.mode, name=args.name, command=args.command, out=args.out
            )
        )

    load_dotenv()
    (
        app_token,
        token,
        user_id,
        allowed_user_ids,
        blocked_senders,
        allowed_bot_ids,
        silent_sender_ids,
    ) = run_slack()
    frontend = SlackFrontend(
        app_token=app_token,
        token=token,
        user_id=user_id,
        allowed_user_ids=allowed_user_ids,
        blocked_senders=blocked_senders,
        allowed_bot_ids=allowed_bot_ids,
        silent_sender_ids=silent_sender_ids,
    )
    asyncio.run(run(frontend, platform="slack"))


if __name__ == "__main__":  # pragma: no cover
    main()
