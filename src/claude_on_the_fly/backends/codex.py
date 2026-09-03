"""Codex CLI backend."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from claude_on_the_fly import (
    agent,
    codex_state,
    permissions,
    pricing,
    sandbox,
    tmux,
    transcript,
)
from claude_on_the_fly.agent import (
    DEFAULT_TIMEOUT,
    NUDGE_PROMPT,
    Compaction,
    OllamaLauncher,
    Response,
    build_system_prompt,
    strip_suggestions_blocks,
)

logger = logging.getLogger(__name__)

# Passed via `-c` for one compaction run only, never written to the user's
# config.toml. Any value comfortably under a real thread's context makes codex's
# pre-turn `run_auto_compact` check fire; the exact number doesn't matter, so
# this is small enough to always trip and non-zero to stay a plausible limit.
COMPACT_TOKEN_LIMIT = 2000
# Codex compaction is checked *before* a turn, so it needs one to hang off — it
# cannot be a standalone operation the way claude's `/compact` is. Keep the reply
# tiny: it lands in the conversation the compaction just summarized.
COMPACT_TRIGGER_PROMPT = "Reply with the single word: compacted"

# How long to let codex exit on its own after it has gone quiet on a finished
# turn, before killing it.
#
# codex can deadlock in its own teardown: it drops its tokio runtime with no
# `shutdown_timeout`, so one stuck `spawn_blocking` task — which `abort()`
# cannot cancel — parks the process forever with its reply already written to
# stdout. Waiting for EOF is no escape, because a surviving
# `codex-code-mode-host` child inherits stdout and holds the pipe open for as
# long as it lives; observed hangs lasted days.
#
# `turn.completed` is codex saying the turn produced everything it is going to.
# After that, continued silence is teardown rather than work, so kill it and let
# the caller deliver the reply already in hand.
POST_TURN_EXIT_GRACE = 30.0


async def _kill_once_quiet_after_turn(
    proc: asyncio.subprocess.Process, follower: _RolloutFollower
) -> None:
    """Kill `proc` once it has been silent for the grace period post-turn.

    "Silent" is measured on the rollout rather than on stdout: without `--json`
    stdout carries only the closing reply, so a codex wedged after finishing
    would look silent from the moment it started.
    """
    await follower.turn_completed.wait()
    while True:
        idle = time.monotonic() - follower.last_output_at
        if idle >= POST_TURN_EXIT_GRACE:
            break
        # Re-armed by any further output, so a second turn in one exec
        # (auto-compaction) is never cut short.
        await asyncio.sleep(POST_TURN_EXIT_GRACE - idle)
    logger.warning(
        "codex exec: silent for %ss after task_complete and still running; "
        "killing it so the finished reply can be delivered",
        POST_TURN_EXIT_GRACE,
    )
    await agent._kill_process_tree(proc)


# `model_reasoning_effort` choices, from codex's config reference. Whichever key
# the resolver read it from is validated against this before it reaches codex,
# because the shared `agent.ollama.effort` is also claude's, and claude's
# accepted set differs: no `minimal`.
#
# `max` is codex's too, not claude-only, and `ultra` sits above it. Codex's own
# model catalog is the source: `supported_reasoning_levels` reads
# [low, medium, high, xhigh, max] for gpt-5.6-luna and gpt-reserve, and adds
# `ultra` for gpt-5.6-sol and gpt-5.6-terra. Omitting them here made
# `effort: max` a silently dropped setting -- warned, replaced with no `-c`,
# leaving the turn on whatever ~/.codex/config.toml said.
#
# Levels are per-model in that catalog (gpt-5.5 stops at xhigh), so this one
# flat set is a lower bound on correctness: it can still accept a level the
# chosen model lacks, and codex rejects that itself.
_CODEX_EFFORT_LEVELS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)


def _merge_codex_results(first: dict, second: dict) -> dict:
    """Combine an initial result with a nudge retry: body from retry, usage
    and tool_counts summed, thread_id preserved.
    """
    return {
        "thread_id": first.get("thread_id") or second.get("thread_id"),
        "body": second.get("body") or "",
        "usage": agent._sum_counts(first.get("usage"), second.get("usage")),
        "error": None,
        "tool_counts": agent._sum_counts(
            first.get("tool_counts"), second.get("tool_counts")
        ),
    }


_CODEX_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _usage_from_events(events: list[dict]) -> dict:
    """Sum a run of per-call usage records into one turn's counts."""
    return {
        field: sum(int(event.get(field, 0) or 0) for event in events)
        for field in _CODEX_USAGE_FIELDS
    }


def _billable_usage(usage: dict) -> tuple[int, int, int, int]:
    """Return non-overlapping ``(input, output, cache_read, cache_write)``."""
    input_tokens = usage.get("input_tokens", 0)
    cache_read = usage.get("cached_input_tokens", 0)
    cache_write = usage.get("cache_write_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    reasoning_tokens = usage.get("reasoning_output_tokens", 0)
    return (
        max(input_tokens - cache_read - cache_write, 0),
        output_tokens + reasoning_tokens,
        cache_read,
        cache_write,
    )


# --- rollout reading -------------------------------------------------------
#
# The rollout, not stdout, is this backend's machine data. `codex exec --json`
# would put the same events on stdout, but a `--json` run renders JSONL into the
# terminal, and the terminal is what the tmux pane mirrors — so the flag buys a
# parser and costs the live view. Reading the rollout instead serves both arms
# from one source, which is also the only way the hosted and unhosted arms can be
# guaranteed to report a turn identically.
#
# The rollout names items in PascalCase where the `--json` stream used
# snake_case, so this is a second vocabulary rather than the same one. Both sets
# are observed, not guessed: this one comes from every item type present across
# 400 local rollouts (Reasoning, CommandExecution, AgentMessage,
# CollabAgentToolCall, UserMessage, FileChange, Extension, ImageView,
# ContextCompaction). An unknown type counts as a tool, which is the safe
# direction — a new tool shows up in the footer rather than vanishing from it.
_NON_TOOL_ROLLOUT_ITEMS = frozenset(
    {"AgentMessage", "ContextCompaction", "Reasoning", "UserMessage"}
)

# `phase` on an AgentMessage says what the message was for: codex marks its
# closing reply `final_answer` and everything it says along the way
# `commentary`. The stream had no such field, which is why the old relay had to
# infer narration by waiting for a tool call to prove it. Reading the phase is
# the same judgement made by the producer instead of reconstructed here.
_PHASE_FINAL = "final_answer"

# How often a running turn's rollout is re-read. Progress is the only thing
# waiting on it, and a chat frontend coalesces what it forwards anyway
# (`interim.py`), so a faster poll would buy nothing a reader could see and
# would stat the file for the whole length of a turn.
ROLLOUT_POLL_S = 1.0

# What codex takes as "read the prompt from stdin", per `codex exec --help`.
_STDIN_PROMPT = "-"

# Rows of the pane kept to explain a failing exit. A pane is a screenful, not an
# error message, and the tail is where the failure is.
_PANE_ERROR_ROWS = 40

# How often the hosted arm asks whether the turn is done. The answer comes from
# the rollout follower, which polls once a second itself, so anything finer only
# adds `tmux has-session` calls.
_PANE_POLL_S = 0.5


def _rollout_content_text(content: Any) -> str:
    """The text of a rollout `content` list, which holds typed parts.

    Codex writes `[{"type": "Text", "text": ...}]` on an item and
    `[{"type": "output_text", "text": ...}]` on a response_item. Both are read
    because both name the same thing, and a part with no text is skipped rather
    than rendered as "None".
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        part.get("text") or ""
        for part in content
        if isinstance(part, dict) and part.get("text")
    ]
    return "".join(parts)


def parse_codex_rollout(records: Iterable[dict]) -> dict:
    """One turn's rollout records folded into the same dict `run` consumes.

    The keys match what the `--json` stream parser produced, so nothing
    downstream had to learn a second shape: `thread_id`, `body`,
    `last_assistant_text`, `usage`, `completed`, `tool_counts`, `error`.

    `completed` comes from `task_complete`, which codex writes after the final
    message — the same in-band proof the stream's `turn.completed` gave, and what
    the nudge retry keys on. `error` comes from `turn_aborted`, the only terminal
    failure record observed in the local corpus.

    `usage` is a fallback here rather than the source of truth: `run` diffs the
    rollout's cumulative `total_token_usage` across the call, which is what makes
    a resumed thread's numbers per-turn instead of running totals.
    """
    thread_id: str | None = None
    body = ""
    last_assistant_text = ""
    usage: dict = {}
    error: str | None = None
    completed = False
    tool_counts: dict[str, int] = {}
    for record in records:
        kind = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind == "session_meta":
            thread_id = payload.get("session_id") or thread_id
            continue
        if kind == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                text = _rollout_content_text(payload.get("content"))
                # A lone <suggestions> block is the protocol token the prompt
                # asked for, not something the agent said. Keeping the last real
                # text is what lets a block-only reply fall back to it.
                if strip_suggestions_blocks(text).strip():
                    last_assistant_text = text
            continue
        if kind != "event_msg":
            continue
        sub = payload.get("type")
        if sub == "item_completed":
            item = payload.get("item") or {}
            item_type = item.get("type")
            if item_type and item_type not in _NON_TOOL_ROLLOUT_ITEMS:
                tool_counts[item_type] = tool_counts.get(item_type, 0) + 1
        elif sub == "token_count":
            usage = (payload.get("info") or {}).get("last_token_usage") or usage
        elif sub == "task_complete":
            completed = True
            body = payload.get("last_agent_message") or body
        elif sub == "turn_aborted":
            error = payload.get("reason") or "codex turn aborted"
    return {
        "thread_id": thread_id,
        "body": body,
        "last_assistant_text": last_assistant_text,
        "usage": usage,
        "error": error,
        "completed": completed,
        "tool_counts": tool_counts,
    }


def read_rollout(path: Path, offset: int) -> tuple[list[dict], int]:
    """Records appended to `path` after `offset`, and where reading stopped.

    Byte offsets rather than a line count because the rollout is append-only
    across `codex exec resume`: a resumed thread's file already holds every
    previous turn, and parsing from the top would fold an old turn's tool counts
    and final message into this one.

    A trailing partial line is left unread — the offset stops before it — so a
    follower polling a file codex is still writing never sees half a record.
    """
    records: list[dict] = []
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return records, offset
    consumed = 0
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break
        consumed += len(raw)
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("codex: skipping malformed rollout line: %s", line[:120])
    return records, offset + consumed


def rollout_size(path: Path | None) -> int:
    """`path`'s current size, or 0 when it does not exist yet.

    0 is right for the missing case rather than an error: a fresh thread has no
    rollout until codex creates one, and everything in it then belongs to the
    turn being started.
    """
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


class _RolloutFollower:
    """Watch one turn's rollout while it runs.

    Two jobs the run itself cannot do: forward the agent's commentary to the
    conversation as it happens, and notice that the turn is finished. Both used
    to come from the `--json` stream. They come from the rollout now, so the
    hosted and unhosted arms behave identically and neither has to read a
    terminal it may not own.

    Locating the file is the awkward part of a first turn: codex names a rollout
    after a thread id it has not told anyone yet, so a new thread is found by the
    cwd in its `session_meta`, and only once codex has written it. Polling is
    therefore for the file as well as for its contents.
    """

    def __init__(
        self,
        workspace: Path,
        thread_id: str | None,
        emit: Callable[[str], None] | None,
    ) -> None:
        self._workspace = workspace
        self._thread_id = thread_id
        self._emit = emit
        self._path = transcript._find_codex_rollout(thread_id) if thread_id else None
        # Everything already in a resumed thread's file belongs to earlier turns.
        # Starting at its current size is what keeps their tool counts and their
        # final messages out of this turn's result.
        self._offset = rollout_size(self._path)
        self.records: list[dict] = []
        self.turn_completed = asyncio.Event()
        self.last_output_at = time.monotonic()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._follow())

    async def aclose(self) -> None:
        """Stop polling, then read whatever landed after the last poll.

        The final read is not optional: codex writes `task_complete` and exits,
        so the last records routinely arrive between two polls, and they are the
        ones carrying the reply.
        """
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._drain()

    def _resolve(self) -> None:
        if self._path is not None:
            return
        found = (
            transcript._find_codex_rollout(self._thread_id)
            if self._thread_id
            # Resolved, because the comparison is a string equality against the
            # cwd codex recorded, and codex records the path it resolved. On
            # macOS a workspace under /tmp is written as /private/tmp/..., so the
            # unresolved name matches nothing and the turn never resolves at all.
            else transcript._find_codex_rollout_by_cwd(
                os.path.realpath(self._workspace)
            )
        )
        if found is not None:
            self._path = found
            # Only a thread with no id starts at the top. A resumed one already
            # holds every earlier turn, so reading from 0 folds their tool counts
            # and their last reply into this turn -- and fires turn_completed on
            # a task_complete that happened before this turn began. Starting at
            # the current size can miss a few of this turn's opening records; it
            # cannot invent a finished turn.
            self._offset = 0 if not self._thread_id else rollout_size(found)

    def _drain(self) -> None:
        """Read new records once, feeding progress and completion. Never raises."""
        self._resolve()
        if self._path is None:
            return
        try:
            records, self._offset = read_rollout(self._path, self._offset)
        except Exception:
            logger.exception("codex: could not read the rollout")
            return
        if not records:
            return
        self.last_output_at = time.monotonic()
        self.records.extend(records)
        for record in records:
            self._observe(record)

    def _observe(self, record: dict) -> None:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "task_complete":
            self.turn_completed.set()
            return
        if payload.get("type") != "item_completed":
            return
        item = payload.get("item") or {}
        if item.get("type") != "AgentMessage":
            return
        # The closing reply is delivered as the turn's answer, so forwarding it
        # here too would post it twice. Commentary is the part nobody would
        # otherwise see until the turn ended.
        if item.get("phase") == _PHASE_FINAL:
            return
        text = _rollout_content_text(item.get("content")).strip()
        if not text or self._emit is None:
            return
        try:
            self._emit(text)
        except Exception:
            # Progress is best effort. A broken frontend must not abort the turn.
            logger.exception("codex: progress relay failed")

    async def _follow(self) -> None:
        while True:
            self._drain()
            await asyncio.sleep(ROLLOUT_POLL_S)


def _ensure_workspace_trusted(workspace: Path, codex_home: Path) -> None:
    """Record the workspace as trusted in `codex_home`, so the TUI does not park.

    The interactive binary asks "do you trust the contents of this directory?"
    before it will do anything, and nobody is there to answer: the turn would
    spend its whole timeout on a keystroke that never comes. `codex exec` never
    asks, which is why this is only needed by the hosted arm.

    The key is the **resolved** path. Codex records the directory it actually
    resolved, so on macOS a workspace under `/tmp` is stored as `/private/tmp/...`
    and a stanza written under the unresolved name matches nothing — measured,
    the dialog still appeared.

    Append-only and idempotent: this is codex's own config file, and when session
    scoping is off it is the operator's. Adding a stanza for a directory cotf
    created is the same thing codex would write had somebody answered the dialog.
    """
    real = os.path.realpath(workspace)
    stanza = f'[projects."{real}"]'
    config = codex_home / "config.toml"
    try:
        current = config.read_text()
    except OSError:
        current = ""
    if stanza in current:
        return
    separator = "" if current.endswith("\n") or not current else "\n"
    try:
        with config.open("a") as handle:
            handle.write(f'{separator}\n{stanza}\ntrust_level = "trusted"\n')
    except OSError as exc:
        # Best effort, like every other pane concern: an unwritable CODEX_HOME
        # costs the interactive UI (the turn parks on the trust dialog and the
        # timeout ends it), and raising here would cost the turn outright even
        # where the plain arm would have run it.
        logger.warning(
            "codex: could not record workspace trust in %s (%s)", config, exc
        )
        return
    logger.debug("codex: trusted %s for the interactive pane", real)


async def _wait_for_exit(
    proc: asyncio.subprocess.Process, follower: _RolloutFollower
) -> tuple[bytes, bytes]:
    """Collect codex's output, without being held hostage by a codex that won't exit."""
    watchdog = asyncio.create_task(_kill_once_quiet_after_turn(proc, follower))
    try:
        return await agent.communicate_capped(proc)
    finally:
        watchdog.cancel()


async def _run_codex_plain(
    wrapped: list[str],
    workspace: Path,
    env: dict[str, str],
    follower: _RolloutFollower,
    timeout: float | None,
) -> tuple[int, str]:
    """Run codex as a direct child. Returns `(returncode, stderr text)`.

    The arm taken when tmux is absent, or when the daemon did not host this run.
    Without `--json`, codex writes its closing reply to stdout and its narration
    to stderr — neither is parsed, because the rollout is the machine data. The
    stderr text is kept only to explain a failing exit.
    """
    proc = await asyncio.create_subprocess_exec(
        *wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # DEVNULL, not inherited. `codex exec` appends piped stdin to the prompt
        # as a `<stdin>` block, so a spawn that inherits an open pipe prints
        # "Reading additional input from stdin..." and blocks there until the
        # turn's whole timeout expires (measured, codex 0.151.0; the same is true
        # with `--json`). A supervised daemon is safe because `supervisor.spawn`
        # passes DEVNULL, which is exactly why this never bit in production and
        # bit immediately anywhere else. The prompt is in argv; codex has no
        # business reading this process's stdin.
        stdin=asyncio.subprocess.DEVNULL,
        cwd=workspace,
        start_new_session=True,
        env=env,
    )
    agent.track_agent_process(proc, wrapped)
    try:
        if timeout is not None:
            _, stderr_bytes = await asyncio.wait_for(
                _wait_for_exit(proc, follower), timeout=timeout
            )
        else:
            _, stderr_bytes = await _wait_for_exit(proc, follower)
    except TimeoutError:
        logger.warning("codex exec: timed out after %ss", timeout)
        raise RuntimeError(f"Codex CLI timed out after {timeout}s") from None
    finally:
        await agent._kill_process_tree(proc)
    return (
        proc.returncode if proc.returncode is not None else -1,
        stderr_bytes.decode(errors="replace").strip(),
    )


def _pane_env_file(env: dict[str, str], path: Path) -> Path | None:
    """Write the curated env for a pane to sourceable 0600 file. None on failure.

    A pane on a tmux server that was already running does not see the client's
    environment at all (measured on tmux 3.7c), and cotf's panes now share one
    server, so `env=` on the `new-session` client reaches the pane only for
    whichever turn happened to start the server. The obvious fix, `-e KEY=VALUE`
    per pair, is not available here: it would put `COTF_CMD_TOKEN` into a command
    line any local `ps` can read, and that token reaches credentialed CLIs
    *outside* the jail.

    So the pane sources a file instead, which is what `claude-pty` has always
    done. 0600, daemon-owned, and named per turn (`tmux.turn_file`) rather than in
    the agent-writable workspace. The agent can read its own file, which is no new
    exposure -- it is the agent's own environment, and it is about to have it.
    Another turn reading it would be, which is why the name carries the session.

    Only the keys cotf actually set are written. Forwarding all of `os.environ`
    would be hundreds of pairs, and the ones that matter are exactly the ones
    that differ from what the daemon inherited.
    """
    curated = {
        key: value
        for key, value in env.items()
        if os.environ.get(key) != value and "\0" not in key
    }
    try:
        # Opened through os.open so the mode is set at creation: a write_text
        # followed by chmod leaves a window where the token is world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for key, value in sorted(curated.items()):
                handle.write(f"export {key}={shlex.quote(value)}\n")
    except OSError as exc:
        logger.warning(
            "codex exec: could not stage the pane env (%s); the pane runs with "
            "whatever the tmux server inherited",
            exc,
        )
        return None
    return path


# Names of the tmux server a daemon was started under. Dropped from every spawn
# env: they would aim claude-pty at the operator's server, and nothing cotf runs
# needs them (see `tmux.argv_prefix`).
_INHERITED_TMUX = frozenset({"TMUX", "TMUX_PANE"})


class _PaneUnavailable(Exception):
    """The pane did not land on cotf's server, so this turn runs unhosted.

    Private and caught one frame up. Not a `RuntimeError`: the caller has a
    working fallback in `codex exec`, so this must not travel the same path as a
    failure that costs the turn.
    """


async def _run_codex_in_pane(
    pane: tmux.Pane,
    argv: list[str],
    workspace: Path,
    env: dict[str, str],
    follower: _RolloutFollower,
    timeout: float | None,
) -> tuple[int, str]:
    """Run codex's interactive TUI inside its pane. Returns `(returncode, detail)`.

    The pane runs the **interactive** binary, not `codex exec`. That is the whole
    point of hosting it: exec is non-interactive by definition, so a mirror of it
    shows plain lines rather than the agent's screen, and the pane reads as dead
    next to claude-pty's. Interactive codex draws its own UI, and the mirror shows
    exactly what somebody sitting at the terminal would see.

    Two things follow from that, and they are why this arm is not just a different
    argv:

    - **It never exits.** The TUI returns to its prompt when the turn is done and
      waits for the next one. So the end of a turn is read from the rollout
      (`task_complete`, via the follower) rather than from a process exit, and the
      session is killed once it lands. A `tmux wait-for` on the command finishing
      would never fire here, which is why this polls rather than blocks.
    - **There is no exit code.** A turn that completed is a success by definition,
      because the reply is already in the rollout. A pane that vanished before
      completing is the failure, and the tap explains it.

    Every tmux call goes through `tmux.argv_prefix()`. Addressing the server by
    name rather than by `TMUX_TMPDIR` is what stops an inherited `TMUX` -- which a
    daemon started inside the operator's tmux has -- from putting the pane on the
    operator's server while `tmux.alive` looks for it on cotf's, a split that read
    a working agent as a dead pane and left it running unsupervised.
    """
    tmux_argv = tmux.argv_prefix()
    output_path = tmux.turn_file(pane.session, "out")
    channel = f"{pane.session}-start"
    env_file = _pane_env_file(env, tmux.turn_file(pane.session, "env"))
    # The pane blocks on `start` first so the tap below cannot miss codex's
    # opening output, then sources the curated env, then execs. `exec` so the
    # pane's process is codex itself rather than a bash holding it: a
    # `kill-session` reaps the process group either way, but the mirror and any
    # `ps` read straighter without the shell in between.
    inner = f"tmux wait-for {shlex.quote(channel)}; "
    if env_file is not None:
        inner += f". {shlex.quote(str(env_file))}; "
    inner += f"exec {shlex.join(argv)}"
    create = [
        *tmux_argv,
        "new-session",
        "-d",
        "-s",
        pane.session,
        "-c",
        str(workspace),
        # tmux's default for a detached session is 80x24. Starting at the floor
        # the mirror reflows to means the first captured frame is already usable,
        # and the TUI lays itself out for a width worth reading.
        "-x",
        "120",
        "-y",
        str(tmux.MIRROR_MIN_ROWS),
        # bash explicitly rather than whatever /bin/sh is: on a Debian-family
        # host that is dash, and this is the one place the backend depends on a
        # shell it did not choose.
        "bash",
        "-c",
        inner,
    ]
    create_proc = await asyncio.create_subprocess_exec(
        *create,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
        env=env,
    )
    _, create_err = await create_proc.communicate()
    if create_proc.returncode != 0:
        detail = (create_err or b"").decode(errors="replace").strip()
        raise RuntimeError(f"tmux refused to host the codex turn: {detail}")

    # A zero exit from `new-session` is not proof the session landed on our
    # server: tmux reports success for a session it created somewhere else. Ask
    # the address we will poll, so a mismatch degrades to an unhosted turn here
    # instead of being read as a dead pane forty lines below. That misread is the
    # bug this check exists for -- it reported `Exit code -1` on turns whose agent
    # was still running, and left them running.
    if not await asyncio.to_thread(tmux.alive, pane):
        logger.warning(
            "codex exec: the session did not land on %s, so this turn runs "
            "unmirrored; check for TMUX in the daemon's environment",
            tmux.socket_path(),
        )
        raise _PaneUnavailable

    # Tap the pane, then release it. A failed tap costs the failure detail, not
    # the run, so it is logged and released anyway rather than wedging the pane.
    tap = await asyncio.create_subprocess_exec(
        *tmux_argv,
        "pipe-pane",
        "-t",
        pane.session,
        f"cat >> {shlex.quote(str(output_path))}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, tap_err = await tap.communicate()
    if tap.returncode != 0:
        logger.warning(
            "codex exec: could not tap the pane (%s); a failure will have no detail",
            (tap_err or b"").decode(errors="replace").strip(),
        )
    release = await asyncio.create_subprocess_exec(
        *tmux_argv,
        "wait-for",
        "-S",
        channel,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    await release.wait()

    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            if follower.turn_completed.is_set():
                # End the session here, not at the end of the run. The TUI does
                # not exit on its own, and a nudge retry opens a session with this
                # same name -- `tmux new-session` refuses a duplicate, so leaving
                # it alive turned every retry into "tmux refused to host the codex
                # turn".
                tmux.kill(pane)
                return 0, ""
            if not await asyncio.to_thread(tmux.alive, pane):
                # The TUI went away without finishing. Whatever it printed on the
                # way out is the only account of why.
                return -1, _pane_output_tail(output_path)
            if deadline is not None and time.monotonic() > deadline:
                logger.warning("codex exec: timed out after %ss", timeout)
                raise RuntimeError(f"Codex CLI timed out after {timeout}s")
            await asyncio.sleep(_PANE_POLL_S)
    finally:
        # The env file holds this turn's `COTF_CMD_TOKEN`, so it goes as soon as
        # the pane can no longer need it -- on the timeout and the crash paths
        # too, not just the clean one. The tap output goes with it: one file per
        # turn accumulates otherwise, and `_pane_output_tail` has already read
        # whatever the caller is about to report.
        if env_file is not None:
            with contextlib.suppress(OSError):
                env_file.unlink()


def _pane_output_tail(path: Path) -> str:
    """The last rows of what the pane printed, for explaining a failing exit.

    A whole turn's terminal output is not an error message, so only the tail is
    kept. Read as text with escapes stripped, because this ends up in a chat
    message rather than in a terminal.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    rows = tmux.trim_trailing_blank_rows(text).splitlines()
    return "\n".join(rows[-_PANE_ERROR_ROWS:]).strip()


async def _run_codex_exec(
    workspace: Path,
    cmd: list[str],
    timeout: float | None,
    thread_id: str | None = None,
    interactive: list[str] | None = None,
) -> dict:
    """Run one codex turn and return its result. Raises RuntimeError on failure.

    Two arms, one result. When the daemon hosted this run in a tmux pane, codex
    runs inside it so the pane mirrors the real terminal; otherwise it runs as a
    direct child. Both read the turn from the rollout through the same parser,
    which is what keeps a hosted turn and an unhosted one from reporting
    differently.

    `thread_id` is codex's own id for a resumed thread, and only decides where
    the follower starts reading. A fresh thread has none until codex writes one.

    `interactive` is the argv for the hosted arm. It is a second argv rather than
    a flag because the two arms run different programs: `codex exec` for a plain
    child, and the interactive binary for a pane worth mirroring. Omitting it
    (compaction does) keeps a run on the plain arm even where a pane exists.
    """
    # Before the wrap: the jail grants this path by name, and on Linux it is a
    # mount source, which has to exist. Set on the child rather than through a
    # session override so every spawn path gets it -- the jobs and cron daemons
    # never open a session, and a codex turn there would otherwise write its
    # rollout into the shared tree that the jail no longer grants.
    codex_home = codex_state.ensure_home(workspace)
    wrapped = sandbox.wrap(cmd, workspace)
    logger.debug("codex exec: cwd=%s cmd=%s", workspace, " ".join(wrapped[:8]) + "...")
    curated = sandbox.agent_env()
    # `TMUX` names a server, and a tmux client obeys it over every other hint. A
    # daemon started from inside the operator's tmux carries it into every child,
    # so forwarding it sends claude-pty -- which takes no `-S` and can only be
    # aimed with `TMUX_TMPDIR` -- to the operator's server instead of cotf's.
    # cotf's own calls use `tmux.argv_prefix()` and are already immune.
    env: dict[str, str] = {
        key: value
        for key, value in (os.environ if curated is None else curated).items()
        if key not in _INHERITED_TMUX
    }
    env["CODEX_HOME"] = str(codex_home)
    pane = tmux.pane_from_env(env) if interactive else None
    if interactive and pane is None:
        logger.info(
            "codex pty mode needs a hosted pane and this turn has none, so it "
            "runs `codex exec`; install tmux, or check agent.pane"
        )
    if pane is not None:
        # Before the spawn: the interactive TUI will not act until the directory
        # is trusted, and nobody is watching the pane to answer it.
        _ensure_workspace_trusted(workspace, codex_home)

    logger.debug(
        "codex exec: %s",
        f"hosted in pane {pane.session}" if pane is not None else "not hosted",
    )
    follower = _RolloutFollower(workspace, thread_id, agent.progress_sink())
    follower.start()
    try:
        hosted = pane is not None and interactive is not None
        if hosted:
            try:
                returncode, stderr_text = await _run_codex_in_pane(
                    pane,
                    sandbox.wrap(interactive, workspace),
                    workspace,
                    env,
                    follower,
                    timeout,
                )
            except _PaneUnavailable:
                hosted = False
        if not hosted:
            returncode, stderr_text = await _run_codex_plain(
                wrapped, workspace, env, follower, timeout
            )
    finally:
        await follower.aclose()

    parsed = parse_codex_rollout(follower.records)
    if parsed.get("error"):
        raise RuntimeError(parsed["error"])
    if returncode != 0:
        # A non-zero exit *after* the turn completed means the turn's work is
        # done and only codex's own teardown failed. That happens: codex can
        # deadlock at exit (main thread parked in pthread_join on a runtime
        # thread that never finishes), leaving a process that has already
        # finished its reply but never exits, until something kills it. Raising
        # here would discard a reply we are holding in `parsed` and post an exit
        # code to the chat instead, so deliver it and log the exit.
        #
        # `body` alone would not be enough to gate on: a turn killed mid-work can
        # leave an intermediate message that reads exactly like a final answer.
        # Only `task_complete` distinguishes "finished, then died" from "died".
        if parsed.get("completed") and parsed.get("body"):
            logger.warning(
                "codex exec: exit %s after task_complete; delivering the "
                "parsed reply anyway. output: %s",
                returncode,
                stderr_text[:500] or "(empty)",
            )
            return parsed
        raise RuntimeError(stderr_text or f"Exit code {returncode}")
    return parsed


def _codex_prompts_dir() -> Path:
    """Codex custom-prompt dir: `$CODEX_HOME/prompts` (defaults to ~/.codex)."""
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home) / "prompts"


_NAMED_ARG_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\$\$|\$([1-9])|\$([A-Za-z_][A-Za-z0-9_]*)")


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML front-matter block (metadata, not prompt body)."""
    if text.startswith("---\n") or text.startswith("---\r\n"):
        return re.sub(r"^---\r?\n.*?\r?\n---\r?\n", "", text, count=1, flags=re.DOTALL)
    return text


def _substitute_placeholders(template: str, args_raw: str) -> str:
    """Fill codex prompt placeholders: $ARGUMENTS, $1..$9, named $NAME (from
    KEY=value args), and $$ for a literal $. Unknown placeholders become empty,
    matching codex's own substitution."""
    try:
        tokens = shlex.split(args_raw)
    except ValueError:
        tokens = args_raw.split()
    positional: list[str] = []
    named: dict[str, str] = {}
    for tok in tokens:
        match = _NAMED_ARG_RE.match(tok)
        if match:
            named[match.group(1)] = match.group(2)
        else:
            positional.append(tok)

    def _replace(match: re.Match) -> str:
        if match.group(0) == "$$":
            return "$"
        if match.group(1):
            idx = int(match.group(1))
            return positional[idx - 1] if idx <= len(positional) else ""
        name = match.group(2)
        if name == "ARGUMENTS":
            return args_raw
        return named.get(name, "")

    return _PLACEHOLDER_RE.sub(_replace, template)


def _expand_codex_prompt(prompt: str) -> str:
    """Expand a `/<name> [args]` codex custom-prompt into its file body.

    `codex exec` (non-interactive) does not expand custom prompts — that is an
    interactive slash-menu feature — so the picker's forwarded `/name` would
    otherwise reach the model as literal text. Read the matching prompt file,
    strip front-matter, substitute placeholders, and return the body. Leaves the
    prompt unchanged when it isn't a slash invocation or has no matching file.
    """
    stripped = prompt.strip()
    if not stripped.startswith("/"):
        return prompt
    name, _, args_raw = stripped[1:].partition(" ")
    if name.startswith("prompts:"):  # accept the namespaced form too
        name = name[len("prompts:") :]
    if not name:
        return prompt
    # Slack controls this name. Keep the documented top-level prompt surface,
    # but never let an absolute, traversing, or symlinked name escape it.
    if name in {".", ".."} or "/" in name or "\\" in name:
        return prompt
    prompts_dir = _codex_prompts_dir()
    try:
        prompts_root = prompts_dir.resolve()
        path = prompts_dir / f"{name}.md"
        if path.resolve().parent != prompts_root:
            return prompt
    except OSError as exc:
        logger.warning("expand: cannot resolve prompt %s: %s", name, exc)
        return prompt
    if not path.is_file():
        return prompt
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("expand: cannot read %s: %s", path, exc)
        return prompt
    return _substitute_placeholders(_strip_frontmatter(template), args_raw.strip())


class CodexBackend:
    """Drives the `codex exec` CLI in non-interactive (`--json`) mode.

    Codex assigns its own `thread_id` and offers no flag to pre-seed it, so
    COTF persists a daemon-owned, workspace-bound mapping after the first turn
    and passes `resume <thread_id>` on follow-ups. The mapping is never read from
    the agent-writable workspace.
    """

    def __init__(
        self,
        launcher: OllamaLauncher | None = None,
        pty: bool = False,
        model: str = "",
        effort: str = "",
    ) -> None:
        self.launcher = launcher
        # Both already resolved by `agent.resolve_profile`, which is also where
        # the ollama/native key routing lives. Binding them here rather than
        # reading settings at argv time is what lets one process run two jobs
        # under different models without them reading each other's.
        self.model = model
        self.effort = effort
        # `pty` picks the interface, not the runtime: the interactive binary
        # rather than `codex exec`. Kept separate from hosting so a break in
        # codex's UI or its trust format costs this backend its TUI and nothing
        # else -- `agent.pane` is global, so using it as the escape hatch would
        # take claude-pty's mirror away too.
        self._pty = pty

    async def run(
        self,
        workspace: Path,
        session_uuid: str,
        prompt: str,
        platform: str,
        user_name: str = "unknown",
        channel_context: str = "dm",
        timeout: float | None = DEFAULT_TIMEOUT,
        nudge_prompt: str | None = None,
    ) -> Response:
        logger.info(
            "session: id=%s platform=%s user=%s context=%s workspace=%s",
            session_uuid,
            platform,
            user_name,
            channel_context,
            workspace,
        )
        # codex exec won't expand a /custom-prompt itself, so do it here.
        prompt = _expand_codex_prompt(prompt)
        existing_thread = codex_state.read_thread_id(workspace, session_uuid)
        # A mapping is only a thread id, and the rollout it names has to be in the
        # home this spawn will use. Turning the session boundary on moves that home,
        # so a mapping written before the change points at a rollout codex cannot
        # see: `resume` then fails the whole turn with "no rollout found for thread
        # id". Adopting the rollout keeps the conversation; when there is none left
        # to adopt, the mapping is dead and the thread starts again with the system
        # prompt and the handoff, which is a forgetful turn instead of a failed one.
        if existing_thread and not codex_state.adopt_rollout(
            workspace, existing_thread
        ):
            logger.warning(
                "codex: thread=%s is not resumable here, starting a new one, "
                "session=%s",
                existing_thread,
                session_uuid,
            )
            codex_state.clear_thread_id(workspace, session_uuid)
            existing_thread = None

        # First codex turn for this session: forward any prior claude history,
        # and prepend the system prompt (codex has no --system-prompt flag, so
        # this is our only way to deliver it). On subsequent turns the system
        # prompt is already in the thread's persisted history — re-sending it
        # every turn inflates tokens (~4.7KB per turn) and bloats context.
        if existing_thread:
            composed_prompt = prompt
        else:
            user_payload = prompt
            if platform not in agent.NO_HANDOFF_PLATFORMS:
                user_payload = transcript.prepend_latest_handoff(
                    workspace, prompt, exclude_uuid=session_uuid
                )
            system_prompt = build_system_prompt(
                platform, user_name, channel_context, workspace
            )
            composed_prompt = f"{system_prompt}\n\n---\n\n{user_payload}"

        # `ollama launch codex` already invokes the codex binary; repeating
        # "codex" after `--` would make it argv[1], which codex treats as the
        # subcommand. Skip the binary when the launcher is set.
        base = self._base_argv(workspace)
        if existing_thread:
            logger.debug(
                "codex: resuming thread=%s session=%s", existing_thread, session_uuid
            )
            cmd = [*base, "resume", existing_thread, composed_prompt]
        else:
            logger.debug("codex: starting new thread session=%s", session_uuid)
            cmd = [*base, composed_prompt]

        # Snapshot the thread's cumulative token usage before invoking codex
        # so we can compute this exec's per-turn delta afterward. For fresh
        # threads there's no prior session, so pre-totals are zero.
        # Pin the file the baseline came from. Reading the pair by thread id
        # alone lets the newest-by-mtime lookup return a different rollout
        # after the exec than before it, and the two do not share a history.
        # Count the per-call usage records this thread already has, and pin the
        # file they were counted in. What the exec appends is its own cost. A
        # before/after subtraction of `total_token_usage` cannot give that:
        # codex writes each call's own figures there, not a running total.
        pre_rollout = (
            transcript.codex_rollout_path(existing_thread) if existing_thread else None
        )
        pre_event_count = (
            len(transcript.extract_codex_usage_events(existing_thread))
            if existing_thread
            else 0
        )

        started_at = time.monotonic()
        result = await _run_codex_exec(
            workspace,
            cmd,
            timeout=timeout,
            thread_id=existing_thread,
            interactive=(
                self._interactive_argv(workspace, existing_thread, composed_prompt)
                if self._pty
                else None
            ),
        )
        duration = time.monotonic() - started_at

        new_thread = result.get("thread_id")
        if not existing_thread and new_thread:
            codex_state.write_thread_id(workspace, session_uuid, new_thread)
            logger.info(
                "codex: persisted new thread=%s for session=%s",
                new_thread,
                session_uuid,
            )

        body = (result.get("body") or "").strip()
        if body and not strip_suggestions_blocks(body).strip():
            # The turn ended with only a <suggestions> block: the protocol
            # token the prompt asked for, not a reply. The agent did say
            # something earlier in the turn, so use the last real text it
            # produced instead of the orchestrator's placeholder. This is
            # still NOT the empty case below — the block is evidence the
            # turn ran to completion, so it is still not nudged.
            last_text = (result.get("last_assistant_text") or "").strip()
            if last_text:
                body = last_text
        if not body:
            # Nothing at all came back: a plausible dead turn, worth one retry.
            #
            # A body that is only a <suggestions> block is NOT this case and is
            # deliberately not retried. That block is the protocol token the
            # turn was asked to end with, so emitting it well-formed is
            # evidence the turn ran to completion and chose to say nothing to
            # the user — an unattended router told not to reply does exactly
            # that. Nudging it re-asks a question already answered: measured
            # across a day of routed alerts the retry changed the reply 0 times
            # out of 11 and cost a second full-context turn each time. The
            # orchestrator turns such a body into its placeholder instead.
            thread_for_retry = result.get("thread_id") or existing_thread
            if thread_for_retry:
                logger.warning(
                    "codex: no visible reply, retrying with nudge, session=%s",
                    session_uuid,
                )
                retry_cmd = [
                    *base,
                    "resume",
                    thread_for_retry,
                    nudge_prompt or NUDGE_PROMPT,
                ]
                retry_started = time.monotonic()
                retry_thread = result.get("thread_id") or existing_thread
                retry_result = await _run_codex_exec(
                    workspace,
                    retry_cmd,
                    timeout=timeout,
                    thread_id=retry_thread,
                    interactive=(
                        self._interactive_argv(
                            workspace, retry_thread, nudge_prompt or NUDGE_PROMPT
                        )
                        if self._pty
                        else None
                    ),
                )
                duration += time.monotonic() - retry_started
                result = _merge_codex_results(result, retry_result)
                body = (result.get("body") or "").strip() or "No response"
            else:
                body = "No response"

        # Prefer the model codex actually recorded in its session file; fall
        # back to whatever the profile resolved. With no model configured that
        # is the literal "codex", which is uninformative — the session-file
        # lookup gives us the real name.
        configured_label = (
            self.launcher.model if self.launcher else (self.model or "codex")
        )
        thread_for_lookup = result.get("thread_id") or existing_thread
        model_label = (
            transcript.extract_codex_model(thread_for_lookup)
            if thread_for_lookup
            else None
        ) or configured_label
        post_rollout = (
            transcript.codex_rollout_path(thread_for_lookup)
            if thread_for_lookup
            else None
        )
        events = (
            transcript.extract_codex_usage_events(thread_for_lookup)
            if thread_for_lookup
            else []
        )
        # A rollout that moved under the turn makes the earlier count refer to
        # another file's history, so only this exec's own last record is safe.
        start = pre_event_count if pre_rollout == post_rollout else len(events) - 1
        fresh = events[max(start, 0) :]
        usage = _usage_from_events(fresh) if fresh else (result.get("usage") or {})
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0) + usage.get(
            "reasoning_output_tokens", 0
        )
        billable_in, billable_out, cache_read, cache_write = _billable_usage(usage)
        # Codex CLI doesn't emit cost; look it up from a price table off-thread
        # so the rare fetch never blocks the event loop. None coalesces to 0.
        computed_cost = (
            await asyncio.to_thread(
                pricing.cost_for,
                model_label,
                billable_in,
                billable_out,
                cache_read,
                cache_write,
            )
            or 0
        )
        # The reading the auto-compact gate thresholds on. Codex reports it in
        # the rollout rather than on stdout: `last_token_usage.input_tokens` is
        # this turn's own prompt, and `model_context_window` the window it has to
        # fit in. Absent (no rollout yet) leaves both None, which reads
        # downstream as "no reading" rather than as an empty context.
        context: dict = {}
        if thread_for_lookup:
            reading = transcript.extract_codex_prompt_tokens(thread_for_lookup)
            if reading and reading[1]:
                context = {
                    "context_tokens": reading[0],
                    "context_window_size": reading[1],
                }

        return Response(
            body=body,
            cost=computed_cost,
            duration=duration,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model_label,
            tool_counts=result.get("tool_counts", {}),
            skill_counts={},
            **context,
        )

    def _base_argv(self, workspace: Path) -> list[str]:
        """`codex exec` argv up to (not including) the prompt or `resume`."""
        # `ollama launch codex` already invokes the codex binary; repeating
        # "codex" after `--` would make it argv[1], which codex treats as the
        # subcommand. Skip the binary when the launcher is set.
        prefix = self.launcher.prefix("codex") if self.launcher else []
        binary = [] if self.launcher else ["codex"]
        model_args = [] if self.launcher else (["-m", self.model] if self.model else [])
        effort_args = self._effort_args()
        # --yolo stays whether approvals are on or not. codex exec overrides
        # approval_policy to `never` regardless (measured: request untrusted, get
        # never), so there is no CLI-side gate to leave enabled -- the PreToolUse
        # hook in permissions.codex_argv is the entire gate.
        return [
            *prefix,
            *binary,
            "exec",
            # No `--json`, deliberately. It puts the same events on stdout that
            # the rollout already records, and renders JSONL into the terminal —
            # which is the terminal the tmux mirror shows. The rollout costs
            # nothing to read and leaves the pane human-readable.
            "--skip-git-repo-check",
            "--yolo",
            *permissions.codex_argv(),
            "-C",
            str(workspace),
            *model_args,
            *effort_args,
        ]

    def _effort_args(self) -> list[str]:
        """`-c model_reasoning_effort=...`, or [] when no effort is configured.

        Unset means inherit: no `-c` is passed and codex reads
        model_reasoning_effort from ~/.codex/config.toml exactly as it did
        before this setting existed. Quoted as TOML per the `-c` contract.

        Responses-API-only in codex, so under ollama it reaches the model only
        when that endpoint honors it -- harmless either way. Under ollama the
        value came from the shared `ollama.effort`, whose accepted levels differ
        from codex's (claude has no `minimal`), so a value codex doesn't accept
        is skipped rather than passed through to die in codex's own config
        parse. A native value takes the same check: a typo in config.yaml should
        warn here rather than fail the turn.

        Shared by both argv builders because `-c` is a global codex option, not
        an `exec` one. The interactive entry point honours it: driven under tmux
        with `-c model_reasoning_effort="low"` against a config.toml saying
        `medium`, codex's own statusline reads `gpt-5.6-luna low` and its
        rollout records `effort: low`.
        """
        effort = self.effort
        if effort and effort not in _CODEX_EFFORT_LEVELS:
            logger.warning(
                "codex: ignoring unknown effort %r "
                "(minimal|low|medium|high|xhigh|max|ultra)",
                effort,
            )
            return []
        return ["-c", f'model_reasoning_effort="{effort}"'] if effort else []

    def _interactive_argv(
        self, workspace: Path, thread_id: str | None, prompt: str
    ) -> list[str]:
        """Argv for the interactive binary, which is what a hosted turn runs.

        Not `_base_argv` with a flag removed: the interactive entry point takes
        no `exec` subcommand and no `--skip-git-repo-check`, and it resumes with
        `codex resume <id>` rather than `codex exec resume <id>`. Autonomy comes
        from `--dangerously-bypass-approvals-and-sandbox` rather than `--yolo`,
        because that is the spelling both the interactive entry point and
        `resume` document; `--yolo` is undocumented on `resume`.
        """
        prefix = self.launcher.prefix("codex") if self.launcher else []
        binary = [] if self.launcher else ["codex"]
        model_args = [] if self.launcher else (["-m", self.model] if self.model else [])
        flags = [
            "--dangerously-bypass-approvals-and-sandbox",
            *permissions.codex_argv(),
            "-C",
            str(workspace),
            *model_args,
            *self._effort_args(),
        ]
        if thread_id:
            return [*prefix, *binary, "resume", *flags, thread_id, prompt]
        return [*prefix, *binary, *flags, prompt]

    def _thread_id(self, workspace: Path, session_uuid: str) -> str | None:
        """Codex's own thread id for one of our sessions, or None if unmapped."""
        return codex_state.read_thread_id(workspace, session_uuid)

    async def compact(
        self,
        workspace: Path,
        session_uuid: str,
        timeout: float | None = DEFAULT_TIMEOUT,
    ) -> Compaction | None:
        """Compact the thread by forcing codex's own auto-compaction to fire.

        Codex has two compaction paths and neither is a command we can send.
        `thread/compact/start` belongs to the app-server protocol, which `exec`
        doesn't speak. And typing `/compact` as a prompt is worse than useless:
        `exec` *recognizes* it (a bogus slash command answers "Unknown command")
        and replies "Context compacted." — but the context does not change.
        Measured across a real thread, 45,730 → 46,335 → 46,357 tokens, and the
        rollout gains none of the compaction bookkeeping the binary defines. The
        turn compacts in memory and `exec` exits without writing it back, so the
        next `resume` rebuilds from an untouched rollout.

        What does work is the threshold codex checks for itself before a turn:
        `run_auto_compact` lives in exec's own code path. Passing a limit far
        below the current context via `-c` makes that check fire on this turn
        only, leaving the user's `~/.codex/config.toml` alone. Same thread, that
        took 46,357 → 18,507 and it survived later plain resumes.

        The cost is that codex compaction needs a turn to hang off, so it spends
        one cheap exchange where claude spends none.
        """
        thread_id = self._thread_id(workspace, session_uuid)
        if thread_id is None:
            logger.info("compact: no codex thread for %s yet", session_uuid)
            return Compaction(
                ok=False,
                error="this thread has no session yet, so there is nothing to compact",
            )

        before = transcript.extract_codex_prompt_tokens(thread_id)
        argv = [
            *self._base_argv(workspace),
            "-c",
            f"model_auto_compact_token_limit={COMPACT_TOKEN_LIMIT}",
            "resume",
            thread_id,
            COMPACT_TRIGGER_PROMPT,
        ]
        logger.info("compact: codex thread=%s", thread_id)
        await _run_codex_exec(workspace, argv, timeout=timeout, thread_id=thread_id)
        after = transcript.extract_codex_prompt_tokens(thread_id)

        if before is None or after is None:
            return Compaction(ok=False, error="couldn't read the thread's token usage")
        pre_tokens, _ = before
        post_tokens, _ = after
        if post_tokens >= pre_tokens:
            # No in-band signal exists, so the token count is the only evidence.
            # A context that didn't shrink means the threshold found nothing worth
            # summarizing — reporting success off the trigger alone would be the
            # same lie `/compact` tells.
            logger.info(
                "compact: codex context did not shrink (%s → %s)",
                pre_tokens,
                post_tokens,
            )
            return Compaction(ok=False, error="nothing to compact")
        return Compaction(ok=True, pre_tokens=pre_tokens, post_tokens=post_tokens)

    def takeover_command(self, workspace: Path, session_uuid: str) -> str | None:
        """`codex resume <thread_id>` when a thread mapping exists for this uuid."""
        thread_id = codex_state.read_thread_id(workspace, session_uuid)
        if not thread_id:
            return None
        return f"codex resume {shlex.quote(thread_id)}"

    def session_log_path(self, workspace: Path, session_uuid: str) -> Path | None:
        """The rollout JSONL codex appends to, resolved via our session mapping.

        Codex names rollouts by its own thread id, not our session_uuid, so we
        read the thread id from the daemon-owned mapping and let the
        transcript helper locate the dated rollout file. The watch formatter
        understands codex's message events (role + input_text/output_text)."""
        thread_id = codex_state.read_thread_id(workspace, session_uuid)
        if thread_id:
            rollout = transcript._find_codex_rollout(thread_id)
            if rollout is not None:
                return rollout
        # No mapping yet (first turn, still running): codex only reveals its
        # thread id at the end, so match the rollout it's actively writing by
        # the workspace cwd. Lets the watch pane tail live instead of staying
        # blank until the turn completes.
        return transcript._find_codex_rollout_by_cwd(str(workspace))

    async def list_skills(self) -> list[tuple[str, str]]:
        """List codex custom prompts as (name, description).

        Codex has no CLI to enumerate them, so scan its prompts dir directly:
        `$CODEX_HOME/prompts/*.md` (CODEX_HOME defaults to ~/.codex). The
        filename without .md is the slash name; the description comes from the
        file's YAML front-matter. Empty when the dir is absent.
        """
        prompts_dir = _codex_prompts_dir()
        try:
            files = sorted(p for p in prompts_dir.glob("*.md") if p.is_file())
        except OSError as exc:
            logger.warning("list_skills: cannot read %s: %s", prompts_dir, exc)
            return []
        out: list[tuple[str, str]] = []
        for path in files:
            try:
                meta = agent.parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                meta = {}
            out.append(
                (path.stem, " ".join(str(meta.get("description") or "").split()))
            )
        return out
