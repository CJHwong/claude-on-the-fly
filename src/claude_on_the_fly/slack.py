"""Slack frontend over Socket Mode. Replies as you (user token) or as the app
(bot token) — the kind is inferred from the token prefix."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import aiohttp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from claude_on_the_fly import checks, logs, settings
from claude_on_the_fly.agent import (
    DATA_DIR,
    MAX_ATTACHMENT_BYTES,
    Response,
    cached_skills,
    footer_parts,
    get_backend,
    persona_for,
    read_attachment,
    sender_marker,
    write_attachment,
)
from claude_on_the_fly.approvals import ApprovalRequest
from claude_on_the_fly.heartbeat import live_pid
from claude_on_the_fly.jobs.core import Job, JobQueue, QueueRow
from claude_on_the_fly.jobs.registry import make_queue
from claude_on_the_fly.protocol import Frontend
from claude_on_the_fly.slack_mrkdwn import split_blocks as _split_blocks
from claude_on_the_fly.slack_mrkdwn import to_mrkdwn

if TYPE_CHECKING:
    from claude_on_the_fly.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Soft cap on agent replies per thread. Once reached, inbound messages are
# gated (no agent run) until the user sends CONTINUE_COMMAND, which resets the
# counter.
DEFAULT_REPLY_SOFT_LIMIT = 10
CONTINUE_COMMAND = "$continue"
# How long the gate notice is held before it is posted. Off by default: 0 posts
# it the moment the message is gated, which is what this always did. Set it to a
# few seconds where people fire a question and leave -- posted instantly, the
# notice arrives while the sender still has the thread open, Slack marks it read
# on arrival, and they walk away with no unread badge believing the message is
# being worked on. Waiting until they have gone makes it an unread thread reply.
DEFAULT_REPLY_LIMIT_NOTICE_SECONDS = 0.0
# Ceiling on the debounce above, timed from the first gated message. Somebody
# firing a message every three seconds would otherwise defer the notice for as
# long as they kept typing, and never learn the thread is gated. Not an operator
# setting: it is a guard on the delay, not a second thing to tune.
REPLY_LIMIT_NOTICE_MAX_HOLD = 30.0
# Channel kinds where a message only reaches the bot if it tags it. The one
# source of truth for the mention gate and for the reminder below.
TAG_REQUIRED_CHANNEL_TYPES = frozenset({"channel", "group"})
# How long an untagged channel message sits unanswered before the bot says why.
# 0, the default, turns the notice off entirely -- nothing is scheduled and no
# per-thread state is recorded, so an install that does not want the bot speaking
# unprompted in a channel pays nothing for it. When set, make it long: the sender
# is watching the thread for the reply they think is coming, and a notice posted
# into that wait is read on arrival and forgotten, while a couple of minutes of
# nothing means they have moved on and it lands as an unread ping.
DEFAULT_MENTION_NOTICE_SECONDS = 0.0
# Abort the in-flight turn. A plain-text prefix (not a slash command) so it
# works inside threads, where Slack blocks custom slash commands.
STOP_COMMAND = "$stop"
# Summarize the thread's history so later turns stop re-paying for all of it.
# Same prefix rationale as $stop, and on by default for the same reason: a
# long-running thread is exactly where nobody thinks to go looking for a setting.
COMPACT_COMMAND = "$compact"
# Background-job trigger. The message tail is queued as a job that survives this
# chat turn — the worker (claude-jobs) runs it in a fresh session and replies
# into this thread when done. A plain-text prefix, same rationale as $stop: it
# works inside threads, where Slack blocks custom slash commands, and like $stop
# it is on by default rather than something each install has to discover.
#
# `SLACK_JOB_COMMAND` renames it; setting it *empty* turns the feature off
# entirely, which is the difference between "absent" and "present but blank" in
# `_resolve_job_command`. `checks._job_command_error` rejects values that cannot
# work as a trigger.
#
# Resolved per-instance in `SlackFrontend.__init__`, deliberately not bound here:
# an import-time constant cannot see a value that only `load_dotenv()` puts in
# the environment, and it made the constructor's job_queue seam unusable on its
# own.
DEFAULT_JOB_COMMAND = "$job"
# Marker on every forwarded mid-turn message. It goes in the message's `text=` as
# well as the block: that is what a mobile push shows, and a context block is
# styling, not a prefix.
INTERIM_PREFIX = "⏳"
# Progress goes only where the audience asked for this turn. In a shared channel
# there is no reply budget protecting bystanders from it (interim posts do not
# increment one), so narration would land on people who never asked a question.
_INTERIM_CHANNEL_TYPES = frozenset({"im", "mpim"})


# Bot-token-only slash command, opt-in: unset registers no command at all, and
# the skill picker is reached from a message's "..." shortcut instead. When set
# it must match the command in the Slack app manifest — Slack does not namespace
# slash commands, so two installs sharing one command means the newest install
# wins and the older silently stops firing. `claude-slack --manifest` renders a
# manifest that agrees with this value. Under a user token the command is never
# received, so the $ prefixes above stay the only control surface.
def _positive_int(name: str, fallback: int) -> int:
    """A numeric setting, read per use so an edit lands without a restart.

    A junk value falls back and says so once per read rather than raising: these are
    a memory bound and a politeness limit, and neither is worth refusing to serve a
    message over.
    """
    raw = settings.get(name).strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %d", name, raw, fallback)
        return fallback
    if value <= 0:
        logger.warning("%s=%d must be positive; using %d", name, value, fallback)
        return fallback
    return value


def _non_negative_float(name: str, fallback: float) -> float:
    """A duration setting, read per use. Same fallback-and-say-so contract as
    `_positive_int`, but zero is a meaningful value (post with no delay)."""
    raw = settings.get(name).strip()
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, fallback)
        return fallback
    if not math.isfinite(value):
        # `float()` takes "nan" and "inf". A NaN hold survives `min()` against the
        # ceiling -- min(nan, 30) is nan -- and `asyncio.sleep(nan)` never wakes,
        # so the notice would be lost for every thread until a restart.
        logger.warning("%s=%s is not a finite number; using %s", name, value, fallback)
        return fallback
    if value < 0:
        logger.warning("%s=%s cannot be negative; using %s", name, value, fallback)
        return fallback
    return value


def reply_soft_limit() -> int:
    """Agent replies allowed per thread before inbound messages are gated."""
    return _positive_int("SLACK_REPLY_SOFT_LIMIT", DEFAULT_REPLY_SOFT_LIMIT)


def reply_limit_notice_seconds() -> float:
    """Seconds to hold the gate notice back before posting it. 0 posts it now."""
    return _non_negative_float(
        "SLACK_REPLY_LIMIT_NOTICE_SECONDS", DEFAULT_REPLY_LIMIT_NOTICE_SECONDS
    )


def mention_notice_seconds() -> float:
    """Seconds an untagged channel message waits for a "you need to tag me"
    notice. 0 disables the notice."""
    return _non_negative_float(
        "SLACK_MENTION_NOTICE_SECONDS", DEFAULT_MENTION_NOTICE_SECONDS
    )


def session_cap() -> int:
    """Live threads whose per-session state is retained before LRU eviction."""
    return _positive_int("SLACK_SESSION_CAP", DEFAULT_SESSION_CAP)


def slash_command() -> str | None:
    """The registered slash command, or None when none is configured.

    Read once, at `start`, because registering it is a handshake with Slack rather
    than a local decision -- which is why `slack.slash_command` is in
    `settings.RESTART_REQUIRED` and an edit to it earns a restart notice instead of
    silently pointing at a command nothing is listening for.
    """
    return settings.get("SLACK_SLASH_COMMAND") or None


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
DEFAULT_SESSION_CAP = 1000
# Seconds of elapsed time between spinner-verb changes. The order is shuffled
# once per turn (at message-in); ticks just index into it by elapsed time.
STATUS_VERB_ROTATE_SECS = 4

# One reaction per state of a message, and three distinct states. Named rather
# than written inline at six call sites, because the failure mode is two states
# sharing a glyph: an interrupted turn wearing the queue's hourglass cannot be
# told from one that is merely waiting its turn.
#
# `arrows_counterclockwise` for interrupted, because the cause is a restart and
# the recovery is automatic, which is exactly what it says.
QUEUED_EMOJI = "hourglass_flowing_sand"
RUNNING_EMOJI = "eyes"
INTERRUPTED_EMOJI = "arrows_counterclockwise"

_ALLOWED_SUBTYPES = {"file_share"}
_FALLBACK_ERRORS = frozenset({"not_in_channel", "is_archived", "channel_not_found"})


# How many queued jobs a bare-trigger listing shows before it summarises the
# rest. A listing is a glance, not a report, and it competes with the rest of
# the thread for screen.
JOB_LIST_LIMIT = 10
# Cap on remembered tapped suggestion menus. A tap usually retires the menu, so
# the spent set only grows on marks that failed; the cap bounds it regardless.
SUGGESTION_SPENT_CAP = 500
# Prompt characters per listed row. Long enough to recognise the job you asked
# for, short enough that ten of them stay one screen.
JOB_LIST_PROMPT_CHARS = 80


def _fmt_job_age(enqueued_at: datetime | None, now: datetime) -> str:
    """Coarse age for a listing: `12s`, `4m`, `3h`, `2d`, or `?` when the id
    carried no timestamp."""
    if enqueued_at is None:
        return "?"
    seconds = max(0, int((now - enqueued_at).total_seconds()))
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{suffix}"
    return f"{seconds}s"


def _resolve_job_command() -> str | None:
    """The background-job trigger, or None when the feature is switched off.

    Absent means the default; present-but-empty means off. The distinction is
    the opt-out — `SLACK_JOB_COMMAND=` in a `.env` is how an install that does
    not want a background-job trigger says so, and `get(name, default) or None`
    is what keeps those two cases apart.
    """
    configured = settings.lookup("SLACK_JOB_COMMAND")
    return (DEFAULT_JOB_COMMAND if configured is None else configured) or None


def _build_job_queue() -> JobQueue | None:
    """The producer's queue, or None when one cannot be built.

    `make_queue()` raises on an unrecognised `JOBS_QUEUE_KIND`. Now that the
    trigger is on by default, letting that propagate would mean a typo in the
    *jobs* configuration takes down the *Slack* daemon of an install that never
    thought about background jobs. Preflight catches the same mistake earlier
    and more legibly; this is the backstop for the paths that skip it, and it
    degrades to "no background jobs" rather than "no Slack".
    """
    try:
        return make_queue()
    except Exception as exc:
        logger.error(
            "slack: background jobs disabled — could not build the queue: %s", exc
        )
        return None


def _render_job_list(rows: list[QueueRow], channel: str, job_command: str) -> str:
    """The bare-trigger listing, as Slack mrkdwn.

    Scoped to `channel`: a listing prints other people's prompts verbatim, and
    in a shared channel the queue holds work from threads its readers were never
    part of. Jobs elsewhere are counted, never quoted, so the reader still knows
    the worker is busy without being shown what with.
    """
    usage = f"Usage: `{job_command} <task>` — runs in the background, replies here."
    here = [row for row in rows if row.origin.get("channel") == channel]
    elsewhere = len(rows) - len(here)

    lines: list[str] = []
    if not here:
        lines.append("No jobs queued from this channel.")
    else:
        now = datetime.now(UTC)
        shown = here[:JOB_LIST_LIMIT]
        lines.append(f"*{len(here)} job(s) from this channel:*")
        for row in shown:
            state = "running" if row.in_flight else "queued"
            prompt = (row.prompt or "").strip().replace("\n", " ") or "(no prompt)"
            if len(prompt) > JOB_LIST_PROMPT_CHARS:
                prompt = prompt[: JOB_LIST_PROMPT_CHARS - 1] + "…"
            lines.append(
                f"• `{state}` · {_fmt_job_age(row.enqueued_at, now)} · {prompt}"
            )
        if len(here) > len(shown):
            lines.append(f"_…and {len(here) - len(shown)} more._")
    if elsewhere:
        lines.append(f"_{elsewhere} job(s) queued from other channels._")
    lines.append(usage)
    return "\n".join(lines)


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


def _retire_suggestion_block(blocks: list[dict], label: str) -> list[dict]:
    """Rebuild message blocks with the suggestion menu collapsed to a status line.

    The whole menu goes on any tap, not just the tapped button: a second click
    must not send a second message. The context block keeps the ✓ pick visible
    in the chat as history. The label is the clicked button's own text rather
    than something parsed out of the message body, so agent-rendered content
    is never re-read out of the payload.
    """
    retired: list[dict] = []
    for block in blocks:
        if block.get("type") != "actions":
            retired.append(block)
            continue
        retired.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"✓ {label}"}],
            }
        )
    return retired


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


def _literal(text: str) -> str:
    """Make `text` safe to drop inside a mrkdwn code span.

    Slack has no general mrkdwn escape, so the only thing that keeps a code span
    literal is denying it the character that ends it. Newlines go too: a span
    cannot survive one, and the tail would render as ordinary mrkdwn.

    Used for an approval subject, which is partly agent-reachable (a broker
    route-scope request carries the path tail the agent asked for). Without this
    the agent can close the span and style the operator's own prompt.
    """
    return " ".join(text.replace("`", "'").split())


# Slack rejects a section text over 3000 characters, so a long detail has to be
# cut somewhere. Leaves room for the marker below.
_BLOCK_TEXT_LIMIT = 2900


def _fit_block(text: str) -> str:
    """`text` short enough for a Block Kit section, and honest when it was cut.

    An approval detail is the one thing the operator has to read before granting,
    so a silent truncation is the worst possible failure here: the tail is where
    a suspicious path or an unexpected method would sit. Says so instead.
    """
    if len(text) <= _BLOCK_TEXT_LIMIT:
        return text
    dropped = len(text) - _BLOCK_TEXT_LIMIT
    return f"{text[:_BLOCK_TEXT_LIMIT]}\n[{dropped} more characters, see the log]"


def _approval_headline(request: ApprovalRequest) -> str:
    """The card's first line.

    Leads with `origin` when the requester set one, because those subjects are
    digests: `pty:Bash:f5771755993b` as a headline tells the operator nothing, and
    it pushed the command -- the thing being decided -- onto a second line on a
    phone. A requester whose subject is already readable (a host, a command) sets
    no origin and keeps the subject up here.
    """
    if request.origin:
        return f"*Permission request*  ({_literal(request.origin)})"
    return f"*Permission request*\n`{_literal(request.subject)}`"


def _decided_text(request: ApprovalRequest) -> str:
    """What a retired card should say was decided.

    The scope when there is one, because that is the only text on the request that
    carries arguments. The subject otherwise -- an egress subject (`pypi.org:443`) is
    already the whole thing that was decided.
    """
    return request.scope or request.subject


def _approval_footer(request: ApprovalRequest) -> str:
    """Grant lifetime, and nothing else.

    An earlier version put the subject here, then a `Covers:` line in front of it to
    make a digest subject mean something. Both were duplication: the detail block
    above already *is* the command, so a tool card said the same thing three times --
    `(Bash)`, then `chmod 700 .`, then `Covers: bash:chmod 700 .  bash:chmod`.

    Nothing is lost by dropping them. The grant key and the full command are both on
    the GRANTED line in the log, and a retired card records the command
    (`_decided_text`), so the decision is still traceable without spending three
    lines of a phone screen on it.
    """
    return f"Grant lasts {request.ttl_seconds / 60:.0f} min and dies on restart."


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
        # `None` means "not specified, read the environment"; `""` means off.
        # Without that split a caller could rename the trigger but never
        # disable it, since every falsy override would fall through to the
        # default — the same absent-vs-blank distinction the setting itself makes.
        self._job_command = (
            job_command if job_command is not None else _resolve_job_command()
        ) or None
        # The same file queue the worker reads (make_queue), so the trigger and
        # claude-jobs agree on one inbox.
        self._job_queue: JobQueue | None = job_queue or (
            _build_job_queue() if self._job_command else None
        )
        # Access control is read per message, not pinned here, so adding a sender
        # to config.yaml takes effect on their next message instead of on the next
        # restart. A caller that passes a set is pinning it (every test does), and
        # `None` means "read the config" -- the same absent-vs-empty split the job
        # command makes, because an explicitly empty set is a real answer.
        self._pinned_allowed_user_ids = allowed_user_ids
        self._pinned_blocked_senders = blocked_senders
        self._pinned_allowed_bot_ids = allowed_bot_ids
        self._pinned_silent_sender_ids = silent_sender_ids
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
        self._own_dm: dict[str, tuple[bool, float]] = {}
        # Membership is authorization state, not immutable channel metadata.
        # Cache definitive answers briefly, and never cache an answer produced by
        # an API failure.
        self._own_dm_ttl = 60.0
        self._connected_once = False
        self._workspace_names: dict[int, str] = {}
        self._sender_names: dict[int, str] = {}
        self._channel_contexts: dict[int, str] = {}
        # session -> channel name, or "" for a DM/group DM. Set alongside the
        # workspace name and read by `persona_source` to tell a channel (keyed by
        # id or name) from a DM (keyed by whoever is talking).
        self._channel_names: dict[int, str] = {}
        self._user_name_cache: dict[str, str] = {}  # slack user_id -> display name
        self._session_sender_ids: dict[int, str] = {}  # session -> slack user_id
        self._dm_channels: dict[str, str] = {}  # slack user_id -> im channel id
        self._pending_msg: dict[
            int, deque[tuple[str, str]]
        ] = {}  # session -> FIFO of (channel, ts)
        self._pending_reply_suppressed: dict[int, deque[bool]] = {}
        # (channel, ts) -> None for suggestion menus already tapped. The menu
        # retire is a cosmetic chat_update that can fail; this is the
        # synchronous guard that actually stops a re-tap from sending twice.
        self._spent_menus: dict[tuple[str, str], None] = {}
        self._in_flight: dict[int, tuple[str, str]] = {}
        self._in_flight_reply_suppressed: dict[int, bool] = {}
        self._reply_counts: dict[int, int] = {}  # session -> agent replies sent
        # session -> the gate notice waiting out its delay. One per thread, so a
        # sender who fires three messages into a gated thread gets one warning.
        self._gate_notices: dict[int, asyncio.Task[None]] = {}
        # session -> monotonic time the notice can no longer be deferred past.
        # Held across reschedules, which is what bounds the debounce.
        self._gate_deadlines: dict[int, float] = {}
        # Threads already told that a channel needs an @mention. Once each.
        self._mention_hinted: set[int] = set()
        # session -> the idle reminder waiting for the thread to stay quiet.
        self._mention_notices: dict[int, asyncio.Task[None]] = {}
        # nonce -> future awaiting an approve/deny click. Keyed by nonce so the
        # button's `value` stays opaque and a subject never has to be encoded
        # into a client-supplied field.
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}
        self._status_started: dict[int, float] = {}  # session -> turn start (mono)
        self._status_verbs: dict[int, list[str]] = {}  # session -> shuffled verbs

    def _senders(self, key: str, pinned: set[str] | None) -> set[str]:
        """One sender set: the pinned override, else the current config.

        Goes through `checks.resolve_slack_ids` so the deprecated split allowlists
        still merge exactly as they did at startup -- one resolver, not two that can
        drift.
        """
        if pinned is not None:
            return pinned
        return checks.resolve_slack_ids(settings.environment(), key)

    def _configured_senders(self) -> set[str]:
        """The one configured allowlist, before the human/bot prefix split."""
        return checks.resolve_slack_ids(
            settings.environment(), "SLACK_ALLOWED_SENDER_IDS"
        )

    @property
    def _allowed_user_ids(self) -> set[str]:
        """Human senders allowed to trigger the agent, plus the token's own id.

        One configured list routes by Slack id prefix: `B…` is a bot and takes the
        trusted-bot path below, everything else (`U…`/`W…`/`*`) is a human. The
        own-id union replaces an `.add(user_id)` mutation of the caller's set. With a
        bot token that id is the BOT's, which is why an operator has to list their own
        U… id (or `*`) for their DMs to get through.
        """
        pinned = self._pinned_allowed_user_ids
        base = (
            pinned
            if pinned is not None
            else {sid for sid in self._configured_senders() if not sid.startswith("B")}
        )
        return base | {self._user_id}

    @property
    def _allow_all_senders(self) -> bool:
        return "*" in self._allowed_user_ids

    @property
    def _blocked_senders(self) -> set[str]:
        """Blocks both humans (U…) and bots (B…) — a single sender denylist."""
        return self._senders("SLACK_BLOCKED_SENDER_IDS", self._pinned_blocked_senders)

    @property
    def _allowed_bot_ids(self) -> set[str]:
        """Bot senders that bypass the @mention gate, by `B…` prefix."""
        pinned = self._pinned_allowed_bot_ids
        if pinned is not None:
            return pinned
        return {sid for sid in self._configured_senders() if sid.startswith("B")}

    @property
    def _silent_sender_ids(self) -> set[str]:
        return self._senders("SLACK_SILENT_SENDER_IDS", self._pinned_silent_sender_ids)

    def set_orchestrator(self, orchestrator: object) -> None:
        from claude_on_the_fly.orchestrator import Orchestrator

        if not isinstance(orchestrator, Orchestrator):
            raise TypeError(f"Expected Orchestrator, got {type(orchestrator)}")
        self._orchestrator = orchestrator

    def workspace_name(self, chat_id: int) -> str:
        return f"slack/{self._workspace_names.get(chat_id, str(chat_id))}"

    def sender_name(self, chat_id: int) -> str:
        return self._sender_names.get(chat_id, "unknown")

    def sender_identity(self, chat_id: int) -> str:
        """Stable Slack user id used for prompt/memory routing."""
        return self._session_sender_ids.get(chat_id, str(chat_id))

    def channel_context(self, chat_id: int) -> str:
        return self._channel_contexts.get(chat_id, "dm")

    def persona_source(self, chat_id: int) -> Path | None:
        """The `slack.personas` file for this thread's channel, or None.

        Candidates are built per call, not cached with the rest of the session
        metadata: a DM's persona may be keyed on the sender, and that id is not
        always known at the moment the workspace name is (a message with no `user`
        field resolves the session without one). Reading it live means the persona
        lands as soon as the id does, and an edited config takes effect on the next
        message.
        """
        entry = self._sessions.get(chat_id)
        channel = entry[0] if entry else ""
        name = self._channel_names.get(chat_id, "")
        if name:
            keys = (channel, name)
        else:
            # DM or group DM. Its channel id is stable per conversation, so it can
            # be keyed directly; the sender id is what makes one person's DMs
            # separable from everyone else's.
            keys = (channel, self._session_sender_ids.get(chat_id, ""), "dm")
        return persona_for("slack", tuple(key for key in keys if key))

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
        """Drop the least-recently-active threads once over the session cap.

        _sessions is an OrderedDict moved-to-end on every message, so the front
        is the oldest by last activity. Active threads (in-flight, or replied to
        recently) sit at the back and are never the eviction candidate.
        """
        while len(self._sessions) > session_cap():
            oldest_id = next(iter(self._sessions))
            self._forget_session(oldest_id)
            logger.debug("evicted stale session %s", oldest_id)

    def _remember_session(
        self, session_id: int, channel: str, thread_ts: str | None
    ) -> None:
        """Mark a thread as the most recently active, evicting past the cap.

        Every entry point that routes a turn goes through here, so a thread the
        cap evicted (or a process restart dropped) re-hydrates on next contact
        instead of being unreachable.
        """
        self._sessions[session_id] = (channel, thread_ts)
        self._sessions.move_to_end(session_id)
        self._evict_stale_sessions()

    def route_for(self, chat_id: int) -> dict:
        """What a pending turn has to be journaled with to be picked back up.

        Two things, and they are different. The thread (`channel`, `thread_ts`) is
        where the *reply* goes: `_session_key` is a one-way hash of that pair, so
        the chat id alone cannot address it again. `message_ts` is the message that
        *asked*, which is where the reactions go -- without it a resumed turn runs
        with no hourglass and no eyes, and the person cannot tell it came back.

        Empty when the session is unknown, which makes the turn unroutable and is
        why `restore_route` treats it as such.
        """
        entry = self._sessions.get(chat_id)
        if entry is None:
            return {}
        channel, thread_ts = entry
        route: dict = {"channel": channel, "thread_ts": thread_ts}
        pending = self._pending_msg.get(chat_id)
        if pending:
            # The newest entry is this turn's: the frontend appends it before
            # dispatching, so `route_for` runs after that append.
            route["message_ts"] = pending[-1][1]
        return route

    def restore_route(self, chat_id: int, route: dict) -> None:
        """Re-register a journaled turn so a replay behaves like a fresh message.

        The same move `_handle_suggestion_tap` makes for a button pressed after a
        restart: these tables are in memory and empty now, so what they held has
        to come back from whatever carried it. A route without a channel is dropped
        rather than registered as a half-session that `send` would fail on.

        Restoring `_pending_msg` is what makes the resume visible in the only place
        it should be. `notify_start` pops from it to swap the hourglass for eyes,
        and `notify_complete` clears them when the reply lands, so a resumed turn
        gets the same reaction lifecycle as any other and needs no announcement.
        """
        channel = route.get("channel")
        if not isinstance(channel, str) or not channel:
            logger.warning(
                "slack: pending turn for chat_id=%s has no channel, cannot restore",
                chat_id,
            )
            return
        thread_ts = route.get("thread_ts")
        self._remember_session(
            chat_id, channel, thread_ts if isinstance(thread_ts, str) else None
        )
        message_ts = route.get("message_ts")
        if isinstance(message_ts, str) and message_ts:
            # Appended in replay order, because notify_start pops from the left.
            self._pending_msg.setdefault(chat_id, deque()).append((channel, message_ts))

    def _forget_session(self, session_id: int) -> None:
        """Drop every per-session dict entry for one thread. All of this state
        is reconstructable, so a re-hydrating thread just re-resolves it."""
        self._sessions.pop(session_id, None)
        self._workspace_names.pop(session_id, None)
        self._sender_names.pop(session_id, None)
        self._channel_contexts.pop(session_id, None)
        self._channel_names.pop(session_id, None)
        self._session_sender_ids.pop(session_id, None)
        self._reply_counts.pop(session_id, None)
        self._cancel_gate_notice(session_id)
        self._cancel_mention_notice(session_id)
        self._mention_hinted.discard(session_id)
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

        # Suggestion buttons render on every reply regardless of token kind,
        # and block_actions payloads reach both installs, so their handler is
        # registered unconditionally. The slash command + skill picker are an
        # app interaction only a bot-token install receives; a user token
        # never sees them, so registering would be dead weight.
        @self._app.action(re.compile(r"^cotf-sugg:"))
        async def handle_suggestion_action(ack, body):
            await ack()
            await self._on_suggestion_action(body)

        if self._is_bot_token:
            self._register_app_interactions()
            self._warm_task = asyncio.create_task(self._warm_skills())

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._handler.start_async()
        logger.info("Slack connected via Socket Mode (user_id=%s)", self._user_id)

    def _register_app_interactions(self) -> None:
        """Register the bot-token-only surface: picker, shortcut, and (when
        slack.slash_command is set) the slash command.

        The view and shortcut callback ids are app-scoped, so they can't collide
        with another install and register unconditionally. The slash command is
        workspace-global and therefore opt-in."""
        app = self._app

        command = slash_command()
        if command:

            @app.command(command)
            async def handle_command(ack, command, body, respond):
                await self._handle_slash_command(ack, command, body, respond)

        @app.view("cof_picker")
        async def handle_picker_submit(ack, view):
            await self._handle_picker_submit(ack, view)

        @app.shortcut("cof_run_skill")
        async def handle_run_skill_shortcut(ack, shortcut):
            await self._handle_run_skill_shortcut(ack, shortcut)

        @app.action(re.compile(r"^cotf-(grant|deny)$"))
        async def handle_approval_action(ack, body):
            await ack()
            await self._on_approval_action(body)

        logger.info(
            "slack: skill picker registered (slash command: %s)",
            command or "off, use the message shortcut",
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

    # --- Runtime permission approvals ---

    def _approval_target(self, chat_id: int | None) -> tuple[str, str | None] | None:
        """(channel, thread_ts) for an approval prompt, or None if nowhere to ask.

        The session's own thread, or nothing. There is deliberately no configured
        fallback channel: an approval is an interactive act, so work with no
        conversation behind it (cron, the job queue) has nobody to ask and denies
        instead. That keeps an unattended job from acquiring network access it
        was never granted, which a fallback channel would quietly allow.

        Landing in a shared channel is not a privilege leak: _on_approval_action
        re-checks the clicker against the allowed senders, so bystanders can read
        the prompt but cannot answer it.
        """
        if chat_id is None:
            return None
        session = self._sessions.get(chat_id)
        if session is None:
            return None
        channel, thread_ts = session
        return channel, thread_ts

    async def ask_approval(
        self, request: ApprovalRequest, chat_id: int | None = None
    ) -> bool:
        """Post Block Kit approve/deny buttons and wait for a click.

        Requires a bot token: interaction payloads only reach a bot-token
        install, so a user-token deployment has no way to receive the click and
        denies instead of hanging until the caller's timeout.
        """
        target = self._approval_target(chat_id)
        if target is None or not self._is_bot_token:
            logger.warning(
                "slack: approval denied (target=%s, bot_token=%s)",
                target,
                self._is_bot_token,
            )
            return False
        channel, thread_ts = target
        nonce = uuid4().hex[:12]
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_approvals[nonce] = future
        posted = await self._post_approval(channel, thread_ts, request, nonce)
        granted = False
        try:
            granted = await future
        finally:
            # Also reached when the caller times out and cancels, so a stale
            # nonce cannot linger and be answered later. The prompt is retired in
            # here for the same reason: on the timeout path the cancellation used
            # to skip it, leaving a spent card with live-looking buttons in the
            # thread forever.
            self._pending_approvals.pop(nonce, None)
            try:
                await self._retire_approval(channel, posted["ts"], request, granted)
            except Exception:
                # Never let a cosmetic edit change the answer, but never lose it
                # either: `_retire_approval` already logs the Slack errors it
                # expects, so anything arriving here is a surprise and the one
                # place it would be visible is this log line.
                logger.exception("slack: retiring the approval prompt failed")
        return granted

    async def _post_approval(
        self,
        channel: str,
        thread_ts: str | None,
        request: ApprovalRequest,
        nonce: str,
    ) -> AsyncSlackResponse:
        """Post the approve/deny card. Pops the nonce if the post never lands."""
        try:
            return await self._post_approval_message(channel, thread_ts, request, nonce)
        except Exception:
            # Nothing will ever answer a card that was not posted, so the entry
            # would sit in _pending_approvals for the life of the daemon.
            self._pending_approvals.pop(nonce, None)
            raise

    async def _post_approval_message(
        self,
        channel: str,
        thread_ts: str | None,
        request: ApprovalRequest,
        nonce: str,
    ) -> AsyncSlackResponse:
        return await self._app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            # The detail names the host the agent asked for, so an unfurl would
            # have Slack fetch the very destination being gated, before any
            # decision is made. It also pushes the buttons off the first screen
            # on mobile, which is where these get answered.
            unfurl_links=False,
            unfurl_media=False,
            text=f"Permission request: {request.subject}",
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _approval_headline(request)},
                },
                {
                    # plain_text, so Slack parses no mrkdwn in it. Parts of a
                    # detail are agent-reachable -- a broker route-scope request
                    # carries the path tail the agent asked for -- and mrkdwn here
                    # would let the agent style the operator's own prompt, hiding
                    # the real subject behind formatting or a fake verdict line.
                    "type": "section",
                    "text": {"type": "plain_text", "text": _fit_block(request.detail)},
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": _approval_footer(request)}],
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "style": "primary",
                            "action_id": "cotf-grant",
                            "value": nonce,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Deny"},
                            "style": "danger",
                            "action_id": "cotf-deny",
                            "value": nonce,
                        },
                    ],
                },
            ],
        )

    async def _retire_approval(
        self, channel: str, ts: str, request: ApprovalRequest, granted: bool
    ) -> None:
        """Collapse the prompt to a one-line record of the decision.

        Editing rather than deleting keeps the decision visible in the thread
        where it was made. A `context` block rather than a `section` is what
        stops it reading as a second reply: Slack renders context in small grey
        text, so a spent card sits next to the answer as a status line instead of
        competing with it. The buttons go, so the prompt cannot be reused.
        """
        verdict = "approved" if granted else "denied"
        # The scope, not the subject. A subject is the grant key, scoped to the
        # program, so this line used to read "Permission approved: bash:chmod" -- a
        # record of a decision that does not say what was decided. The scope carries
        # the arguments.
        decided = _decided_text(request)
        try:
            await self._app.client.chat_update(
                channel=channel,
                ts=ts,
                text=f"Permission {verdict}: {decided}",
                blocks=[
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": (
                                    f"Permission *{verdict}*: `{_literal(decided)}`"
                                ),
                            }
                        ],
                    }
                ],
            )
        except SlackApiError as exc:
            logger.warning("slack: could not retire approval prompt: %s", exc)

    async def _on_approval_action(self, body: dict) -> None:
        """Resolve a pending approval from a button click."""
        actions = body.get("actions") or []
        if not actions:
            return
        sender_id = (body.get("user") or {}).get("id", "")
        # Only an allowed sender may widen policy. Anyone else who can see the
        # message in the channel must not be able to grant the agent a host.
        if not self._is_allowed(sender_id):
            logger.warning("slack: ignoring approval click from %s", sender_id)
            return
        action = actions[0]
        nonce = action.get("value", "")
        future = self._pending_approvals.get(nonce)
        if future is None or future.done():
            logger.info("slack: approval click for unknown or settled nonce %s", nonce)
            return
        future.set_result(action.get("action_id") == "cotf-grant")

    async def _on_suggestion_action(self, body: dict) -> None:
        """Send a suggestion label from a button tap as the next message.

        Same dispatch gate as a typed message: the clicker must be an allowed
        sender, because the button lands in a thread anyone in the channel can
        see.
        """
        actions = body.get("actions") or []
        if not actions:
            return
        sender_id = (body.get("user") or {}).get("id", "")
        if not self._is_allowed(sender_id):
            logger.warning("slack: ignoring suggestion click from %s", sender_id)
            return
        # The label comes straight from the clicked button's payload: it is
        # the label we rendered, so no per-message state and no index
        # round-trip is needed.
        label = (actions[0].get("text") or {}).get("text", "")
        if not label:
            logger.info("slack: suggestion click without a label, dropping")
            return
        if self._on_message is None:
            logger.warning("slack: suggestion click with no message handler wired")
            return
        chat_id = self._session_id_for(body)
        channel = (body.get("channel") or {}).get("id", "")
        # Slack gives a clicked button no selected state, so the menu is
        # retired on any tap: the whole actions block collapses to a status
        # line, so nothing on it can be clicked twice, and the ✓ names the
        # pick for anyone scrolling back. The retire is cosmetic and can fail
        # (a transient API error, a dropped scope), so the spent set below is
        # what actually enforces one tap, one message.
        ts = (body.get("message") or {}).get("ts", "")
        if channel and ts:
            key = (channel, ts)
            if key in self._spent_menus:
                logger.info("slack: suggestion on %s already spent, dropping", key)
                return
            self._spent_menus[key] = None
            if len(self._spent_menus) > SUGGESTION_SPENT_CAP:
                self._spent_menus.pop(next(iter(self._spent_menus)))
        await self._retire_suggestion_menu(body, label)
        # A tap can arrive after a restart or a cap eviction emptied _sessions,
        # so the thread is registered here exactly as a typed message registers
        # it. Without this, send() has no route for the reply.
        self._remember_session(
            chat_id, channel, (body.get("message") or {}).get("thread_ts")
        )
        # Mirror the typed-message bookkeeping: one (channel, ts) and one
        # suppressed flag per dispatched turn, so notify_start pairs this
        # turn's reactions with its own message instead of the next one's.
        self._pending_msg.setdefault(chat_id, deque()).append((channel, ts))
        self._pending_reply_suppressed.setdefault(chat_id, deque()).append(False)
        display = (body.get("user") or {}).get("name", "")
        # Same sender marker a typed message carries, so the agent sees who
        # pressed the button.
        await self._on_message(chat_id, f"{sender_marker(sender_id, display)} {label}")

    async def _retire_suggestion_menu(self, body: dict, label: str) -> None:
        """Rewrite the clicked message, collapsing the menu to a status line."""
        message = body.get("message") or {}
        channel = (body.get("channel") or {}).get("id", "")
        ts = message.get("ts", "")
        blocks = message.get("blocks")
        if not (channel and ts and blocks):
            return
        try:
            await self._app.client.chat_update(
                channel=channel,
                ts=ts,
                text=message.get("text", ""),
                blocks=_retire_suggestion_block(blocks, label),
            )
        except Exception as exc:
            # The label is routed regardless; only the cosmetic mark failed,
            # and the spent set keeps a re-tap from dispatching twice.
            logger.warning("slack: could not retire suggestion menu: %s", exc)

    def _session_id_for(self, body: dict) -> int:
        """The chat whose thread a button lives in.

        The button was rendered on a reply that `send` posted at the session's
        (channel, thread_ts), and _session_key is a pure function of that pair,
        so the id is derived from the tap itself. Deriving rather than looking up
        a live session is what makes a button outlive the process that drew it: a
        restart (or a session-cap eviction) empties _sessions, and a scan there
        would find nothing and silently drop the tap.
        """
        channel = (body.get("channel") or {}).get("id", "")
        thread = (body.get("message") or {}).get("thread_ts")
        return _session_key(channel, thread)

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
        logger.info(
            "slack: slash command text=%s channel=%s", logs.redact(text), channel
        )
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
        self._remember_session(session_id, channel, thread_ts)
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
        isn't in. Cache definitive answers briefly; unexpected API failures fail
        closed and are not cached.
        """
        cached = self._own_dm.get(channel)
        now = time.monotonic()
        if cached is not None and cached[1] > now:
            return cached[0]
        result = False
        try:
            info = await self._app.client.conversations_info(channel=channel)
            ch = info["channel"]
            result = bool(ch.get("is_im") or ch.get("is_mpim"))
        except SlackApiError as exc:
            if exc.response.get("error") in ("channel_not_found", "not_in_channel"):
                result = False
            else:
                logger.warning("is_bot_conversation: %s: %s", channel, exc)
                return False
        except Exception as exc:
            logger.warning("is_bot_conversation: %s: %s", channel, exc)
            return False
        self._own_dm[channel] = (result, now + self._own_dm_ttl)
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
            if channel_type in TAG_REQUIRED_CHANNEL_TYPES:
                mention = f"<@{self._user_id}>"
                if mention not in text:
                    logger.debug("skipped: no mention of %s in text", self._user_id)
                    await self._hint_mention_required(channel, thread_ts, sender_id)
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
        self._remember_session(session_id, channel, thread_ts)
        # They tagged the bot, so they know how this works: drop any pending
        # "you need to tag me" notice rather than lecturing them about a mistake
        # they have already corrected.
        self._cancel_mention_notice(session_id)
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

        # $compact summarizes the thread's history in place. Queued as a turn
        # rather than answered here, so it gets the same reaction and live status
        # a reply would — a compaction is a couple of minutes of silence on a
        # large thread, and silence is indistinguishable from a hung daemon.
        # Ahead of the soft-limit gate for the same reason as $stop: a thread
        # over budget is the one that most needs this to still work.
        if text.strip() == COMPACT_COMMAND:
            logger.info("slack %s/%s: %s", channel, thread_ts, COMPACT_COMMAND)
            # This branch returns before the normal path's catch-up bookkeeping,
            # so mirror it here or a reconnect re-ingests the trigger and
            # compacts a second time.
            self._processed_ts.append(ts)
            self._active_channels[channel] = ts
            if channel_type:
                self._channel_types[channel] = channel_type
            if self._orchestrator is None:
                await self._post_notice(
                    channel, thread_ts, "Not connected to a session yet."
                )
                return
            # Unlike $stop, a compaction needs to know *which workspace* this
            # thread's session lives in, and that name is only resolved here. The
            # normal path does this further down, past the point this branch
            # returns from — skipping it pointed the compaction at
            # `slack/<session_key>`, so every thread looked brand new.
            await self._identify_session(
                session_id,
                event,
                channel,
                channel_type,
                thread_ts,
                bot_id,
                is_trusted_bot,
            )
            self._pending_msg.setdefault(session_id, deque()).append((channel, ts))
            self._pending_reply_suppressed.setdefault(session_id, deque()).append(False)
            await self._orchestrator.on_compact(session_id)
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
                # A bare trigger is somebody asking what is already running,
                # which is the one moment the answer is worth more than the
                # usage line — so show both.
                await self._post_notice(
                    channel,
                    thread_ts,
                    _render_job_list(
                        self._read_unfinished_jobs(), channel, job_command
                    ),
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
            # Promise a reply only if something can produce one. With the
            # trigger on by default, an install that never started the worker
            # would otherwise get "I'll reply here when it's done" on a job that
            # nothing will ever run — a failure that looks exactly like success.
            if live_pid("jobs") is None:
                notice = (
                    f"Queued job `{job.id}`, but no worker is running — it will "
                    "stay queued until one starts (`claude-tui start jobs`)."
                )
            else:
                notice = f"Queued job `{job.id}` — I'll reply here when it's done."
            await self._post_notice(channel, thread_ts, notice)
            return

        # Reply soft-limit gate. CONTINUE_COMMAND resets the counter and any
        # trailing text is processed as the next turn; otherwise, once the
        # thread is over budget we post the warning and stop here (no agent run).
        stripped = text.strip()
        if stripped == CONTINUE_COMMAND or stripped.startswith(CONTINUE_COMMAND + " "):
            self._reply_counts[session_id] = 0
            # A notice still waiting out its delay would land after the thread
            # has already resumed, telling the user to send what they just sent.
            self._cancel_gate_notice(session_id)
            text = stripped[len(CONTINUE_COMMAND) :].strip()
            logger.info(
                "slack %s/%s: reply count reset via continue", channel, thread_ts
            )
        elif self._reply_counts.get(session_id, 0) >= reply_soft_limit():
            logger.info(
                "slack %s/%s: reply soft-limit %d reached, gating message",
                channel,
                thread_ts,
                reply_soft_limit(),
            )
            self._schedule_reply_limit_warning(
                session_id, channel, thread_ts, sender_id
            )
            return

        thread_context = ""
        if is_new_session and is_mid_thread:
            thread_context = await self._fetch_thread_context(channel, thread_ts, ts)

        sender = await self._identify_session(
            session_id, event, channel, channel_type, thread_ts, bot_id, is_trusted_bot
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
            segments.append(f"{sender_marker(sender_id, sender)} {cover}")
        elif forwards:
            segments.append(sender_marker(sender_id, sender))
        final_text = "\n\n".join(segments)

        self._pending_msg.setdefault(session_id, deque()).append((channel, ts))
        silent = bool(bot_id and bot_id in self._silent_sender_ids) or (
            sender_id in self._silent_sender_ids
        )
        self._pending_reply_suppressed.setdefault(session_id, deque()).append(silent)
        preview = text[:80] if text else "(forward only)"
        fwd_marker = f" (+{len(forwards)} fwd)" if forwards else ""
        logger.info(
            "slack %s/%s: %s %s%s",
            channel,
            thread_ts,
            sender_marker(sender_id, sender),
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
        for session_id in list(self._gate_notices):
            self._cancel_gate_notice(session_id)
        for session_id in list(self._mention_notices):
            self._cancel_mention_notice(session_id)
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
        logger.info("slack %s/%s => %s", channel, thread_ts, logs.redact(response.body))

        blocks = _build_response_blocks(response.body, response)
        # Suggestions parsed out of the reply; empty means no buttons.
        labels = response.suggestions
        if labels:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": label},
                            "action_id": f"cotf-sugg:{i}",
                        }
                        for i, label in enumerate(labels)
                    ],
                }
            )

        try:
            resp = await self._app.client.chat_postMessage(
                channel=channel,
                text=response.body,
                blocks=blocks,
                thread_ts=thread_ts,
                # A reply's links are for the user to click, not for Slack to
                # fetch; an unfurl adds noise and pushes buttons off-screen.
                unfurl_links=False,
                unfurl_media=False,
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
                data = await asyncio.to_thread(read_attachment, path)
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

    def _schedule_reply_limit_warning(
        self,
        session_id: int,
        channel: str,
        thread_ts: str | None,
        sender_id: str,
    ) -> None:
        """Queue the gate notice, held back by `reply_limit_notice_seconds`.

        Deliberately not awaited by the caller: the message is already gated, and
        sitting on the delay inside `_ingest_event` would stall the events behind
        it (`_catchup` ingests serially).

        Each further gated message restarts the wait (debounce), so the quiet gap
        falls after the sender stopped typing instead of in the middle of their
        burst — a notice that lands mid-burst is read on arrival, which is the
        whole failure this delay exists to avoid. `REPLY_LIMIT_NOTICE_MAX_HOLD`
        keeps a fast talker from deferring it forever: the ceiling is set by the
        *first* gated message and survives every reschedule.
        """
        deadline = self._gate_deadlines.setdefault(
            session_id, time.monotonic() + REPLY_LIMIT_NOTICE_MAX_HOLD
        )
        self._drop_gate_task(session_id)
        self._gate_notices[session_id] = asyncio.create_task(
            self._warn_reply_limit_later(
                session_id, channel, thread_ts, sender_id, deadline
            )
        )

    def _drop_gate_task(self, session_id: int) -> None:
        """Cancel a pending notice, leaving its ceiling in place for a reschedule."""
        task = self._gate_notices.pop(session_id, None)
        if task is not None:
            task.cancel()

    def _cancel_gate_notice(self, session_id: int) -> None:
        """Drop a pending notice and forget the thread's ceiling with it."""
        self._drop_gate_task(session_id)
        self._gate_deadlines.pop(session_id, None)

    async def _warn_reply_limit_later(
        self,
        session_id: int,
        channel: str,
        thread_ts: str | None,
        sender_id: str,
        deadline: float,
    ) -> None:
        try:
            hold = min(
                reply_limit_notice_seconds(),
                max(0.0, deadline - time.monotonic()),
            )
            await asyncio.sleep(hold)
        finally:
            # Deregister before posting, not after: while this task is still the
            # thread's notice, the next gated message cancels it, and a cancel
            # landing inside `chat_postMessage` aborts the request with the
            # notice half-sent. Only clean up if this task is still the thread's
            # notice — a debounce cancels us *after* the replacement is stored,
            # and popping blind would throw away that reschedule.
            if self._gate_notices.get(session_id) is asyncio.current_task():
                self._gate_notices.pop(session_id, None)
                self._gate_deadlines.pop(session_id, None)
        await self._warn_reply_limit(channel, thread_ts, sender_id)

    async def _hint_mention_required(
        self, channel: str, thread_ts: str | None, sender_id: str
    ) -> None:
        """Say once, in a thread the bot is already in, that a channel message
        without a tag is invisible to it.

        Scoped to threads with a live session because those are the ones where
        somebody is talking *to* the bot and the missing tag is a slip. Without
        that check this fires on ordinary channel chatter the bot was never part
        of. Called from inside the channel/group branch, so a DM (where no tag is
        needed) can never reach it.

        Opt-in (`slack.mention_notice_seconds`, off by default) and, when on, held
        for that many seconds rather than posted now. Each further untagged
        message restarts the wait: while they are still typing they are also
        still watching, and a notice read on arrival is one they never register.
        While it is off nothing is scheduled and no per-thread state is recorded. A tagged message cancels it outright -- somebody who got
        it right does not need telling -- so this only reaches the person who
        forgot *and* walked away, which is exactly who comes back to an unread
        thread.
        """
        delay = mention_notice_seconds()
        if delay <= 0:
            return
        session_id = _session_key(channel, thread_ts)
        if session_id not in self._sessions or session_id in self._mention_hinted:
            return
        self._cancel_mention_notice(session_id)
        self._mention_notices[session_id] = asyncio.create_task(
            self._notice_mention_later(session_id, channel, thread_ts, sender_id, delay)
        )

    def _cancel_mention_notice(self, session_id: int) -> None:
        """Drop a notice that has not fired yet."""
        task = self._mention_notices.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def _notice_mention_later(
        self,
        session_id: int,
        channel: str,
        thread_ts: str | None,
        sender_id: str,
        delay: float,
    ) -> None:
        # Read at schedule time and carried in, so switching the notice off does
        # not leave a task that wakes minutes later and posts anyway.
        try:
            await asyncio.sleep(delay)
            self._mention_hinted.add(session_id)
        finally:
            # Deregister before posting, for the same reason the gate notice
            # does: a cancel landing inside `chat_postMessage` aborts a half-sent
            # request. Only if this task is still the thread's notice — a restart
            # cancels us *after* the replacement is stored.
            if self._mention_notices.get(session_id) is asyncio.current_task():
                self._mention_notices.pop(session_id, None)
        logger.info("slack %s/%s: told the thread it needs a tag", channel, thread_ts)
        await self._post_notice(
            channel,
            thread_ts,
            f"<@{sender_id}> I only see messages in a channel that tag me. "
            f"Add <@{self._user_id}> and I'll pick it up.",
        )

    async def _warn_reply_limit(
        self, channel: str, thread_ts: str | None, sender_id: str = ""
    ) -> None:
        """Tell the user the thread hit the reply soft-limit and how to resume.

        Addressed to the sender when there is one, so it arrives as a real
        notification rather than a thread reply Slack can mark read on arrival.
        A trusted bot has no user id, hence the plain form.
        """
        who = f"<@{sender_id}> " if sender_id else ""
        note = (
            f"{who}Hit the {reply_soft_limit()}-message limit for this thread. "
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

    def _read_unfinished_jobs(self) -> list[QueueRow]:
        """What is queued or running, or [] if the queue cannot say.

        A listing is a courtesy: the queue lives on a filesystem (or, for
        another adapter, across a network) that a chat turn has no business
        failing over, so a read error degrades to "nothing to show" and is
        logged. The limit is asked for before filtering, so a busy queue can
        still fill a channel's page.
        """
        if self._job_queue is None:
            return []
        try:
            return self._job_queue.list_unfinished(JOB_LIST_LIMIT * 4)
        except Exception as exc:
            logger.warning("slack: could not read the job queue: %s", exc)
            return []

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

    async def send_progress(self, chat_id: int, text: str) -> None:
        """Post one mid-turn progress message into the thread the turn runs in.

        Its own `chat_postMessage` rather than `send()`, for two reasons that are
        both about not spending the user's allowance: `send` counts a reply against
        `slack.reply_soft_limit` (in both its arms, `send` and `_fallback_dm`), and
        progress must not burn a ten-message budget; and a progress line is a
        context block, not a reply with a stats footer.

        DMs and group DMs only. That exemption from the reply budget is exactly why
        — in a channel, nothing at all would cap how much narration a heavy turn
        pushes at people who are not in the conversation.

        Records the ts for the same reason every other post here does — under a
        user token our own message comes back as an event, and `_catchup` re-reads
        it after a reconnect without passing Bolt's self-event filter at all.
        """
        route = self._sessions.get(chat_id)
        if not route:
            logger.debug("progress: no channel for session %s", chat_id)
            return
        channel, thread_ts = route
        if self._in_flight_reply_suppressed.get(chat_id, False):
            logger.debug("progress: omitted for silenced sender in %s", channel)
            return
        # Async and last of the three guards, so a cheap sync check never pays for
        # a lookup. `_channel_type` is cache-first and returns "" when
        # conversations_info fails — "" is not in the set, so a failed lookup
        # means no post. Fail-closed on purpose: a missed progress line costs one
        # silent turn, a wrong one puts narration in a team channel.
        if await self._channel_type(channel) not in _INTERIM_CHANNEL_TYPES:
            logger.debug("progress: omitted outside a DM/group DM in %s", channel)
            return
        logger.info("slack %s/%s ~> %s", channel, thread_ts, logs.redact(text))
        rendered = f"{INTERIM_PREFIX} {_fit_block(to_mrkdwn(text))}"
        try:
            resp = await self._app.client.chat_postMessage(
                channel=channel,
                text=rendered,
                blocks=[
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": rendered}],
                    }
                ],
                thread_ts=thread_ts,
            )
        except Exception as exc:
            logger.error("progress: failed to post: %s", exc)
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
        await self._react(channel, ts, QUEUED_EMOJI)

    async def notify_interrupted(
        self, chat_id: int, *, running: bool, queued: int
    ) -> None:
        """Mark the affected messages as interrupted, and say nothing.

        A stop no longer costs anybody an answer: every pending turn is journaled
        and resumes on the next start. So prose here would be the daemon narrating
        its own lifecycle, and it would do it once per stop -- twice if you press
        `r`, which is a stop and a start.

        The reaction says it in the vocabulary this thread already speaks, with its
        own glyph: `QUEUED_EMOJI` already means "waiting behind other work", so
        reusing it here would make a restart indistinguishable from a queue. The
        resume clears this one and moves the message to `RUNNING_EMOJI` by itself.

        A turn with no message behind it (a slash command, the picker) has nothing
        to react to, and falls back to the base class's line.
        """
        targets: list[tuple[str, str]] = []
        in_flight = self._in_flight.get(chat_id)
        if in_flight is not None:
            targets.append(in_flight)
        targets.extend(self._pending_msg.get(chat_id) or ())
        if not targets:
            await super().notify_interrupted(chat_id, running=running, queued=queued)
            return
        for channel, ts in targets:
            await self._unreact(channel, ts, RUNNING_EMOJI)
            await self._react(channel, ts, INTERRUPTED_EMOJI)

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
        # Both waiting marks, because a turn reaching here may have been queued
        # behind other work, interrupted by a restart, or both in either order.
        await self._unreact(channel, ts, QUEUED_EMOJI)
        await self._unreact(channel, ts, INTERRUPTED_EMOJI)
        await self._react(channel, ts, RUNNING_EMOJI)
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
        await self._unreact(channel, ts, RUNNING_EMOJI)

    # --- Helpers ---

    async def _react(self, channel: str, timestamp: str, emoji: str) -> None:
        try:
            await self._app.client.reactions_add(
                channel=channel, timestamp=timestamp, name=emoji
            )
        except Exception as exc:
            # `already_reacted` is the normal case for a resumed turn: the eyes a
            # force-killed daemon left behind are still on the message. Logged at
            # debug so the recovery path is not noisy about working correctly.
            if "already_reacted" in str(exc):
                logger.debug("react: :%s: already on %s", emoji, timestamp)
                return
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
                unfurl_links=False,
                unfurl_media=False,
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
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, headers=headers, allow_redirects=True) as resp,
        ):
            resp.raise_for_status()
            content_type = resp.content_type or ""
            if content_type.startswith("text/html"):
                raise RuntimeError(
                    f"got HTML instead of file data (likely auth issue): {url}"
                )
            data = await resp.content.read(MAX_ATTACHMENT_BYTES + 1)
            if not data:
                raise RuntimeError(f"empty response body: {url}")
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise ValueError(
                    f"download exceeds {MAX_ATTACHMENT_BYTES} bytes: {dest.name}"
                )
            write_attachment(dest, data)
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

    async def _identify_session(
        self,
        session_id: int,
        event: dict,
        channel: str,
        channel_type: str,
        thread_ts: str,
        bot_id: str,
        is_trusted_bot: bool,
    ) -> str:
        """Resolve and cache this session's sender + workspace name. Returns the
        sender.

        `workspace_name()` reads `_workspace_names`, which only
        `_resolve_session_metadata` fills in — so anything that needs the
        workspace has to come through here first. A control prefix that answers
        without it gets `slack/<session_key>`, a directory no session was ever
        created under, which is how `$compact` came to report "no session yet" on
        threads days deep in conversation.
        """
        if is_trusted_bot:
            sender = event.get("username") or bot_id or "bot"
        else:
            sender = await self._resolve_sender(event.get("user", "unknown"))
        self._sender_names[session_id] = sender
        await self._resolve_session_metadata(
            session_id, sender, channel, channel_type, thread_ts
        )
        return sender

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

        # The whole thread_ts, fraction included, with the dot swapped for a
        # dash so it reads as one path segment. Truncating to the integer
        # second used to look tidier and silently collided: `_session_key`
        # hashes the full ts, so two messages in the same second are two
        # separate sessions, and both landed on one workspace directory —
        # concurrent agents sharing a cwd, and `_save_files` overwriting or
        # cross-reading the other conversation's attachments. Slack emits
        # sub-second duplicates often enough for this to be routine.
        short_ts = thread_ts.replace(".", "-") if thread_ts else "root"

        if channel_type == "im":
            self._workspace_names[session_id] = f"dm-{sender}-{short_ts}"
            self._channel_contexts[session_id] = "dm (private)"
            self._channel_names[session_id] = ""
            return

        try:
            info = await self._app.client.conversations_info(channel=channel)
            ch = info["channel"]
            name = ch["name"]
        except Exception as exc:
            logger.warning("Failed to resolve channel %s: %s", channel, exc)
            self._workspace_names[session_id] = f"{channel}-{short_ts}"
            self._channel_contexts[session_id] = f"channel:{channel}"
            # Still a channel, just an unnamed one: the id is the only key a
            # persona can match, and falling through to the DM branch would key it
            # on the sender instead.
            self._channel_names[session_id] = channel
            return

        if ch.get("is_mpim"):
            members = await self._resolve_mpim_members(channel)
            self._workspace_names[session_id] = f"{name}-{short_ts}"
            context = f"group-dm (private)\nParticipants: {', '.join(members)}"
            self._channel_contexts[session_id] = context
            self._channel_names[session_id] = ""
        else:
            self._channel_names[session_id] = name
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


def main() -> None:
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
    app_token, token, user_id = run_slack()
    # Sender lists are left unset on purpose: unset means "read the config on every
    # message", which is what makes adding an allowed sender take effect without a
    # restart. preflight has already validated them and refused to start on a broken
    # one.
    frontend = SlackFrontend(app_token=app_token, token=token, user_id=user_id)
    asyncio.run(run(frontend, platform="slack"))


if __name__ == "__main__":  # pragma: no cover
    main()
