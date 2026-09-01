"""Session management, message queuing, and agent execution."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from claude_on_the_fly import (
    agent,
    broker,
    commands,
    cotf_approve,
    egress,
    logs,
    permissions,
    sandbox,
    settings,
    tmux,
    turns,
)
from claude_on_the_fly import approvals as approvals_mod
from claude_on_the_fly.agent import (
    DATA_DIR,
    SUGGESTIONS_BLOCK_RE,
    ClaudeUnavailableError,
    Response,
    current_backend_key,
    strip_suggestions_blocks,
    workspace_path,
)
from claude_on_the_fly.approvals import ApprovalBroker
from claude_on_the_fly.events import (
    EVENT_DISPATCHED,
    EVENT_WORKER_DONE,
    EVENT_WORKER_FAILED,
    EventLog,
)
from claude_on_the_fly.heartbeat import HeartbeatWriter
from claude_on_the_fly.interim import InterimProgress, interim_progress_enabled
from claude_on_the_fly.jobs.orphans import ProcessLedger
from claude_on_the_fly.protocol import Frontend
from claude_on_the_fly.turns import PendingTurn, TurnJournal, new_turn_id

logger = logging.getLogger(__name__)

# Percentage of the model's context window above which the next inbound message
# is preceded by a compaction. Unset or 0 disables it, which is the default:
# compaction is a full-context pass, so firing it unasked costs real money.
#
# Read per-instance rather than at import: `load_dotenv()` runs after this module is
# imported, and the config file can change under a running daemon either way. Only pty
# mode can supply the reading it compares against
# (see `Response.context_tokens`), so in native mode this is inert however it is
# set — the manual trigger is the whole feature there.
AUTO_COMPACT_PCT_VAR = "COTF_AUTO_COMPACT_PCT"

# Appended to every chat turn's prompt when suggestions are enabled: the agent
# ends its reply with the follow-ups it was asked for, wrapped so the daemon
# can strip them and render them as buttons without the user ever seeing the
# machine half of the reply. Appended per turn rather than baked into the
# system prompt so cron and the job queue (which share the agent) never carry
# it, and so an edit takes effect on the next read like any live setting.
SUGGESTIONS_TEMPLATE = (
    "<cotf-suggest>\n"
    "System instruction, not user text. Answer the user first, then ALWAYS "
    "end your reply with the block. The block is mandatory in every reply: a "
    "JSON array of 1 to 3 short follow-up options when the conversation has a "
    "real decision fork, or an empty array when it does not. The block is "
    "the END of your reply, never the whole reply.\n"
    "\n"
    "Offer as many real options as the fork actually has. Never invent one to "
    "reach a count: two real options beat three, and one beats two padded.\n"
    "\n"
    "Put the most concrete executable step first. The first option is the one "
    "you judge most likely.\n"
    "\n"
    'Never offer "wait", "monitor" or "leave it as is" as filler. Offer a '
    "cancel option only when acting would be hard to undo.\n"
    "\n"
    "Example of a complete reply:\n"
    "The Eiffel Tower is in Paris, built for the 1889 World's Fair.\n"
    '<suggestions>["Tell me more about its history", "Show me photos", '
    '"Compare it with other landmarks"]</suggestions>\n'
    "\n"
    "A tapped option is sent back verbatim as the user's next message, so "
    "each option must be a step you can execute with no further input "
    '("Audit the release posts") or a question you can answer by doing work '
    '("What is it doing now?").\n'
    "</cotf-suggest>"
)

# Slack allows at most five buttons per actions block, and button text maxes
# out at 75 characters (the block-kit hard cap; more and chat_postMessage
# rejects the whole block); both caps live here so the frontends and the
# parser agree without importing each other.
MAX_SUGGESTIONS = 5
MAX_SUGGESTION_LENGTH = 75

_SUGGESTIONS_TRUTHY = frozenset({"1", "true", "yes", "on"})

# How long shutdown may spend telling interrupted chats their work died. Sized
# under the supervisor's safe grace (`supervisor.SAFE_GRACE_S`) so the notices
# finish inside the window rather than being cut off by the SIGKILL that ends it.
SHUTDOWN_NOTICE_BUDGET_S = 8.0

# Prepended to a resumed turn whose agent had already started before the daemon
# stopped. The turn is replayed rather than handed back, because somebody asked
# for the work and wants it done; this is what keeps the replay from silently
# repeating a push, a message, or a file write that already happened. Wrapped like
# SUGGESTIONS_TEMPLATE so the machine half never reads as the user's own words,
# and deliberately short on instructions: what "already done" looks like is the
# agent's judgement, not something this can enumerate.
RESUME_TEMPLATE = (
    "<cotf-resume>\n"
    "System note, not user text. A restart interrupted this turn while you were "
    "working on it, and you are picking it up again. Some of it may already be "
    "done. Check the current state before repeating anything that writes, sends, "
    "or publishes, then carry on and answer normally.\n"
    "</cotf-resume>"
)


def _session_tag(value: int | str | None) -> str | None:
    """The session discriminator as journaled text, or None for the base session.

    `_session_counters` holds an int for cron's bumps and a str for a token the
    frontend pinned. Stringifying both keeps one field on disk; 0 and None both
    mean "the base session", which needs nothing restored.
    """
    if not value:
        return None
    return str(value)


def _resume_prompt(entry: PendingTurn) -> str:
    """The text to replay for one pending turn.

    A turn that never reached an agent is replayed verbatim. One that had already
    started carries the resume note, so it can check what it already did instead
    of doing it twice.
    """
    if entry.phase != turns.DISPATCHED:
        return entry.text
    return f"{RESUME_TEMPLATE}\n\n{entry.text}"


def _suggestions_enabled() -> bool:
    """Live gate for the suggestions template. Read per turn, not at import."""
    return settings.get("COTF_SUGGESTIONS_ENABLED").lower() in _SUGGESTIONS_TRUTHY


def _extract_suggestions(body: str) -> tuple[str, list[str]]:
    """Split a reply into (visible text, suggestion labels).

    Every <suggestions> block is stripped from the visible body; the labels
    come from the last one, since the template tells the agent to end the
    reply with it. A reply that was only blocks gets a placeholder so
    frontends never send an empty message, and its labels are dropped —
    buttons with no reply above them are blind taps. The backends nudge a
    block-only reply before it ever reaches here, so this is the last resort.
    """
    matches = list(SUGGESTIONS_BLOCK_RE.finditer(body))
    if not matches:
        return body, []
    cleaned = strip_suggestions_blocks(body)
    if not cleaned:
        # The agent skipped its reply and emitted only the block. Drop the
        # labels too (a button without a reply carries no context) and log
        # so the frequency of the failure stays measurable. The text matches
        # the backends' own empty-reply fallback: naming the buttons here
        # would promise an affordance the line above just deleted.
        logger.warning("suggestions: reply body empty; dropping suggestion labels")
        return "No response", []
    return cleaned, _parse_suggestion_block(matches[-1].group(1))


def _labels_from(data: object) -> list[str]:
    """Suggestion labels out of a parsed block payload (a list of strings)."""
    if not isinstance(data, list):
        return []
    return [
        str(item).strip()[:MAX_SUGGESTION_LENGTH]
        for item in data
        if isinstance(item, str) and item.strip()
    ]


def _parse_suggestion_block(raw: str) -> list[str]:
    """Turn the block's contents into labels: a JSON list first, a
    Python-style literal (single-quoted lists, trailing commas) as a lenient
    second, a markdown list as a third. Anything else yields nothing, and the
    caller shows no buttons for a malformed block rather than stale ones."""
    content = raw.strip()
    if content.startswith("```"):
        # The agent sometimes wraps the block in a code fence despite the
        # template; strip the fence and any "json" language tag.
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content).strip()
    items: list[str] = []
    try:
        items = _labels_from(json.loads(content))
    except json.JSONDecodeError:
        try:
            items = _labels_from(ast.literal_eval(content))
        except (ValueError, SyntaxError):
            for line in content.splitlines():
                match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
                if match:
                    items.append(match.group(1)[:MAX_SUGGESTION_LENGTH])
    return items[:MAX_SUGGESTIONS]


@dataclass(frozen=True)
class Turn:
    """One item of queued work for a chat.

    A compaction is queued as a turn rather than run inline so it inherits the
    whole per-turn lifecycle — the reaction, the live status ticker, `$stop`, and
    the event-log entry — and so it takes its place in FIFO order ahead of the
    message that triggered it.
    """

    text: str
    compact: bool = False
    # The `turns` journal entry this turn was recorded as, so the phase can be
    # advanced and the record dropped once the reply is posted. Empty for a turn
    # nothing journaled (a compaction, which is daemon maintenance rather than
    # somebody's message, and which the auto-compact gate re-queues by itself).
    journal_id: str = ""


class SessionEgress:
    """One CONNECT proxy, with its own grant store, per session.

    Per-session rather than one daemon-wide proxy for two reasons that are really
    the same reason:

    - **Attribution.** A CONNECT carries a hostname and nothing else, so a shared
      proxy cannot tell which of several concurrently running chats made it. The
      port is the only available label, so giving each session its own port is
      what lets the approval prompt land in the conversation that caused it.
    - **Grant scope.** A grant lives on an ApprovalBroker's store. Share the
      broker and approving a host in one chat silently authorizes it for every
      other chat and for cron. One store per session confines it.

    A session is (chat_id, session_uuid), so `/new` earns a fresh proxy and drops
    the previous session's grants rather than inheriting them.
    """

    def __init__(self, frontend: Frontend) -> None:
        self._frontend = frontend
        self._proxies: dict[int, tuple[str, egress.EgressProxy]] = {}

    async def env_for(self, chat_id: int, session: str) -> dict[str, str]:
        """Proxy env for this session, starting a proxy the first time."""
        existing = self._proxies.get(chat_id)
        if existing is not None and existing[0] == session:
            return existing[1].proxy_env()
        if existing is not None:
            # Session changed under this chat: the old grants died with it.
            await existing[1].stop()
            logger.info("egress: chat %s session changed, grants dropped", chat_id)
        # Both the proxy and its grant store carry the chat label, so every gate
        # decision in the log names the conversation it belongs to. Without it two
        # concurrent chats reaching the same host are indistinguishable, and the
        # per-session confinement this class exists for cannot be verified.
        label = f"chat {chat_id}"
        approvals = ApprovalBroker(
            approvals_mod.gate_from_frontend(self._frontend, chat_id),
            policy=approvals_mod.ApprovalPolicy(never_ask=egress.never_ask_subjects()),
            label=label,
        )
        proxy = egress.EgressProxy(approvals, label=label)
        await proxy.start()
        self._proxies[chat_id] = (session, proxy)
        logger.info(
            "egress: chat %s -> 127.0.0.1:%d (own grant store, session %s)",
            chat_id,
            proxy.port,
            session[:8],
        )
        return proxy.proxy_env()

    async def close_all(self) -> None:
        """Revoke every session's egress at once."""
        for _session, proxy in list(self._proxies.values()):
            await proxy.stop()
        self._proxies.clear()


class SessionPermissions:
    """One approval service, with its own grant store, per session.

    Mirrors SessionEgress, for the same two reasons: a grant must not leak into
    another chat, and the prompt has to land in the conversation that caused it.
    Separate from SessionEgress rather than folded into it because a deployment can
    run either without the other -- egress gating is COTF_SANDBOX, tool approvals
    are `permissions.mode`, and neither implies the other.
    """

    def __init__(self, frontend: Frontend) -> None:
        self._frontend = frontend
        self._services: dict[int, tuple[str, permissions.PermissionService]] = {}
        # chat_id -> the service's request total as of the end of the last turn.
        # The guard needs a per-turn delta, not a lifetime count: a session that
        # asked once and then lost its gate would otherwise pass every later check
        # on the strength of that one early request.
        self._asked_before_turn: dict[int, int] = {}

    async def env_for(
        self, chat_id: int, session: str, workspace: Path
    ) -> dict[str, str]:
        """Approval env for this session, starting a service the first time."""
        resolved = permissions.configured()
        if not resolved.enabled:
            return {}
        existing = self._services.get(chat_id)
        if existing is not None and existing[0] == session:
            existing[1].update_timing(
                ttl_seconds=resolved.ttl_seconds,
                timeout_seconds=resolved.timeout_seconds,
            )
            return self._env(existing[1])
        if existing is not None:
            await existing[1].stop()
            # The replacement service starts its count at zero, so a stale
            # baseline here would make the first turn's delta negative.
            self._asked_before_turn.pop(chat_id, None)
            logger.info("permissions: chat %s session changed, grants dropped", chat_id)
        label = f"chat {chat_id}"
        service = permissions.PermissionService(
            broker=ApprovalBroker(
                approvals_mod.gate_from_frontend(self._frontend, chat_id),
                policies={"tool": approvals_mod.tool_policy()},
                timeout_seconds=resolved.timeout_seconds,
                label=label,
            ),
            workspace=workspace,
            ttl_seconds=resolved.ttl_seconds,
            label=label,
            tmux_session=permissions.tmux_session_name(chat_id, session),
            notify=self._notifier(chat_id),
        )
        await service.start()
        self._services[chat_id] = (session, service)
        logger.info(
            "permissions: chat %s -> 127.0.0.1:%d (own grant store, session %s, "
            "pane %s)",
            chat_id,
            service.port,
            session[:8],
            service.tmux_session,
        )
        return self._env(service)

    def _notifier(self, chat_id: int) -> Callable[[str], Awaitable[None]]:
        """How a permission service reaches the conversation it belongs to.

        A plain message rather than an approval card: these are reports of a gate
        that could not function, not questions, and offering buttons for something
        already decided would only invite a tap that does nothing.
        """

        async def send(text: str) -> None:
            await self._frontend.send(chat_id, Response(body=text))

        return send

    @staticmethod
    def _env(service: permissions.PermissionService) -> dict[str, str]:
        """What a spawned agent needs to reach this service.

        The pane name is published too, because claude-pty picks its own otherwise
        and the daemon has to know where an approval keystroke goes.
        """
        return {
            cotf_approve.ENDPOINT_ENV: service.base_url + permissions.DECIDE_PATH,
            cotf_approve.NOTIFY_ENV: service.base_url + permissions.NOTIFY_PATH,
            cotf_approve.REQUEST_TIMEOUT_ENV: str(service.broker.timeout_seconds + 5),
            permissions.TMUX_SESSION_ENV: service.tmux_session,
            **permissions.pty_env(),
        }

    def check_turn(self, chat_id: int, response: Response, backend: str) -> None:
        """Report a turn that used tools without the gate ever being asked.

        codex runs the command when its hook is untrusted or crashes, so that
        failure is invisible from the outside: the operator sees an ordinary turn
        and assumes it was supervised. Comparing the turn's own tool count against
        what the service was asked is the only place the two facts meet.

        Compares a per-turn delta rather than the service's lifetime total. With the
        total, a session that asked about one thing early and then lost its gate
        would pass every subsequent check forever on the strength of that one
        request -- which is the failure mode most worth catching, since a gate that
        never worked at all is far more likely to be noticed.
        """
        entry = self._services.get(chat_id)
        if entry is None:
            return
        total = entry[1].requests_seen
        asked_this_turn = total - self._asked_before_turn.get(chat_id, 0)
        self._asked_before_turn[chat_id] = total
        permissions.warn_if_ungated(
            sum(response.tool_counts.values()),
            asked_this_turn,
            backend=backend,
        )

    async def close_all(self) -> None:
        for _session, service in list(self._services.values()):
            await service.stop()
        self._services.clear()


class Orchestrator:
    def __init__(
        self,
        frontend: Frontend,
        platform: str,
        event_log: EventLog | None = None,
        egress_manager: SessionEgress | None = None,
        permissions_manager: SessionPermissions | None = None,
        command_broker: commands.CommandBroker | None = None,
    ) -> None:
        # None when sandboxing is off: no proxy, no per-session env, and the
        # spawn sites behave exactly as they did before any of this existed.
        self._egress = egress_manager
        # None when approvals are off, which keeps the spawn env untouched.
        self._permissions = permissions_manager
        # Daemon-wide service, but each turn receives a token bound to its own
        # workspace before the backend process is spawned.
        self._commands = command_broker
        self._frontend = frontend
        self._platform = platform
        self._running: dict[int, asyncio.Task] = {}
        # Session discriminator per chat: cron bumps an int via
        # reset_session; telegram /new pins a string token via set_session_token.
        # Either feeds session_uuid's `{chat_id}-{value}` tag.
        self._session_counters: dict[int, int | str] = {}
        self._queues: dict[int, asyncio.Queue[Turn]] = {}
        # Last turn's prompt size and window per chat, for the auto-compact gate.
        # Only pty mode populates it; elsewhere the gate never has a reading and
        # so never fires.
        self._context: dict[int, tuple[int, int]] = {}
        # None means use the live setting. Tests and embedders may assign an
        # explicit threshold through the compatibility property below.
        self._auto_compact_pct_override: int | None = None
        self._event_log = event_log if event_log is not None else EventLog()
        # chat_id -> {identifier, started_at_monotonic, session_uuid}.
        # Populated at dispatch, cleared on completion. Drives the heartbeat
        # `running_jobs` slot consumed by the TUI's Active AI jobs pane.
        self._in_flight: dict[int, dict] = {}
        # Restart-required config fields already reported. Compared as a set rather
        # than a flag so a second edit is reported too, and reverting one clears it.
        self._restarts_reported: tuple[str, ...] = ()
        # Whether the interruption notices already went out. Shutdown happens in
        # two steps from two places (see notify_interrupted), and a person told
        # twice that their turn died would go looking for two lost turns.
        self._interruptions_sent = False

    @property
    def _journal(self) -> TurnJournal:
        """Pending turns, on disk, written before a turn can run. This is what
        makes a stop recoverable at all: everything above it is in memory.

        Resolved per call rather than held from construction, for the reason
        `supervisor._last_running_file` gives: DATA_DIR is a module constant, so a
        path captured in `__init__` cannot see a later redirection of it. Cheap,
        because a journal is a path and nothing else.
        """
        return TurnJournal(DATA_DIR / "state" / f"{self._platform}.turns.json")

    def session_uuid(self, chat_id: int) -> str:
        counter = self._session_counters.get(chat_id, 0)
        tag = f"{chat_id}" if counter == 0 else f"{chat_id}-{counter}"
        return str(uuid5(NAMESPACE_URL, tag))

    def reset_session(self, chat_id: int) -> None:
        # Cron uses an int counter here; telegram pins a str token via
        # set_session_token. Only the int form is bumped (different chat_id
        # spaces), so coerce defensively to keep the +1 well-typed.
        current = self._session_counters.get(chat_id, 0)
        self._session_counters[chat_id] = (
            current if isinstance(current, int) else 0
        ) + 1
        self._forget_context(chat_id)

    def set_session_token(self, chat_id: int, token: str) -> None:
        """Pin the session discriminator to a token the frontend minted, so the
        session UUID matches the frontend's workspace suffix (telegram's /new
        uses a unique timestamp token). The tag formatting in session_uuid
        accepts a string just as it does cron's integer counter, which
        reset_session still bumps."""
        self._session_counters[chat_id] = token
        self._forget_context(chat_id)

    def _forget_context(self, chat_id: int) -> None:
        """Drop this chat's context reading because its session changed.

        The reading is keyed by chat, but it describes a *session* — and both
        callers above repoint a chat at a fresh one. Cron does this
        before every fire, so without this a big reading from the last fire would
        survive into the next and queue a compaction against a session that has
        nothing in it yet.
        """
        self._context.pop(chat_id, None)

    def is_busy(self, chat_id: int) -> bool:
        return chat_id in self._running and not self._running[chat_id].done()

    def queue_size(self, chat_id: int) -> int:
        queue = self._queues.get(chat_id)
        return queue.qsize() if queue else 0

    async def abort(self, chat_id: int) -> bool:
        """Stop the in-flight turn for a chat and drop anything queued behind it.

        Cancelling the drain task raises CancelledError into the awaited
        agent.run; the backend's exec finally reaps the whole process tree
        (spawned with start_new_session), so the agent CLI and its tool
        subprocesses die together instead of orphaning. Returns whether a turn
        was actually running.
        """
        queue = self._queues.get(chat_id)
        if queue is not None:
            while not queue.empty():
                try:
                    dropped = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                # Somebody asked for this to stop, so it is not pending work the
                # next start should resume. Without this, `$stop` would come back
                # to haunt them after a restart.
                self._journal.forget(dropped.journal_id)
        task = self._running.get(chat_id)
        if task is None or task.done():
            return False
        logger.info("abort: chat_id=%s cancelling in-flight turn", chat_id)
        running = self._in_flight.get(chat_id) or {}
        self._journal.forget(running.get("journal_id", ""))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def on_message(self, chat_id: int, text: str) -> None:
        logger.debug("on_message: chat_id=%s text=%s", chat_id, logs.redact(text))
        if self._due_for_compaction(chat_id):
            # Ahead of the message, not during the idle window before it. An idle
            # thread may never be spoken to again, and compacting one that isn't
            # pays a full-context pass for nothing. Waiting until someone
            # actually comes back costs them this turn's latency and saves every
            # turn after it.
            logger.info("on_message: chat_id=%s auto-compacting first", chat_id)
            await self.on_compact(chat_id)
        await self._enqueue(chat_id, Turn(text))

    async def on_compact(self, chat_id: int) -> None:
        """Queue a compaction for this chat. Runs in FIFO order like any turn."""
        await self._enqueue(chat_id, Turn("", compact=True))

    def _journal_turn(self, chat_id: int, turn: Turn, *, replays: int = 0) -> Turn:
        """Record a turn as pending and return it carrying its journal id.

        Before the queue, not after: a record written after the turn could run is
        a record that does not exist for the crash it was meant to survive.

        A compaction is not journaled. It is daemon maintenance with no text, so
        replaying it would answer nobody and offering it back would show an empty
        prompt; the auto-compact gate re-queues one when the next message arrives.
        """
        if turn.compact:
            return turn
        entry = PendingTurn(
            chat_id=chat_id,
            text=turn.text,
            route=self._frontend.route_for(chat_id),
            session=_session_tag(self._session_counters.get(chat_id)),
            compact=turn.compact,
            turn_id=new_turn_id(),
            recorded_at=time.time(),
            replays=replays,
        )
        self._journal.record(entry)
        return replace(turn, journal_id=entry.turn_id)

    async def _enqueue(self, chat_id: int, turn: Turn, *, replays: int = 0) -> None:
        turn = self._journal_turn(chat_id, turn, replays=replays)
        if chat_id not in self._queues:
            self._queues[chat_id] = asyncio.Queue()
        self._queues[chat_id].put_nowait(turn)
        if self.is_busy(chat_id):
            queued = self._queues[chat_id].qsize()
            logger.debug("enqueue: chat_id=%s busy, queued=%s", chat_id, queued)
            await self._frontend.notify_queued(chat_id, queued)
        else:
            logger.debug("enqueue: chat_id=%s starting drain", chat_id)
            self._running[chat_id] = asyncio.create_task(self._drain(chat_id))

    def _due_for_compaction(self, chat_id: int) -> bool:
        """Whether this chat's last turn left the context over the threshold.

        Consumes the reading, so two messages arriving back to back queue one
        compaction rather than two — the second would find nothing to compact and
        bill a full-context pass to be told so.
        """
        threshold = self._auto_compact_pct
        if not threshold:
            return False
        reading = self._context.get(chat_id)
        if reading is None:
            return False
        tokens, window = reading
        if window <= 0:
            return False
        pct = tokens * 100 / window
        if pct < threshold:
            return False
        self._context.pop(chat_id, None)
        logger.info(
            "auto-compact: chat_id=%s context %.0f%% (%s/%s) >= %s%%",
            chat_id,
            pct,
            tokens,
            window,
            threshold,
        )
        return True

    @property
    def _auto_compact_pct(self) -> int:
        if self._auto_compact_pct_override is not None:
            return self._auto_compact_pct_override
        return _auto_compact_pct()

    @_auto_compact_pct.setter
    def _auto_compact_pct(self, value: int) -> None:
        self._auto_compact_pct_override = value

    async def _drain(self, chat_id: int) -> None:
        queue = self._queues[chat_id]
        try:
            while True:
                try:
                    turn = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self._process(chat_id, turn)
        finally:
            if self._running.get(chat_id) is asyncio.current_task():
                self._running.pop(chat_id, None)

    async def _typing_loop(self, chat_id: int) -> None:
        while True:
            await self._frontend.send_typing(chat_id)
            await asyncio.sleep(4)

    async def _report_config_restarts(self, chat_id: int) -> None:
        """Name any config edit that this turn will not honour.

        Checked per turn, and reported into the conversation the operator is
        already in, because that is where they will be right after saving the file:
        most of `config.yaml` takes effect on the next read, so the only edits
        worth interrupting for are the ones where saving is *not* enough.

        Reported once per distinct set. `settings.check_reload` compares against
        the startup baseline and so keeps returning the same answer until a
        restart, which sending every turn would turn into noise nobody reads.

        Frontend failures are swallowed on purpose. A missed notice is a worse log
        line; an exception here would kill the drain task with turns still queued.
        """
        changed = settings.check_reload()
        if changed == self._restarts_reported:
            return
        self._restarts_reported = changed
        if not changed:
            return
        logger.warning(
            "settings: %s changed in %s and needs a daemon restart to take effect",
            ", ".join(changed),
            settings.operator_settings(),
        )
        body = (
            f"Config change to {', '.join(changed)} needs a restart. The rest of "
            f"{settings.FILENAME} is picked up on its own, but this part is read "
            "once at startup, so this turn and the ones after it still run the old "
            "value."
        )
        try:
            await self._frontend.send(chat_id, Response(body=body))
        except Exception:
            logger.exception("settings: could not report the restart-required change")

    async def _process(self, chat_id: int, turn: Turn) -> None:
        text = turn.text
        # Before any of this turn's setup: from here on an agent may start, and a
        # turn that started is never replayed automatically. Marking early errs
        # toward offering it back rather than repeating side effects, which is the
        # safe direction for the few milliseconds of difference.
        self._journal.mark_dispatched(turn.journal_id)
        interrupted = False
        await self._report_config_restarts(chat_id)
        workspace = workspace_path(self._frontend.workspace_name(chat_id), DATA_DIR)
        workspace.mkdir(parents=True, exist_ok=True)
        if self._platform in agent.ATTACHMENT_PLATFORMS:
            (workspace / agent.OUTBOX_DIRNAME).mkdir(exist_ok=True)
        agent.ensure_persona(workspace, self._frontend.persona_source(chat_id))
        session = self.session_uuid(chat_id)
        identifier = self._frontend.workspace_name(chat_id)
        logger.debug(
            "process: chat_id=%s workspace=%s session=%s", chat_id, workspace, session
        )

        self._event_log.append(
            EVENT_DISPATCHED,
            source=self._platform,
            backend=current_backend_key(),
            identifier=identifier,
            workspace=workspace,
            session_uuid=session,
        )
        self._in_flight[chat_id] = {
            "identifier": identifier,
            "started_at_monotonic": time.monotonic(),
            "session_uuid": session,
            # So `abort` can drop the journal entry of the turn it cancels: a
            # stop somebody asked for is not work to resume later.
            "journal_id": turn.journal_id,
        }

        # Startup notification performs frontend I/O and is therefore a
        # cancellation point. Keep it inside the lifecycle try/finally so an
        # abort while it is in progress still clears the in-flight slot and
        # any partially-applied frontend status/reaction.
        typing_task: asyncio.Task | None = None
        env_token = None
        relay: sandbox.SessionRelay | None = None
        command_token: str | None = None
        pane: tmux.Pane | None = None
        interim: InterimProgress | None = None
        sink_token = None
        try:
            # Point this turn's agent at its own egress proxy. Set here rather
            # than passed down because the spawn is several frames below, inside
            # a backend; asyncio copied this task's context at creation, so the
            # value reaches that spawn and no other session's.
            #
            # Inside the try because starting a proxy can fail (a bound port, an
            # exhausted fd table). Outside it, that failure escaped _process
            # entirely: the in-flight slot set just above leaked, notify_complete
            # never ran, and the whole drain task died with turns still queued.
            session_overrides: dict[str, str] = {}
            # Host this turn in its own tmux server so the TUI can mirror the
            # pane. The name is `permissions.tmux_session_name` because the
            # approval path addresses the same pane to type an answer into it;
            # both call that one function, so the two cannot name different
            # panes. Skipped entirely when tmux is absent — a turn then runs
            # unmirrored rather than not at all.
            if tmux.hosting_available():
                pane = tmux.pane_for(permissions.tmux_session_name(chat_id, session))
            if pane is not None:
                session_overrides.update(pane.env)
                logger.debug("tmux: chat %s hosted in pane %s", chat_id, pane.session)
            elif not tmux.available():
                # INFO rather than debug: this is the whole reason a watch pane
                # shows a transcript instead of the agent's terminal, and it is
                # the one cause an operator can actually fix. Silent when they
                # switched hosting off themselves — that one is not a surprise.
                logger.info(
                    "tmux is not on PATH, so this turn runs unmirrored; "
                    "install tmux to watch the agent's own terminal"
                )
            if self._egress is not None:
                session_overrides.update(await self._egress.env_for(chat_id, session))
            if self._permissions is not None:
                session_overrides.update(
                    await self._permissions.env_for(chat_id, session, workspace)
                )
            if self._commands is not None:
                command_env = self._commands.agent_env(workspace)
                command_token = command_env[commands.TOKEN_ENV]
                session_overrides.update(command_env)
            # Must come before the spawn and after the overrides are known: on a
            # Linux jail the agent's network namespace contains nothing until
            # this bridges the brokered ports into it, and one of those ports
            # belongs to this session's own egress proxy. Inert on macOS, where
            # seatbelt reaches the host's loopback directly.
            relay = await sandbox.open_session_relay(session_overrides, str(chat_id))
            if session_overrides:
                env_token = sandbox.session_env(session_overrides)
            await self._frontend.notify_start(chat_id)
            typing_task = asyncio.create_task(self._typing_loop(chat_id))
            if turn.compact:
                response = await self._run_compaction(
                    chat_id, workspace, session, self._frontend.timeout_for(chat_id)
                )
            else:
                identity = getattr(self._frontend, "sender_identity", None)
                suggestions = _suggestions_enabled()
                prompt = f"{text}\n\n{SUGGESTIONS_TEMPLATE}" if suggestions else text
                # The backend's nudge retry (an empty or block-only reply)
                # carries the suggestions instruction too, so the retried
                # answer comes back with working buttons rather than a bare
                # reply. None keeps the backend's plain nudge.
                nudge_prompt = (
                    f"{agent.NUDGE_PROMPT}\n\n{SUGGESTIONS_TEMPLATE}"
                    if suggestions
                    else None
                )
                interim = self._start_interim(chat_id)
                if interim is not None:
                    sink_token = agent.set_progress_sink(interim.emit)
                try:
                    response = await agent.run(
                        workspace,
                        session,
                        prompt,
                        self._platform,
                        user_name=(
                            identity(chat_id)
                            if callable(identity)
                            else self._frontend.sender_name(chat_id)
                        ),
                        channel_context=self._frontend.channel_context(chat_id),
                        timeout=self._frontend.timeout_for(chat_id),
                        nudge_prompt=nudge_prompt,
                    )
                except asyncio.CancelledError:
                    # FIRST, and the order is load-bearing rather than stylistic:
                    # CancelledError IS a BaseException, so the arm below would
                    # otherwise catch an abort and take the graceful path.
                    # Synchronous on purpose — $stop is somebody waiting for an
                    # ack, and aclose() can spend INTERIM_CLOSE_GRACE draining
                    # posts that are now moot. One in-flight post may be
                    # cancelled with its ts unrecorded; that is the trade.
                    if interim is not None:
                        interim.cancel()
                    raise
                except BaseException:
                    # Both `except ClaudeUnavailableError` and `except Exception`
                    # below post a message of their own; the relay has to be shut
                    # down before either of them does, or a progress line lands
                    # after the error text and a cancelled post leaves an
                    # unrecorded ts. flush=True because on this path there is no
                    # reply body to duplicate against, and the last thing the
                    # agent said before it failed is the most useful line there is.
                    if interim is not None:
                        await interim.aclose(flush=True)
                    raise
                # A stray <suggestions> block is stripped whether or not the
                # feature is on: an agent mid-session keeps emitting them
                # after a toggle-off, and raw machine text must never reach
                # the user. The labels only become buttons while enabled.
                response.body, parsed = _extract_suggestions(response.body)
                if suggestions:
                    response.suggestions = parsed
                if interim is not None:
                    # Before the reply is posted, so a late progress message cannot
                    # land after the answer it was leading up to. Discards whatever
                    # is still held rather than dumping a digest above the reply.
                    await interim.aclose()
            if self._permissions is not None:
                # After the turn, because the tool count only exists once it is
                # over. Reporting late beats not reporting: an ungated turn is
                # exactly what an operator who enabled approvals must never
                # discover by accident.
                self._permissions.check_turn(
                    chat_id, response, settings.get("AGENT_BACKEND", "claude")
                )
            logger.debug(
                "process: chat_id=%s response cost=%.4f tokens_in=%s tokens_out=%s",
                chat_id,
                response.cost,
                response.tokens_in,
                response.tokens_out,
            )
            # pty reports it off the statusline; native (non-ollama) adds it up
            # in `_native_context_fields`; ollama withholds it. A turn that
            # doesn't report sets nothing rather than zeroing the reading, so a
            # mode switch mid-thread can't make a large context look small.
            if response.context_tokens and response.context_window_size:
                self._context[chat_id] = (
                    response.context_tokens,
                    response.context_window_size,
                )
            if self._platform in agent.ATTACHMENT_PLATFORMS:
                response.attachments = agent.collect_outbox(workspace)
            delivered = await self._frontend.send(chat_id, response)
            if delivered:
                agent.archive_outbox(workspace, delivered)
            self._event_log.append(
                EVENT_WORKER_DONE,
                source=self._platform,
                backend=current_backend_key(),
                identifier=identifier,
                workspace=workspace,
                session_uuid=session,
                cost=response.cost,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
            )
        except asyncio.CancelledError:
            logger.info("process: chat_id=%s aborted", chat_id)
            # Keep the journal entry: this is exactly the case recovery exists
            # for, and the record is what lets the next start offer it back.
            interrupted = True
            raise
        except ClaudeUnavailableError as exc:
            logger.warning("Claude unavailable for chat %s: %s", chat_id, exc)
            await self._frontend.send(
                chat_id,
                Response(body=f"Claude unavailable: {exc}"),
            )
            self._event_log.append(
                EVENT_WORKER_FAILED,
                source=self._platform,
                backend=current_backend_key(),
                identifier=identifier,
                workspace=workspace,
                session_uuid=session,
                error=str(exc),
                reason="unavailable",
            )
        except Exception as exc:
            logger.exception("Agent error for chat %s", chat_id)
            await self._frontend.send(
                chat_id,
                Response(body=f"Error: {exc}"),
            )
            self._event_log.append(
                EVENT_WORKER_FAILED,
                source=self._platform,
                backend=current_backend_key(),
                identifier=identifier,
                workspace=workspace,
                session_uuid=session,
                error=str(exc),
            )
        finally:
            if not interrupted:
                # Answered, or answered with an error the person can see. Either
                # way nothing is owed, so the record goes.
                self._journal.forget(turn.journal_id)
            self._in_flight.pop(chat_id, None)
            if typing_task is not None:
                typing_task.cancel()
            if sink_token is not None:
                agent.reset_progress_sink(sink_token)
            if interim is not None:
                # Synchronous, like typing_task: a finally on the abort path must
                # not spend INTERIM_CLOSE_GRACE before the "Stopped" ack goes out.
                # Idempotent with the aclose/cancel above.
                interim.cancel()
            if env_token is not None:
                sandbox.reset_session_env(env_token)
            if pane is not None:
                # Ends the server, so a backend that left its pane running past
                # the deadline is reaped here too. A process-group kill cannot
                # reach a pane child, which is why this is not covered by the
                # backend's own teardown.
                tmux.kill(pane)
            if relay is not None:
                # Closing this drops the namespace's only route to the host, so
                # it has to outlive the spawn. Safe on every path including the
                # abort one, and a no-op unless this was a Linux jail.
                await relay.close()
            if command_token is not None and self._commands is not None:
                self._commands.revoke_token(command_token)
            await self._frontend.notify_complete(chat_id)

    def _start_interim(self, chat_id: int) -> InterimProgress | None:
        """This turn's progress relay, or None when nothing could come of one.

        The second gate is not redundant with the first. `send_progress` is a
        concrete no-op on the ABC, so a frontend that never overrode it — today,
        Telegram — would otherwise get a relay per turn whose drain task ticks for
        the life of the turn and delivers into nothing. Comparing the attribute on
        the class rather than the instance's bound method, the same way
        `tests/test_telegram.py` asserts the no-op is still inherited.
        """
        if not interim_progress_enabled():
            return None
        if type(self._frontend).send_progress is Frontend.send_progress:
            logger.debug(
                "interim: %s does not deliver progress, not starting a relay",
                type(self._frontend).__name__,
            )
            return None

        async def post(text: str) -> None:
            await self._frontend.send_progress(chat_id, text)

        return InterimProgress(post)

    async def _run_compaction(
        self, chat_id: int, workspace: Path, session: str, timeout: float | None
    ) -> Response:
        """Compact the session and render the outcome as this turn's reply."""
        # Whatever this chat was holding describes the pre-compaction prompt and
        # is stale the moment the compaction lands. Dropping it (rather than
        # guessing the new size) is what stops a manual `$compact` from being
        # followed straight away by an automatic one on the next message.
        self._context.pop(chat_id, None)
        outcome = await agent.compact(workspace, session, timeout=timeout)
        if outcome is None:
            return Response(body="This backend can't compact a conversation.")
        return Response(body=outcome.summary(), compaction=outcome)

    def heartbeat_extra(self) -> dict:
        """Snapshot in-flight chat jobs for the TUI's Active AI jobs pane.

        Shape mirrors the jobs worker's rows so the dashboard can merge
        across sources with a single normalizer.
        """
        now = time.monotonic()
        running_jobs = [
            {
                "identifier": j["identifier"],
                "chat_id": chat_id,
                "uptime_s": int(now - j["started_at_monotonic"]),
                "session_uuid": j["session_uuid"],
            }
            for chat_id, j in self._in_flight.items()
        ]
        # Queued turns live only in this process's memory, so the heartbeat is
        # the one place an outside reader can learn they exist. The supervisor
        # needs that to say what a stop is about to cost before it signals.
        return {
            "running_jobs": running_jobs,
            "queued_turns": sum(queue.qsize() for queue in self._queues.values()),
        }

    def interrupted_chats(self) -> dict[int, tuple[bool, int]]:
        """chat_id -> (a turn is running, how many turns wait behind it).

        What a stop is about to destroy. Chats with neither are left out, so an
        empty mapping means a stop costs nobody an answer.
        """
        chats: dict[int, tuple[bool, int]] = {}
        for chat_id, task in self._running.items():
            if not task.done():
                chats[chat_id] = (True, 0)
        for chat_id, queue in self._queues.items():
            queued = queue.qsize()
            if not queued and chat_id not in chats:
                continue
            chats[chat_id] = (chats.get(chat_id, (False, 0))[0], queued)
        return chats

    async def resume_pending(self) -> tuple[int, int]:
        """Act on the journal left by the last stop. Returns (replayed, nudged).

        Must run after the sandbox is up, because a replayed turn spawns a jailed
        agent that needs the broker, the egress proxy and the shims, and before
        the frontend starts listening, so a live message cannot overtake work that
        was already waiting.

        The journal is emptied by `take` before anything here runs, so a turn that
        kills the daemon cannot be replayed at every start.
        """
        replay, nudge = self._journal.take()
        if not replay and not nudge:
            return 0, 0
        logger.info("resume: replaying %d, offering back %d", len(replay), len(nudge))
        for entry in nudge:
            await self._offer_back(entry)
        by_chat: dict[int, list[PendingTurn]] = {}
        for entry in replay:
            by_chat.setdefault(entry.chat_id, []).append(entry)
        for chat_id, entries in by_chat.items():
            await self._replay_chat(chat_id, entries)
        return len(replay), len(nudge)

    def _restore_chat(self, entry: PendingTurn) -> None:
        """Put back what a replay or a nudge needs before it can be delivered.

        The route first: the frontend's session tables are empty after a restart,
        so without it the turn runs and then has nowhere to post, and no reaction
        can land on the message that asked. Then the session discriminator, or the
        turn would resume a different conversation than the one the person was in.

        Called once per entry rather than once per chat, because a route carries
        the *message* that asked as well as the thread it lives in, and each turn
        has its own.
        """
        self._frontend.restore_route(entry.chat_id, entry.route)
        if entry.session:
            self._session_counters[entry.chat_id] = entry.session

    async def _offer_back(self, entry: PendingTurn) -> None:
        """Hand one turn back, for the rare case a replay is the wrong answer."""
        self._restore_chat(entry)
        try:
            await self._frontend.notify_nudge(entry.chat_id, entry.text)
        except Exception:
            logger.exception(
                "resume: could not offer chat_id=%s its interrupted turn",
                entry.chat_id,
            )

    async def _replay_chat(self, chat_id: int, entries: list[PendingTurn]) -> None:
        """Re-queue one chat's pending turns, oldest first.

        Silent by design: no announcement, because the turn's own reaction and its
        reply are what tell the person it is running, exactly as they would for a
        message sent a second ago.
        """
        try:
            await self._frontend.notify_resumed(chat_id, len(entries))
        except Exception:
            # The work matters more than any announcement of it.
            logger.exception(
                "resume: could not tell chat_id=%s it was resumed", chat_id
            )
        for entry in entries:
            self._restore_chat(entry)
            await self._enqueue(
                chat_id,
                Turn(_resume_prompt(entry), compact=entry.compact),
                replays=entry.replays,
            )

    async def notify_interrupted(self) -> None:
        """Tell every chat with unfinished work that this daemon is going down.

        Called while the frontend is still fully connected, and again (as a
        no-op) from `shutdown`. Both, because the ordering matters in opposite
        directions: the notices must go out before the frontend's own listener is
        torn down, and they must not be forgettable by a caller that only knows
        about `shutdown`. Idempotent, so saying it twice says it once.

        Bounded by a budget: the supervisor SIGKILLs after its grace, and a
        frontend whose API has gone away must cost the exit a few seconds rather
        than the whole window.
        """
        if self._interruptions_sent:
            return
        self._interruptions_sent = True
        chats = self.interrupted_chats()
        if not chats:
            return
        logger.info("shutdown: notifying %d interrupted chat(s)", len(chats))
        try:
            await asyncio.wait_for(
                self._post_interruptions(chats), SHUTDOWN_NOTICE_BUDGET_S
            )
        except TimeoutError:
            logger.warning(
                "shutdown: interruption notices unfinished after %.0fs, exiting anyway",
                SHUTDOWN_NOTICE_BUDGET_S,
            )

    async def _post_interruptions(self, chats: dict[int, tuple[bool, int]]) -> None:
        """Post one notice per chat. One frontend failure never costs the rest."""
        for chat_id, (running, queued) in chats.items():
            try:
                await self._frontend.notify_interrupted(
                    chat_id, running=running, queued=queued
                )
            except Exception:
                logger.exception(
                    "shutdown: could not notify chat_id=%s of its lost work", chat_id
                )

    async def shutdown(self) -> None:
        await self.notify_interrupted()
        for task in self._running.values():
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)


def _auto_compact_pct() -> int:
    """Resolve the auto-compact threshold. 0 (off) for unset or unusable values.

    A junk value disables the feature rather than taking the daemon down: this
    is a cost optimization, not something worth refusing to start over. The
    value is logged so a typo doesn't look like a silently working setting.
    """
    raw = settings.get(AUTO_COMPACT_PCT_VAR).strip()
    if not raw:
        return 0
    try:
        pct = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number, auto-compact off", AUTO_COMPACT_PCT_VAR, raw
        )
        return 0
    if not 1 <= pct <= 100:
        logger.warning(
            "%s=%d is outside 1-100, auto-compact off", AUTO_COMPACT_PCT_VAR, pct
        )
        return 0
    return pct


def _redact_token(token: str) -> str:
    """Mask a secret for log output."""
    if not token:
        return "<unset>"
    if len(token) <= 4:
        return "***"
    return f"{token[:2]}***{token[-2:]}"


def _log_settings_summary(platform: str, frontend: Frontend) -> None:
    """Dump the resolved runtime settings at startup.

    Pulls shared bits (log level, data dir, agent backend) from the resolved
    settings, then
    appends frontend-specific fields via Frontend.describe(). Secrets are
    expected to be redacted by the frontend before being returned.
    """
    import os

    backend = settings.get("AGENT_BACKEND", "claude").lower()
    mode_var = f"{backend.upper()}_MODE"
    mode = settings.get(mode_var, "native").lower()

    logger.info("%s settings:", platform)
    logger.info("  platform        = %s", platform)
    logger.info("  log_level       = %s", os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.info("  data_dir        = %s", DATA_DIR)
    logger.info("  agent_backend   = %s", backend)
    logger.info("  %-15s = %s", mode_var.lower(), mode)
    if mode == "ollama":
        logger.info("  ollama_model    = %s", settings.get("OLLAMA_MODEL", "<unset>"))

    for label, value in frontend.describe().items():
        logger.info("  %-15s = %s", label, value)


async def _start_sandbox(
    frontend: Frontend,
) -> tuple[broker.Broker | None, SessionEgress | None, commands.CommandBroker | None]:
    """Bring up the credential broker, per-session egress, and command broker.

    All three None when sandboxing is off, which is what makes the spawn sites
    behave exactly as they did before any of this existed.

    Partial failure tears down what already started and re-raises. Without that,
    a command broker that cannot bind left the credential broker listening with
    every route's key loaded in memory and ANTHROPIC_BASE_URL published, for a
    daemon that was on its way to exiting.
    """
    # Approval artifacts are independent of the sandbox. Generate them before
    # the sandbox-off return so a fresh ask-mode deployment has both backends'
    # prompt/hook wiring available.
    permissions.check()
    if permissions.configured().enabled:
        permissions.write_shim()
        permissions.write_mcp_config()
        permissions.write_pty_settings()
    if not sandbox.enabled():
        return None, None, None
    broker_instance = None
    command_broker = None
    try:
        # The broker is shared: its routes are operator config, not something a
        # session earns, so one credential-holding proxy for the daemon is right.
        # Egress is the opposite -- see SessionEgress for why it is per-session.
        broker_instance = await broker.start_default_broker(
            approvals=ApprovalBroker(
                approvals_mod.gate_from_frontend(frontend),
                policy=approvals_mod.ApprovalPolicy(
                    never_ask=egress.never_ask_subjects(),
                ),
            )
        )
        # The broker is daemon-wide, but its bearer token is per turn so the
        # request can be bound to the originating workspace. Publish only the
        # harmless endpoint here; the session token is layered by _process().
        command_broker = commands.CommandBroker(sandbox.shim_dir())
        await command_broker.start()
        # The endpoint is harmless to publish daemon-wide, but the bearer token
        # must be issued per turn and bound to that turn's workspace. Do not leave
        # the private base token in the daemon environment for sandbox.agent_env
        # to forward accidentally.
        command_env = command_broker.agent_env()
        os.environ.update(
            {
                key: value
                for key, value in command_env.items()
                if key != commands.TOKEN_ENV
            }
        )
        os.environ.pop(commands.TOKEN_ENV, None)
    except Exception:
        logger.exception("sandbox: startup failed, revoking what already started")
        if command_broker is not None:
            await command_broker.stop()
        if broker_instance is not None:
            await broker_instance.stop()
        raise
    logger.info(
        "sandbox: mode=%s broker=%s egress=per-session commands=%s content_log=%s",
        sandbox.mode(),
        "on" if broker_instance else "none",
        ",".join(command_broker.shimmed) or "none",
        "on" if logs.log_content() else "redacted",
    )
    # macOS cannot report a seatbelt denial, so the agent's own blocked reads are
    # permanently invisible. Probing the denies here is the substitute: it records,
    # per run, that the boundary was actually in force rather than inferring it
    # from an absence of errors.
    # Both halves, in the order that makes the second meaningful. The job worker
    # runs the same gate from its own composition root, so the sequence lives in
    # sandbox.verify_boundary rather than being repeated per daemon.
    await sandbox.verify_boundary()
    return broker_instance, SessionEgress(frontend), command_broker


async def run(frontend: Frontend, platform: str) -> None:
    """Start the orchestrator with the given frontend. Blocks until SIGINT/SIGTERM."""
    from claude_on_the_fly import logs

    logs.configure(platform)
    (DATA_DIR / "memory" / "users").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)

    _log_settings_summary(platform, frontend)

    # Before anything reads the policy: seed the operator's file if it is missing,
    # name any problem with it now, and record what the restart-required fields
    # were, so a later edit to one can be reported rather than silently ignored.
    #
    # Unconditional, where this used to sit inside the sandbox branch. The file
    # holds more than sandbox policy now, and `permissions:` in particular is read
    # with the sandbox off -- so gating the seeding and the validation on
    # `sandbox.enabled()` meant the one deployment shape that most needs the
    # diagnostics was the shape that never got them.
    settings.check_operator_settings()

    # Heartbeat freshness is not an atomic startup guard: two daemons can both
    # pass it before either writes. Claim ownership before sweeping orphaned
    # process groups or starting any shared service.
    heartbeat = HeartbeatWriter(platform)
    heartbeat.claim()

    # Agent CLIs are separate process groups. Recover groups left by a forced
    # daemon stop before accepting new work, and record every live group so the
    # supervisor can reap it even if this process is SIGKILLed.
    process_ledger = ProcessLedger(DATA_DIR / "state" / f"{platform}.pids")
    listener_attached = False
    heartbeat_task: asyncio.Task[None] | None = None

    try:
        process_ledger.sweep()
        agent.add_process_listener(process_ledger.on_process)
        listener_attached = True
        # Same recovery, one layer out: a pane is a child of its own tmux server,
        # so the process ledger above never saw it. The server dies with the
        # daemon that started it, but its socket directory does not, and one per
        # turn would otherwise accumulate.
        tmux.sweep()

        # When sandboxing is enabled, the broker holds the real API keys and the
        # agent reaches them only through loopback. start_default_broker publishes
        # base-urls into os.environ that sandbox.agent_env forwards to the agent.
        #
        # The egress proxy covers everything the broker cannot: it gates ordinary
        # HTTPS by destination host and, via the approval broker, can ask the
        # operator to grant an unknown one mid-run instead of failing the task.
        broker_instance, session_egress, command_broker = await _start_sandbox(frontend)

        # Approvals are independent of COTF_SANDBOX, so this is built whenever the
        # config asks for it rather than only inside the sandbox branch.
        session_permissions = (
            SessionPermissions(frontend) if permissions.configured().enabled else None
        )
        orch = Orchestrator(
            frontend,
            platform,
            egress_manager=session_egress,
            permissions_manager=session_permissions,
            command_broker=command_broker,
        )
        frontend.set_orchestrator(orch)

        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        heartbeat.set_extra_provider(orch.heartbeat_extra)
        heartbeat_task = asyncio.create_task(heartbeat.run())

        # Between the sandbox and the listener, deliberately. A replayed turn
        # spawns a jailed agent, so it needs the broker, the proxy and the shims
        # that _start_sandbox built; and queueing it before the frontend accepts
        # traffic is what stops a live message from overtaking work that was
        # already waiting when the daemon went down.
        await orch.resume_pending()

        frontend_task = asyncio.create_task(frontend.start(orch.on_message))
        logger.info("Running (%s). Ctrl+C to stop.", platform)

        await stop.wait()

        logger.info("Shutting down...")
        # First, while the frontend's own connection is still up: tell everyone
        # whose work this stop is about to destroy. Cancelling the frontend task
        # first can tear down the client the notice needs to post through.
        await orch.notify_interrupted()
        heartbeat_task.cancel()
        frontend_task.cancel()
        await asyncio.gather(heartbeat_task, frontend_task, return_exceptions=True)
        await orch.shutdown()
        await frontend.stop()
        # Stopping these revokes every route out of the sandbox at once.
        if session_egress is not None:
            await session_egress.close_all()
        if session_permissions is not None:
            await session_permissions.close_all()
        if command_broker is not None:
            await command_broker.stop()
        if broker_instance is not None:
            await broker_instance.stop()
    finally:
        # Startup or frontend failures can happen before the normal shutdown
        # sequence. Never leave a durable process listener attached to the module.
        if listener_attached:
            agent.remove_process_listener(process_ledger.on_process)
        # Before remove_owned, always. A failure between the heartbeat task
        # starting and the normal shutdown sequence (resume_pending raising, or
        # a sandbox teardown error) used to leave the loop alive across the
        # removal, and its next write recreated the file with a fresh timestamp
        # and the pid of a process that is exiting. The TUI then read a live
        # daemon that was already gone.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        heartbeat.remove_owned()
        heartbeat.release()
