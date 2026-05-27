"""Claude Code CLI backend."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from claude_on_the_fly import agent, pricing, transcript
from claude_on_the_fly.agent import (
    DEFAULT_TIMEOUT,
    OllamaLauncher,
    Response,
    build_system_prompt,
)
from claude_on_the_fly.transcript import (
    _workspace_to_claude_hash,
)

logger = logging.getLogger(__name__)


SNAP_PROJECT_SLUG = "claude-interactive-p"
SNAP_INSTALL_HINT = (
    f"curl -fsSL https://raw.githubusercontent.com/CJHwong/"
    f"{SNAP_PROJECT_SLUG}/main/install.sh | bash"
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


def resolve_snap_binary() -> str | None:
    """Find the `claude-snap` binary.

    Order: PATH → `$CLAUDE_INTERACTIVE_P_HOME/bin/claude-snap` →
    `~/.local/share/{SNAP_PROJECT_SLUG}/bin/claude-snap`. Returns the absolute
    path or None.
    """
    on_path = shutil.which("claude-snap")
    if on_path:
        return on_path
    home = os.environ.get("CLAUDE_INTERACTIVE_P_HOME") or str(
        Path.home() / ".local/share" / SNAP_PROJECT_SLUG
    )
    candidate = Path(home) / "bin" / "claude-snap"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


class ClaudeBackend:
    """Drives the `claude` CLI.

    Three modes, mutually exclusive:
    - native (default): `claude -p --output-format stream-json …`
    - ollama (`launcher` set): wraps with `ollama launch claude --model X --yes --`
    - snap (`snap=True`): drives `claude-snap` (interactive PTY wrapper from
      claude-interactive-p) so we get statusline-only fields like rate_limits
      and context_window. Argv drops `-p`, `--output-format`, `--verbose`.
    """

    def __init__(
        self,
        launcher: OllamaLauncher | None = None,
        snap: bool = False,
    ) -> None:
        if launcher is not None and snap:
            raise ValueError("ClaudeBackend: launcher and snap are mutually exclusive")
        self.launcher = launcher
        self.snap = snap
        # Resolve once at construction so per-message hot path skips the
        # `shutil.which` + `os.access` syscalls. Missing binary fails fast
        # here rather than on the first message — preflight already guarantees
        # it, this is defense in depth for misconfigured callers.
        self._snap_path: str | None = None
        if snap:
            self._snap_path = resolve_snap_binary()
            if self._snap_path is None:
                raise RuntimeError(
                    "claude-snap binary not found. Install with: " + SNAP_INSTALL_HINT
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
        system_prompt = build_system_prompt(platform, user_name, channel_context)
        # --system-prompt is only attached when (re-)establishing a session; a
        # healthy --resume reuses the prompt already persisted in the session.
        sysprompt_args = ["--system-prompt", system_prompt]

        if self.snap:
            base = self._snap_base_argv()
            executor = _exec_snap
        else:
            # `ollama launch claude` already invokes the claude binary; repeating
            # "claude" after `--` would make it argv[1], which -p mode parses as
            # the prompt and silently drops the real one.
            prefix = self.launcher.prefix("claude") if self.launcher else []
            binary = [] if self.launcher else ["claude"]
            # Empty/unset CLAUDE_MODEL → omit --model and let the claude CLI use
            # its own default (don't pin sonnet).
            _model = "" if self.launcher else os.environ.get("CLAUDE_MODEL", "").strip()
            model_args = ["--model", _model] if _model else []
            base = [
                *prefix,
                *binary,
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "bypassPermissions",
                *model_args,
            ]
            executor = agent._exec

        # Pick --resume vs --session-id deterministically by checking whether
        # the session JSONL exists on disk. Old code sniffed claude's error
        # message ("No conversation found") on a failed --resume, but:
        #   1) Snap mode wraps stderr behind "no envelope produced (claude
        #      rc=1)", so the sniff never matched in snap mode.
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
            prompt = transcript.prepend_latest_handoff(
                workspace, prompt, exclude_uuid=session_uuid
            )
            argv = [*base, *sysprompt_args, "--session-id", session_uuid, prompt]
        cli_output = await executor(workspace, argv, timeout=timeout)

        body = (cli_output.get("result") or "").strip()
        if not body:
            logger.warning(
                "agent.run: empty result, retrying with nudge, session=%s", session_uuid
            )
            retry_output = await executor(
                workspace,
                [*base, "--resume", session_uuid, agent.NUDGE_PROMPT],
                timeout=timeout,
            )
            if self.snap:
                # Snap envelopes have no per-tool counts to merge and snap's
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
        # handles its own cost. Native and snap modes keep the CLI's value,
        # which reflects Anthropic's real billing.
        if self.launcher is not None:
            cost = (
                await asyncio.to_thread(pricing.cost_for, model, tokens_in, tokens_out)
                or 0
            )
        else:
            cost = cli_output.get("total_cost_usd", 0)

        statusline = cli_output.get("statusline") or {}

        return Response(
            body=body,
            cost=cost,
            duration=cli_output.get("duration_ms", 0) / 1000,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            tool_counts=cli_output.get("tool_counts", {}),
            skill_counts=cli_output.get("skill_counts", {}),
            **_statusline_response_fields(statusline),
        )

    def _snap_base_argv(self) -> list[str]:
        """Snap argv minus the prompt and --system-prompt; the caller appends
        --system-prompt only when (re-)establishing a session."""
        assert self._snap_path is not None  # set in __init__ when snap=True
        model = os.environ.get("CLAUDE_MODEL", "").strip()
        model_args = ["--model", model] if model else []
        return [
            self._snap_path,
            "--permission-mode",
            "bypassPermissions",
            *model_args,
        ]

    def _extract_tokens(self, cli_output: dict) -> tuple[int, int]:
        """Return (tokens_in, tokens_out).

        Snap's top-level `usage` is the last assistant message only, so for
        multi-turn snap calls we'd undercount. `modelUsage` is aggregated
        across every assistant record by snap's transcript pass, so it's the
        truthful cross-turn total.

        Native/ollama stay on `usage` because that's how stream-json folds
        already work, and we want zero behavior change there.
        """
        if self.snap:
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


def _statusline_response_fields(statusline: dict) -> dict:
    """Pull the Response-friendly subset of fields out of a snap statusline.

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
    if "exceeds_200k_tokens" in statusline:
        out["exceeds_200k"] = bool(statusline["exceeds_200k_tokens"])
    if "fast_mode" in statusline:
        out["fast_mode"] = bool(statusline["fast_mode"])
    return out


async def _exec_snap(
    workspace: Path, cmd: list[str], timeout: float | None = None
) -> dict:
    """Run `claude-snap` and parse its single-JSON envelope on stdout.

    Returns a dict shaped to match what the native stream-json parser yields,
    plus a `statusline` key carrying the snap-only subtree. tool_counts and
    skill_counts are always empty in snap mode (snap doesn't surface per-turn
    tool_use events).
    """
    logger.debug(
        "exec_snap: cwd=%s cmd=%s timeout=%s",
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
    )

    async def _wait() -> tuple[bytes, bytes, int]:
        stdout, stderr = await proc.communicate()
        return stdout, stderr, proc.returncode if proc.returncode is not None else -1

    try:
        if timeout is not None:
            stdout, stderr, rc = await asyncio.wait_for(_wait(), timeout=timeout)
        else:
            stdout, stderr, rc = await _wait()
    except asyncio.TimeoutError:
        logger.warning("exec_snap: timed out after %ss", timeout)
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        raise RuntimeError(f"claude-snap timed out after {timeout}s")

    stdout_text = stdout.decode(errors="replace").strip()
    stderr_text = stderr.decode(errors="replace").strip()

    if rc != 0 and not stdout_text:
        raise agent._classify(stderr_text or f"claude-snap exit {rc}")

    if not stdout_text:
        raise RuntimeError(
            "claude-snap produced no envelope. See snap's troubleshooting "
            "section — likely a Claude Code upgrade broke the Stop hook. "
            "Fall back with CLAUDE_MODE=native or reinstall snap: " + SNAP_INSTALL_HINT
        )

    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"claude-snap returned malformed JSON: {exc}; first 200 chars: "
            + stdout_text[:200]
        ) from exc

    if envelope.get("is_error") or str(envelope.get("subtype", "")).startswith("error"):
        raise agent._classify(envelope.get("result") or stderr_text or "snap error")

    envelope.setdefault("tool_counts", {})
    envelope.setdefault("skill_counts", {})
    return envelope
