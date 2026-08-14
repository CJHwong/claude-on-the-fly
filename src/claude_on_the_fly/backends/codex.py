"""Codex CLI backend."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from collections.abc import Callable
from pathlib import Path

from claude_on_the_fly import (
    agent,
    codex_state,
    permissions,
    pricing,
    sandbox,
    settings,
    transcript,
)
from claude_on_the_fly.agent import (
    DEFAULT_TIMEOUT,
    NUDGE_PROMPT,
    Compaction,
    OllamaLauncher,
    Response,
    build_system_prompt,
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
_TURN_COMPLETED_MARKER = b'"turn.completed"'


class _StreamWatch:
    """Tracks whether the turn finished and when output was last seen."""

    def __init__(self) -> None:
        self.turn_completed = asyncio.Event()
        self.last_output_at = time.monotonic()
        # Enough of the previous chunk to still match a marker split across a
        # chunk boundary, without retaining the whole stream a second time.
        self._carry = b""

    def feed(self, chunk: bytes) -> None:
        """Note output, and whether it completed the turn. Never raises."""
        self.last_output_at = time.monotonic()
        if self.turn_completed.is_set():
            return
        if _TURN_COMPLETED_MARKER in self._carry + chunk:
            self.turn_completed.set()
            self._carry = b""
            return
        self._carry = (self._carry + chunk)[-(len(_TURN_COMPLETED_MARKER) - 1) :]


class _CodexProgressRelay:
    """Hold Codex messages until a tool event proves they were narration."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._pending: list[str] = []

    def feed(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "item.completed":
            item = msg.get("item") or {}
            item_type = item.get("type")
            if item_type == "agent_message":
                text = (item.get("text") or "").strip()
                if text:
                    self._pending.append(text)
            elif self._is_tool_item(item_type):
                self._flush()
        elif kind == "item.started":
            item_type = (msg.get("item") or {}).get("type")
            if self._is_tool_item(item_type):
                self._flush()
        elif kind == "turn.completed":
            # The pending message is the final answer. Response.body will carry
            # it after the stream is parsed, so forwarding it would duplicate it.
            self._pending.clear()

    def finish(self) -> None:
        """Drop any unproven text when the stream ends without another tool."""
        self._pending.clear()

    @staticmethod
    def _is_tool_item(item_type: object) -> bool:
        return (
            isinstance(item_type, str)
            and item_type != "agent_message"
            and item_type not in _NON_TOOL_ITEMS
        )

    def _flush(self) -> None:
        pending, self._pending = self._pending, []
        for text in pending:
            try:
                self._emit(text)
            except Exception:
                # Progress is best effort. A broken frontend must not abort the
                # Codex turn or stop the JSONL reader.
                logger.exception("codex: progress relay failed")


class _CodexStreamObserver:
    """Observe Codex chunks for progress without changing final parsing."""

    def __init__(self, watch: _StreamWatch, emit: Callable[[str], None] | None) -> None:
        self._watch = watch
        self._line_buffer = b""
        self._relay = _CodexProgressRelay(emit) if emit is not None else None

    def feed(self, chunk: bytes) -> None:
        self._watch.feed(chunk)
        relay = self._relay
        if relay is None:
            return
        self._line_buffer += chunk
        while b"\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split(b"\n", 1)
            self._feed_line(line, relay)

    def finish(self) -> None:
        relay = self._relay
        if relay is None:
            return
        if self._line_buffer.strip():
            self._feed_line(self._line_buffer, relay)
        relay.finish()

    def _feed_line(self, raw: bytes, relay: _CodexProgressRelay) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        try:
            relay.feed(msg)
        except Exception:
            # Keep the callback non-raising: it runs inside the bounded stdout
            # reader, where an exception would discard the whole turn.
            logger.exception("codex: progress relay failed")


async def _kill_once_quiet_after_turn(
    proc: asyncio.subprocess.Process, watch: _StreamWatch
) -> None:
    """Kill `proc` once it has been silent for the grace period post-turn."""
    await watch.turn_completed.wait()
    while True:
        idle = time.monotonic() - watch.last_output_at
        if idle >= POST_TURN_EXIT_GRACE:
            break
        # Re-armed by any further output, so a second turn in one exec
        # (auto-compaction) is never cut short.
        await asyncio.sleep(POST_TURN_EXIT_GRACE - idle)
    logger.warning(
        "codex exec: silent for %ss after turn.completed and still running; "
        "killing it so the finished reply can be delivered",
        POST_TURN_EXIT_GRACE,
    )
    await agent._kill_process_tree(proc)


async def _collect_codex_output(proc) -> tuple[bytes, bytes]:
    """`communicate_capped`, but not hostage to a codex that won't exit."""
    watch = _StreamWatch()
    observer = _CodexStreamObserver(watch, agent.progress_sink())
    watchdog = asyncio.create_task(_kill_once_quiet_after_turn(proc, watch))
    try:
        return await agent.communicate_capped(proc, on_stdout_chunk=observer.feed)
    finally:
        observer.finish()
        watchdog.cancel()


# `model_reasoning_effort` choices, from codex's config reference. The shared
# OLLAMA_EFFORT setting is validated against this before it reaches codex
# (claude's accepted set differs: no `minimal`, plus `max`).
_CODEX_EFFORT_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh"})


# item.completed types that are not tool calls. "reasoning" never was; "error"
# joined the list when permissions mode `ask` started passing
# --dangerously-bypass-hook-trust, which codex announces as an error item -- and
# counting it produced a phantom tool named "error" in the response footer of every
# gated codex turn.
_NON_TOOL_ITEMS = frozenset({"reasoning", "error"})


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


def _usage_delta(current: dict, previous: dict | None) -> dict:
    """Return one turn's counts from Codex's cumulative usage snapshots."""
    if previous is None:
        return dict(current)
    return {
        field: current.get(field, 0) - previous.get(field, 0)
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


def parse_codex_stream(stdout: bytes) -> dict:
    """Parse the JSONL emitted by `codex exec --json`.

    Returns a dict with `thread_id`, `body`, `usage`, `completed`,
    `tool_counts`, and `error` keys. `error` is set only when codex emits
    `turn.failed`. `completed` says codex emitted `turn.completed`, which it
    does after the turn's final `agent_message` — the only in-band proof that
    a `body` is the whole reply rather than one the stream was cut short of.
    """
    thread_id: str | None = None
    body = ""
    usage: dict = {}
    error: str | None = None
    completed = False
    tool_counts: dict[str, int] = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("codex: skipping malformed line: %s", line[:120])
            continue
        kind = msg.get("type")
        if kind == "thread.started":
            thread_id = msg.get("thread_id")
        elif kind == "item.completed":
            item = msg.get("item") or {}
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text") or ""
                if text:
                    body = text
            elif item_type and item_type not in _NON_TOOL_ITEMS:
                tool_counts[item_type] = tool_counts.get(item_type, 0) + 1
        elif kind == "turn.completed":
            usage = msg.get("usage") or {}
            completed = True
        elif kind == "turn.failed":
            error = (msg.get("error") or {}).get("message") or "codex turn failed"
        # "error" events are reconnect noise; only turn.failed is terminal
    return {
        "thread_id": thread_id,
        "body": body,
        "usage": usage,
        "error": error,
        "completed": completed,
        "tool_counts": tool_counts,
    }


async def _run_codex_exec(
    workspace: Path, cmd: list[str], timeout: float | None
) -> dict:
    """Run codex, collect stdout, parse JSONL. Raises RuntimeError on failure."""
    # Before the wrap: the jail grants this path by name, and on Linux it is a
    # mount source, which has to exist. Set on the child rather than through a
    # session override so every spawn path gets it -- the jobs and cron daemons
    # never open a session, and a codex turn there would otherwise write its
    # rollout into the shared tree that the jail no longer grants.
    codex_home = codex_state.ensure_home(workspace)
    cmd = sandbox.wrap(cmd, workspace)
    logger.debug("codex exec: cwd=%s cmd=%s", workspace, " ".join(cmd[:8]) + "...")
    env = sandbox.agent_env()
    env = {**(os.environ if env is None else env), "CODEX_HOME": str(codex_home)}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
        start_new_session=True,
        env=env,
    )
    agent.track_agent_process(proc, cmd)
    try:
        if timeout is not None:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                _collect_codex_output(proc), timeout=timeout
            )
        else:
            stdout_bytes, stderr_bytes = await _collect_codex_output(proc)
    except TimeoutError:
        logger.warning("codex exec: timed out after %ss", timeout)
        raise RuntimeError(f"Codex CLI timed out after {timeout}s") from None
    finally:
        await agent._kill_process_tree(proc)

    parsed = parse_codex_stream(stdout_bytes)
    if parsed.get("error"):
        raise RuntimeError(parsed["error"])
    if proc.returncode != 0:
        stderr_text = stderr_bytes.decode(errors="replace").strip()
        # A non-zero exit *after* `turn.completed` means the turn's work is done
        # and only codex's own teardown failed. That happens: codex can deadlock
        # at exit (main thread parked in pthread_join on a runtime thread that
        # never finishes), leaving a process that has already written its
        # complete reply to stdout but never exits, until something kills it.
        # Raising here would discard a reply we are holding in `parsed` and post
        # an exit code to the chat instead, so deliver it and log the exit.
        #
        # `body` alone would not be enough to gate on: codex overwrites it on
        # every agent_message, so a turn killed mid-work leaves an intermediate
        # message sitting there that reads exactly like a final answer. Only
        # `turn.completed` distinguishes "finished, then died" from "died".
        if parsed.get("completed") and parsed.get("body"):
            logger.warning(
                "codex exec: exit %s after turn.completed; delivering the "
                "parsed reply anyway. stderr: %s",
                proc.returncode,
                stderr_text[:500] or "(empty)",
            )
            return parsed
        raise RuntimeError(stderr_text or f"Exit code {proc.returncode}")
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

    def __init__(self, launcher: OllamaLauncher | None = None) -> None:
        self.launcher = launcher

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
        pre_totals = (
            transcript.extract_codex_cumulative_tokens(existing_thread)
            if existing_thread
            else None
        )

        started_at = time.monotonic()
        result = await _run_codex_exec(workspace, cmd, timeout=timeout)
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
                retry_result = await _run_codex_exec(
                    workspace, retry_cmd, timeout=timeout
                )
                duration += time.monotonic() - retry_started
                result = _merge_codex_results(result, retry_result)
                body = (result.get("body") or "").strip() or "No response"
            else:
                body = "No response"

        # Prefer the model codex actually recorded in its session file; fall
        # back to whatever the user configured. In native mode without
        # CODEX_MODEL the configured value is just the literal "codex", which
        # is uninformative — the session-file lookup gives us the real name.
        configured_label = (
            self.launcher.model
            if self.launcher
            else (settings.get("CODEX_MODEL").strip() or "codex")
        )
        thread_for_lookup = result.get("thread_id") or existing_thread
        model_label = (
            transcript.extract_codex_model(thread_for_lookup)
            if thread_for_lookup
            else None
        ) or configured_label
        # Codex's stdout `turn.completed.usage` re-reports the thread's
        # running total on every exec, not per-turn. Diff cumulative totals
        # from the session file to get this exec's true contribution. Falls
        # back to stdout usage when the session file isn't reachable (mocked
        # tests, rare race) — accepted to be cumulative in that path.
        post_totals = (
            transcript.extract_codex_cumulative_tokens(thread_for_lookup)
            if thread_for_lookup
            else None
        )
        if post_totals is not None:
            usage = _usage_delta(post_totals, pre_totals)
        else:
            usage = result.get("usage") or {}
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
        model_env = settings.get("CODEX_MODEL").strip()
        model_args = [] if self.launcher else (["-m", model_env] if model_env else [])
        # Reasoning effort is passed only for the ollama-served model. Native
        # mode inherits the operator's own model_reasoning_effort in
        # ~/.codex/config.toml; an override here would silently trump it. Quoted
        # as TOML per the `-c` contract. Responses-API-only in codex, so it
        # reaches the model only when the ollama endpoint honors it — harmless
        # either way. OLLAMA_EFFORT is shared with the claude backend, whose
        # accepted levels differ (no `minimal`), so a value codex doesn't accept
        # is skipped, not passed through to die in codex's own config parse.
        effort = settings.get("OLLAMA_EFFORT").strip() if self.launcher else ""
        if effort and effort not in _CODEX_EFFORT_LEVELS:
            logger.warning(
                "codex: ignoring unknown effort %r (minimal|low|medium|high|xhigh)",
                effort,
            )
            effort = ""
        effort_args = ["-c", f'model_reasoning_effort="{effort}"'] if effort else []
        # --yolo stays whether approvals are on or not. codex exec overrides
        # approval_policy to `never` regardless (measured: request untrusted, get
        # never), so there is no CLI-side gate to leave enabled -- the PreToolUse
        # hook in permissions.codex_argv is the entire gate.
        return [
            *prefix,
            *binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--yolo",
            *permissions.codex_argv(),
            "-C",
            str(workspace),
            *model_args,
            *effort_args,
        ]

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
        await _run_codex_exec(workspace, argv, timeout=timeout)
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
