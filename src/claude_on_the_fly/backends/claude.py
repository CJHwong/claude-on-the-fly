"""Claude Code CLI backend."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from claude_on_the_fly import agent, checks, permissions, pricing, sandbox, transcript
from claude_on_the_fly.agent import (
    DEFAULT_TIMEOUT,
    Compaction,
    OllamaLauncher,
    Response,
    build_system_prompt,
)
from claude_on_the_fly.transcript import (
    _workspace_to_claude_hash,
)

logger = logging.getLogger(__name__)


PTY_PROJECT_SLUG = "claude-interactive-p"
PTY_INSTALL_HINT = (
    f"curl -fsSL https://raw.githubusercontent.com/CJHwong/"
    f"{PTY_PROJECT_SLUG}/main/install.sh | bash"
)
# claude's own slash command, sent as the prompt. Runs the real compaction and
# writes a `compact_boundary` into the session transcript.
COMPACT_PROMPT = "/compact"
# Cap for a compaction when the caller sets none. The chat frontends all leave
# `timeout_for` at its default of None, which the executors read as "wait with no
# deadline" — fine for a turn a human is watching, wrong for this: a compaction
# is a single summarization pass (measured at 8-22s, minutes on a very large
# thread), and the drain loop is serial per chat, so an unbounded one wedges
# every message queued behind it.
COMPACT_TIMEOUT = 900.0


def _last_compact_boundary(path: Path | None) -> dict:
    """`compactMetadata` from the newest `compact_boundary` in a transcript.

    Streams the file rather than reading it whole: this runs right after a
    compaction, which is exactly when the transcript is at its largest. Empty
    dict when the file is unreadable or has never been compacted.
    """
    if path is None:
        return {}
    found: dict = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                # Cheap reject before paying for json.loads on every record.
                if '"compact_boundary"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("subtype") == "compact_boundary":
                    found = record.get("compactMetadata") or {}
    except OSError as exc:
        logger.warning("compact: could not read %s: %s", path, exc)
    return found


def _native_context_fields(cli_output: dict) -> dict:
    """Prompt size and model window from a `claude -p` result envelope.

    The pty statusline hands these over ready-made; native has to add them up,
    which is the only reason this looks like arithmetic. All three usage terms
    count: `tokens_in` deliberately omits `cache_creation_input_tokens` (it
    measures what a turn read), but a cold cache puts most of the prompt in
    exactly that term — 22k of 40k on a real turn here — so an auto-compact gate
    built on `tokens_in` would under-read the prompt by about half in the case it
    exists to catch.

    `modelUsage` lists every model a turn touched, sub-agents included. The
    top-level `usage` is the main session's, so pair it with the widest window
    listed: a sub-agent's smaller one would overstate how full the context is,
    and for a feature that spends money to act, over-reading is the costly
    direction. Empty dict when either number is missing, which reads downstream
    as "no reading" rather than as zero.
    """
    usage = cli_output.get("usage") or {}
    tokens = (
        int(usage.get("input_tokens", 0))
        + int(usage.get("cache_read_input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0))
    )
    windows = [
        int(entry.get("contextWindow", 0))
        for entry in (cli_output.get("modelUsage") or {}).values()
        if isinstance(entry, dict) and entry.get("contextWindow")
    ]
    if not tokens or not windows:
        return {}
    return {"context_tokens": tokens, "context_window_size": max(windows)}


def _billable_usage(cli_output: dict) -> tuple[int, int, int, int]:
    """`(input, output, cache_read, cache_write)` — non-overlapping, for pricing.

    Deliberately not derived from `_extract_tokens`: its `tokens_in` folds cache
    reads into one display figure (the footer's `↑72.0k`, which is what a reader
    wants), and `pricing.cost_for` needs the buckets kept apart so each is billed
    at its own rate. Two jobs that happen to read the same `usage` block.
    """
    usage = cli_output.get("usage") or {}
    return (
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        int(usage.get("cache_read_input_tokens", 0)),
        int(usage.get("cache_creation_input_tokens", 0)),
    )


def _compaction_from(cli_output: dict, session_path: Path | None) -> Compaction:
    """Read a compaction's outcome out of a finished `-p` run.

    The stream's `compact_result` decides success; the transcript's boundary
    supplies the numbers. A claude build that stops emitting those status events
    would land here as a failure with an empty error — the compaction still
    happened, only the report would be wrong.
    """
    compact = cli_output.get("compact") or {}
    if compact.get("result") != "success":
        error = compact.get("error") or (cli_output.get("result") or "").strip()
        return Compaction(ok=False, error=error)
    meta = _last_compact_boundary(session_path)
    return Compaction(
        ok=True,
        pre_tokens=int(meta.get("preTokens", 0)),
        post_tokens=int(meta.get("postTokens", 0)),
        duration=int(meta.get("durationMs", 0)) / 1000,
    )


def _session_has_content(path: Path) -> bool:
    """True if the session JSONL exists and holds at least one non-blank line.

    The file merely existing is not proof the session was established: a failed
    first turn (the LLM never started) can leave an empty file. When there is
    real content, claude has already persisted the system prompt into the
    session, so a --resume need not re-send it. When there is none, the caller
    must re-supply the system prompt to avoid running the agent prompt-less.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            return any(line.strip() for line in handle)
    except OSError:
        return False


def resolve_pty_binary() -> str | None:
    """Find the `claude-pty` binary.

    Order: PATH → `$CLAUDE_INTERACTIVE_P_HOME/bin/claude-pty` →
    `~/.local/share/{PTY_PROJECT_SLUG}/bin/claude-pty`. Returns the absolute
    path or None.
    """
    on_path = shutil.which("claude-pty")
    if on_path:
        return on_path
    home = os.environ.get("CLAUDE_INTERACTIVE_P_HOME") or str(
        Path.home() / ".local/share" / PTY_PROJECT_SLUG
    )
    candidate = Path(home) / "bin" / "claude-pty"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


async def _probe_skills(
    prefix: list[str], binary: list[str]
) -> tuple[list[str], list[dict]]:
    """Read skill names and active plugins from a print-mode `system/init` event.

    The init line lists every resolvable skill (and the active plugins with
    their paths) and is emitted before the model turn runs, so we read that one
    line and reap the whole process tree before any tokens are spent. Probed
    from $HOME so it reflects the user + plugin skills a workspace inherits
    (workspaces carry no project-local .claude).
    """
    cmd = [
        *prefix,
        *binary,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "warm",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(Path.home()),
        limit=16 * 1024 * 1024,
        start_new_session=True,
    )
    agent.track_agent_process(proc, cmd)
    assert proc.stdout is not None
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
    except TimeoutError:
        logger.warning("list_skills: timed out waiting for init event")
        return [], []
    finally:
        await agent._kill_process_tree(proc)
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError):
        return [], []
    if event.get("type") != "system" or event.get("subtype") != "init":
        return [], []
    names = sorted(str(s) for s in (event.get("skills") or []))
    plugins = [p for p in (event.get("plugins") or []) if isinstance(p, dict)]
    return names, plugins


def _skill_descriptions(plugins: list[dict]) -> dict[str, str]:
    """Map skill name -> one-line description from SKILL.md front-matter.

    Scans the user skills dir ($CLAUDE_CONFIG_DIR/skills, default ~/.claude) and
    each active plugin's skills dir (paths straight from the init event), so it
    covers the reported skills without walking the whole marketplace clone.
    Descriptions the init event doesn't carry, so this is the only source.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    # (plugin_name, skills_root); user skills have no plugin namespace.
    roots: list[tuple[str | None, Path]] = [(None, base / "skills")]
    roots += [
        (p.get("name"), Path(p["path"]) / "skills") for p in plugins if p.get("path")
    ]
    out: dict[str, str] = {}
    for plugin_name, root in roots:
        try:
            skill_files = sorted(root.glob("*/SKILL.md"))
        except OSError:
            continue
        for skill_file in skill_files:
            try:
                meta = agent.parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = str(meta.get("name") or skill_file.parent.name)
            desc = " ".join(str(meta.get("description") or "").split())
            out.setdefault(name, desc)
            # Plugin skills appear in the init list namespaced as plugin:skill.
            if plugin_name:
                out.setdefault(f"{plugin_name}:{name}", desc)
    return out


class ClaudeBackend:
    """Drives the `claude` CLI.

    Three modes, mutually exclusive:
    - native (default): `claude -p --output-format stream-json …`
    - ollama (`launcher` set): wraps with `ollama launch claude --model X --yes --`
    - pty (`pty=True`): drives `claude-pty` (interactive PTY wrapper from
      claude-interactive-p) so we get statusline-only fields like rate_limits
      and context_window. Argv drops `-p`, `--output-format`, `--verbose`.
    """

    def __init__(
        self,
        launcher: OllamaLauncher | None = None,
        pty: bool = False,
    ) -> None:
        if launcher is not None and pty:
            raise ValueError("ClaudeBackend: launcher and pty are mutually exclusive")
        self.launcher = launcher
        self.pty = pty
        # Resolve once at construction so per-message hot path skips the
        # `shutil.which` + `os.access` syscalls. Missing binary fails fast
        # here rather than on the first message — preflight already guarantees
        # it, this is defense in depth for misconfigured callers.
        self._pty_path: str | None = None
        if pty:
            self._pty_path = resolve_pty_binary()
            if self._pty_path is None:
                raise RuntimeError(
                    "claude-pty binary not found. Install with: " + PTY_INSTALL_HINT
                )

    async def run(
        self,
        workspace: Path,
        session_uuid: str,
        prompt: str,
        platform: str,
        user_name: str = "unknown",
        channel_context: str = "dm",
        timeout: float | None = DEFAULT_TIMEOUT,
    ) -> Response:
        logger.info(
            "session: id=%s platform=%s user=%s context=%s workspace=%s",
            session_uuid,
            platform,
            user_name,
            channel_context,
            workspace,
        )
        system_prompt = build_system_prompt(
            platform, user_name, channel_context, workspace
        )
        # --system-prompt is only attached when (re-)establishing a session; a
        # healthy --resume reuses the prompt already persisted in the session.
        sysprompt_args = ["--system-prompt", system_prompt]

        if self.pty:
            base = self._pty_base_argv()
            executor = _exec_pty
        else:
            base = self._native_base_argv()
            executor = agent._exec

        # Pick --resume vs --session-id deterministically by checking whether
        # the session JSONL exists on disk. Old code sniffed claude's error
        # message ("No conversation found") on a failed --resume, but:
        #   1) claude-pty mode wraps stderr behind "no envelope produced (claude
        #      rc=1)", so the sniff never matched in pty mode.
        #   2) claude 2.1.150 changed the message to "--resume requires a
        #      valid session ID...", so the sniff stopped matching in native
        #      mode too.
        # Codex backend already takes the existence-check approach; mirror it
        # here so first-turn dispatches don't crash before the new-session
        # branch can run.
        # Read via the transcript module rather than the imported symbol so
        # tests that monkeypatch CLAUDE_PROJECTS_DIR via the fixture see the
        # redirected value.
        session_path = (
            transcript.CLAUDE_PROJECTS_DIR
            / _workspace_to_claude_hash(workspace)
            / f"{session_uuid}.jsonl"
        )
        if _session_has_content(session_path):
            # Healthy resume: claude already persisted the system prompt into
            # the session, so don't re-send it (cuts tokens and stops every
            # turn re-asserting the whole prompt).
            logger.debug(
                "agent.run: resuming session=%s prompt=%s", session_uuid, prompt[:80]
            )
            argv = [*base, "--resume", session_uuid, prompt]
        elif session_path.is_file():
            # The file exists but has no content: a prior turn opened the
            # session yet the LLM never produced output (empty/synthetic reply).
            # Resume but RE-SUPPLY the system prompt — otherwise the agent runs
            # with no system prompt at all.
            logger.warning(
                "agent.run: session=%s exists but is empty; re-supplying system "
                "prompt on resume",
                session_uuid,
            )
            argv = [*base, *sysprompt_args, "--resume", session_uuid, prompt]
        else:
            logger.info("No existing session %s, creating new", session_uuid)
            if platform not in agent.NO_HANDOFF_PLATFORMS:
                prompt = transcript.prepend_latest_handoff(
                    workspace, prompt, exclude_uuid=session_uuid
                )
            argv = [*base, *sysprompt_args, "--session-id", session_uuid, prompt]
        cli_output = await executor(workspace, argv, timeout=timeout)

        body = (cli_output.get("result") or "").strip()
        if not body and (cli_output.get("compact") or {}).get("result"):
            # A compaction reports `subtype: "success"` with an empty `result`,
            # which is byte-identical to a turn that died producing nothing. The
            # nudge below would then spend a second billed turn asking for a
            # reply that was never owed, and post its answer as if it were one.
            # Reaching here means someone sent "/compact" as ordinary text
            # rather than going through `compact()`; report it and stop.
            logger.info("agent.run: prompt was a compaction, skipping the nudge")
            return Response(
                body=_compaction_from(cli_output, None).summary(),
                cost=cli_output.get("total_cost_usd", 0),
                duration=cli_output.get("duration_ms", 0) / 1000,
            )
        if not body:
            logger.warning(
                "agent.run: empty result, retrying with nudge, session=%s", session_uuid
            )
            retry_output = await executor(
                workspace,
                [*base, "--resume", session_uuid, agent.NUDGE_PROMPT],
                timeout=timeout,
            )
            if self.pty:
                # claude-pty envelopes have no per-tool counts to merge and pty's
                # `usage` is just the last assistant message — simpler to
                # take the retry envelope wholesale.
                cli_output = retry_output
            else:
                cli_output = agent._merge_cli_output(cli_output, retry_output)
            body = (cli_output.get("result") or "").strip() or "No response"

        tokens_in, tokens_out = self._extract_tokens(cli_output)
        model = next(iter(cli_output.get("modelUsage", {})), "")

        # In ollama mode the claude CLI still computes total_cost_usd from
        # Anthropic's price table, which is meaningless when ollama is
        # actually serving the model. Look up the routed model's price in
        # the OpenRouter registry instead, matching how the codex backend
        # handles its own cost. Native and pty modes keep the CLI's value,
        # which reflects Anthropic's real billing.
        # Cached prompt tokens are priced separately, and they are not a rounding
        # error: a thread that goes quiet past the cache TTL re-establishes its
        # whole prompt, so cache *writes* become the largest term on the turn that
        # brings it back. Folding them into the prompt rate — or dropping them, as
        # `tokens_in` does — was 42% out on a measured turn.
        if self.launcher is not None:
            billable_in, billable_out, cache_read, cache_write = _billable_usage(
                cli_output
            )
            cost = (
                await asyncio.to_thread(
                    pricing.cost_for,
                    model,
                    billable_in,
                    billable_out,
                    cache_read,
                    cache_write,
                )
                or 0
            )
        else:
            cost = cli_output.get("total_cost_usd", 0)

        statusline = cli_output.get("statusline") or {}
        extra = _statusline_response_fields(statusline)
        if not self.pty and self.launcher is None:
            # pty already has these from the statusline, and its top-level
            # `usage` is the last assistant message only (see `_extract_tokens`),
            # so deriving them there would understate a multi-turn prompt.
            #
            # Withheld in ollama mode for the same reason the cost above is
            # recomputed there: the claude CLI reports `contextWindow` from its
            # own table even when ollama is serving some other vendor's model, so
            # the figure describes a model that isn't answering (200000 observed
            # for glm-5.2:cloud). Cost has a real substitute in the OpenRouter
            # registry; a context window has none. Reporting nothing leaves the
            # auto-compact gate with no reading, which switches it off in this
            # mode rather than thresholding against a made-up denominator —
            # over-reading is the direction that spends money. `$compact` is
            # unaffected: it is asked for, and the CLI does the real work.
            extra.update(_native_context_fields(cli_output))

        return Response(
            body=body,
            cost=cost,
            duration=cli_output.get("duration_ms", 0) / 1000,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            tool_counts=cli_output.get("tool_counts", {}),
            skill_counts=cli_output.get("skill_counts", {}),
            **extra,
        )

    def _native_base_argv(self) -> list[str]:
        """`claude -p` argv minus the prompt and --system-prompt."""
        # `ollama launch claude` already invokes the claude binary; repeating
        # "claude" after `--` would make it argv[1], which -p mode parses as
        # the prompt and silently drops the real one.
        prefix = self.launcher.prefix("claude") if self.launcher else []
        binary = [] if self.launcher else ["claude"]
        # Empty/unset CLAUDE_MODEL → omit --model and let the claude CLI use
        # its own default (don't pin sonnet).
        model = "" if self.launcher else os.environ.get("CLAUDE_MODEL", "").strip()
        model_args = ["--model", model] if model else []
        # Permission flags rather than a hardcoded bypassPermissions. With
        # approvals off this returns exactly the old pair, so argv is unchanged.
        return [
            *prefix,
            *binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            *permissions.claude_argv(),
            *model_args,
        ]

    async def compact(
        self,
        workspace: Path,
        session_uuid: str,
        timeout: float | None = DEFAULT_TIMEOUT,
    ) -> Compaction | None:
        """Compact the session in place. None when there's no session yet.

        Runs in whichever mode this backend is in — a pty backend compacts
        through claude-pty, not through `claude -p`. Mixing them would forfeit
        what pty is for: an operator can `tmux attach` to a live turn, and a
        compaction is the longest and priciest thing a thread does, so it is the
        worst one to make invisible. The two also resolve their own settings
        (effort, fast mode, output style), so a `-p` compaction could summarize a
        conversation under settings the conversation never ran under.

        pty needs `hooks/postcompact_envelope.sh` from claude-interactive-p to do
        this at all: a compaction produces no assistant message, so pty's usual
        **Stop** hook never fires and its envelope never appears. Without that
        hook the run hangs until `timeout` (an hour by default) — which is what
        `checks.check_pty_hooks` warns about before a daemon ever gets here.
        """
        session_path = self.session_log_path(workspace, session_uuid)
        if session_path is None:
            # Not the same as "this backend can't compact" — claude can, there is
            # simply nothing here yet. Returning None would collapse the two and
            # tell a claude user their backend has no compaction.
            logger.info("compact: no session %s yet, nothing to do", session_uuid)
            return Compaction(
                ok=False,
                error="this thread has no session yet, so there is nothing to compact",
            )
        if self.pty:
            if not checks.pty_postcompact_hook_wired():
                # Refuse in milliseconds rather than wait forever. The frontends
                # pass no timeout, so `_exec_pty` would skip `wait_for` and block
                # this chat's serial drain until someone sent $stop.
                logger.warning("compact: claude-pty has no PostCompact hook, refusing")
                return Compaction(
                    ok=False,
                    error=(
                        "claude-pty can't finish a compaction without its PostCompact "
                        f"hook, so I didn't start one. Update it: {PTY_INSTALL_HINT}"
                    ),
                )
            base, executor = self._pty_base_argv(), _exec_pty
        else:
            base, executor = self._native_base_argv(), agent._exec
        argv = [*base, "--resume", session_uuid, COMPACT_PROMPT]
        # A compaction is one summarization pass, not open-ended agent work, so
        # it gets a cap of its own even when the caller passes None (which the
        # chat frontends all do, and which means "no timeout" downstream).
        deadline = timeout if timeout is not None else COMPACT_TIMEOUT
        logger.info(
            "compact: session=%s pty=%s timeout=%s", session_uuid, self.pty, deadline
        )
        cli_output = await executor(workspace, argv, timeout=deadline)
        result = _compaction_from(cli_output, session_path)
        logger.info(
            "compact: session=%s ok=%s %s→%s tokens",
            session_uuid,
            result.ok,
            result.pre_tokens,
            result.post_tokens,
        )
        return result

    def _pty_base_argv(self) -> list[str]:
        """claude-pty argv minus the prompt and --system-prompt; the caller appends
        --system-prompt only when (re-)establishing a session."""
        assert self._pty_path is not None  # set in __init__ when pty=True
        model = os.environ.get("CLAUDE_MODEL", "").strip()
        model_args = ["--model", model] if model else []
        # claude-pty forwards every flag to claude verbatim, so the same argv
        # works here. What differs is which of them claude honours: interactive
        # mode ignores --permission-prompt-tool and draws its own dialog instead,
        # which is why the pty path also installs the Notification relay.
        return [
            self._pty_path,
            *permissions.claude_argv(pty=True),
            *permissions.pty_argv(),
            *model_args,
        ]

    def _extract_tokens(self, cli_output: dict) -> tuple[int, int]:
        """Return (tokens_in, tokens_out).

        claude-pty's top-level `usage` is the last assistant message only, so for
        multi-turn pty calls we'd undercount. `modelUsage` is aggregated
        across every assistant record by pty's transcript pass, so it's the
        truthful cross-turn total.

        Native/ollama stay on `usage` because that's how stream-json folds
        already work, and we want zero behavior change there.
        """
        if self.pty:
            mu = cli_output.get("modelUsage") or {}
            tokens_in = sum(
                int(v.get("inputTokens", 0)) + int(v.get("cacheReadInputTokens", 0))
                for v in mu.values()
            )
            tokens_out = sum(int(v.get("outputTokens", 0)) for v in mu.values())
            return tokens_in, tokens_out
        usage = cli_output.get("usage", {})
        tokens_in = usage.get("input_tokens", 0) + usage.get(
            "cache_read_input_tokens", 0
        )
        tokens_out = usage.get("output_tokens", 0)
        return tokens_in, tokens_out

    def takeover_command(self, workspace: Path, session_uuid: str) -> str | None:
        """`claude --resume <uuid>` when a JSONL exists for this workspace+uuid."""
        path = self.session_log_path(workspace, session_uuid)
        if path is None:
            return None
        return f"claude --resume {session_uuid}"

    def session_log_path(self, workspace: Path, session_uuid: str) -> Path | None:
        """Live JSONL claude appends to as the session runs.

        Reads CLAUDE_PROJECTS_DIR through the transcript module so tests
        that monkeypatch it via the `claude_projects_dir` fixture (or a
        direct `setattr`) see the redirected location. The bare import
        captures the value at module-load time and is invisible to
        monkeypatch.
        """
        path = (
            transcript.CLAUDE_PROJECTS_DIR
            / _workspace_to_claude_hash(workspace)
            / f"{session_uuid}.jsonl"
        )
        return path if path.is_file() else None

    async def list_skills(self) -> list[tuple[str, str]]:
        """Enumerate skills (name, description) via a one-shot init probe plus
        SKILL.md front-matter. Uncached — the TTL cache lives in
        agent.cached_skills, which callers use."""
        # Probe the real `claude` binary directly even in pty mode —
        # claude-pty never emits stream-json, but the init event does.
        prefix = self.launcher.prefix("claude") if self.launcher else []
        binary = [] if self.launcher else ["claude"]
        names, plugins = await _probe_skills(prefix, binary)
        descriptions = _skill_descriptions(plugins)
        skills = [(name, descriptions.get(name, "")) for name in names]
        logger.info("list_skills: probed %d skills", len(skills))
        return skills


def _statusline_response_fields(statusline: dict) -> dict:
    """Pull the Response-friendly subset of fields out of a pty statusline.

    Returns kwargs ready to splat into `Response(...)`. Empty dict when the
    statusline is empty (native/ollama paths — Response defaults stand in).
    """
    if not statusline:
        return {}
    rl = statusline.get("rate_limits") or {}
    five = rl.get("five_hour") or {}
    seven = rl.get("seven_day") or {}
    cw = statusline.get("context_window") or {}
    out: dict = {}
    if "used_percentage" in five:
        out["rate_limits_5h_pct"] = int(five["used_percentage"])
    if "resets_at" in five:
        out["rate_limits_5h_resets_at"] = int(five["resets_at"])
    if "used_percentage" in seven:
        out["rate_limits_7d_pct"] = int(seven["used_percentage"])
    if "resets_at" in seven:
        out["rate_limits_7d_resets_at"] = int(seven["resets_at"])
    if "used_percentage" in cw:
        out["context_window_pct"] = int(cw["used_percentage"])
    # The absolutes the auto-compact gate thresholds on. `total_input_tokens` is
    # the whole prompt, so it never drops below the system prompt and tool
    # schemas (tens of thousands of tokens) however hard the conversation is
    # compacted — the floor is a property of the model, hence carrying the
    # window size alongside rather than a bare percentage.
    if "total_input_tokens" in cw:
        out["context_tokens"] = int(cw["total_input_tokens"])
    if "context_window_size" in cw:
        out["context_window_size"] = int(cw["context_window_size"])
    if "exceeds_200k_tokens" in statusline:
        out["exceeds_200k"] = bool(statusline["exceeds_200k_tokens"])
    if "fast_mode" in statusline:
        out["fast_mode"] = bool(statusline["fast_mode"])
    return out


async def _exec_pty(
    workspace: Path, cmd: list[str], timeout: float | None = None
) -> dict:
    """Run `claude-pty` and parse its single-JSON envelope on stdout.

    Returns a dict shaped to match what the native stream-json parser yields,
    plus a `statusline` key carrying the pty-only subtree. tool_counts and
    skill_counts are always empty in pty mode (pty doesn't surface per-turn
    tool_use events).
    """
    cmd = sandbox.wrap(cmd, workspace)
    logger.debug(
        "exec_pty: cwd=%s cmd=%s timeout=%s",
        workspace,
        " ".join(cmd[:4]) + "...",
        timeout,
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
        limit=16 * 1024 * 1024,
        start_new_session=True,
        env=sandbox.agent_env(),
    )
    agent.track_agent_process(proc, cmd)

    async def _wait() -> tuple[bytes, bytes, int]:
        stdout, stderr = await proc.communicate()
        return stdout, stderr, proc.returncode if proc.returncode is not None else -1

    try:
        if timeout is not None:
            stdout, stderr, rc = await asyncio.wait_for(_wait(), timeout=timeout)
        else:
            stdout, stderr, rc = await _wait()
    except TimeoutError:
        logger.warning("exec_pty: timed out after %ss", timeout)
        raise RuntimeError(f"claude-pty timed out after {timeout}s") from None
    finally:
        # Cancellation is how frontends stop a live turn. Reap the dedicated
        # process group here (rather than only in the timeout branch) so the
        # pty wrapper and any tools it launched cannot outlive the turn.
        await agent._kill_process_tree(proc)

    stdout_text = stdout.decode(errors="replace").strip()
    stderr_text = stderr.decode(errors="replace").strip()

    if rc != 0 and not stdout_text:
        raise agent._classify(stderr_text or f"claude-pty exit {rc}")

    if not stdout_text:
        raise RuntimeError(
            "claude-pty produced no envelope. See pty's troubleshooting "
            "section — likely a Claude Code upgrade broke the Stop hook. "
            "Fall back with CLAUDE_MODE=native or reinstall pty: " + PTY_INSTALL_HINT
        )

    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"claude-pty returned malformed JSON: {exc}; first 200 chars: "
            + stdout_text[:200]
        ) from exc

    if envelope.get("is_error") or str(envelope.get("subtype", "")).startswith("error"):
        raise agent._classify(envelope.get("result") or stderr_text or "pty error")

    envelope.setdefault("tool_counts", {})
    envelope.setdefault("skill_counts", {})
    return envelope
