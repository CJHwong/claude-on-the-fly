"""Pi CLI backend.

Pi is an AI coding assistant CLI (pi) with its own tool set (read, bash, edit,
write) and NDJSON output format. This backend drives `pi -p --mode json` in
non-interactive mode, plus optional ollama-mode wrapping via `ollama launch pi`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from claude_on_the_fly import agent, pricing, transcript
from claude_on_the_fly.agent import (
    DEFAULT_TIMEOUT,
    NUDGE_PROMPT,
    OllamaLauncher,
    Response,
    build_system_prompt,
)
from claude_on_the_fly.transcript import _workspace_to_pi_hash

logger = logging.getLogger(__name__)

# pi stores sessions under ~/.pi/agent/sessions/<workspace-hash>/
PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"


def parse_pi_stream(stdout: bytes) -> dict:
    """Parse the NDJSON emitted by `pi -p --mode json`.

    Returns a dict with `body`, `usage`, `model`, `provider`, `tool_counts`,
    and `error` keys. The last `agent_end` event carries the complete message
    history; we extract from there.
    """
    body = ""
    model = ""
    provider = ""
    usage: dict = {}
    error: str | None = None
    tool_counts: dict[str, int] = {}

    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("pi: skipping malformed line: %s", line[:120])
            continue

        kind = msg.get("type")
        if kind == "agent_end":
            messages = msg.get("messages") or []
            for m in messages:
                if m.get("role") != "assistant":
                    continue
                # Collect the last text block as the body.
                for c in m.get("content") or []:
                    c_type = c.get("type")
                    if c_type == "text":
                        body = c.get("text") or ""
                    elif c_type == "toolCall":
                        name = c.get("name", "unknown")
                        tool_counts[name] = tool_counts.get(name, 0) + 1
                # Usage / model from the last assistant message.
                model = m.get("model") or model
                provider = m.get("provider") or provider
                usage = m.get("usage") or usage
            # The willRetry flag signals pi's own retry loop; treat as error
            # only when there's no body at all.
            if not body and msg.get("willRetry") is True:
                error = error or "pi returned no final text (willRetry=True)"

    return {
        "body": body,
        "usage": usage,
        "model": model,
        "provider": provider,
        "error": error,
        "tool_counts": tool_counts,
    }


def _session_has_content(path: Path) -> bool:
    """True if the pi session JSONL exists and holds real content.

    A file that merely exists could be empty (failed first turn where the
    LLM never produced output). When there is real content, pi has already
    persisted the system prompt, so a resume need not re-send it.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            return any(line.strip() for line in handle)
    except OSError:
        return False


def _find_pi_session_path(workspace: Path, session_uuid: str) -> Path | None:
    """Locate the session JSONL for this workspace + uuid.

    pi names session files as `<timestamp>_<uuid>.jsonl` — we glob.
    """
    session_dir = PI_SESSIONS_DIR / _workspace_to_pi_hash(workspace)
    if not session_dir.is_dir():
        return None
    # Match files ending with `_<uuid>.jsonl`.
    candidates = sorted(
        session_dir.glob(f"*_{session_uuid}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


async def _run_pi_exec(workspace: Path, cmd: list[str], timeout: float | None) -> dict:
    """Run pi, stream stdout, parse NDJSON. Raises RuntimeError on failure."""
    logger.debug("pi exec: cwd=%s cmd=%s", workspace, " ".join(cmd[:8]) + "...")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
        limit=16 * 1024 * 1024,
    )
    try:
        result = await asyncio.wait_for(_consume_pi(proc), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("pi exec: timed out after %ss", timeout)
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            except Exception:
                logger.exception("pi exec: failed to reap subprocess")
        raise RuntimeError(f"pi timed out after {timeout}s")
    return result


async def _consume_pi(proc: asyncio.subprocess.Process) -> dict:
    """Stream stdout, drain stderr concurrently, parse the NDJSON result."""
    assert proc.stdout is not None and proc.stderr is not None

    stderr_task = asyncio.create_task(proc.stderr.read())
    chunks: list[bytes] = []
    try:
        async for raw in proc.stdout:
            chunks.append(raw)
    except BaseException:
        stderr_task.cancel()
        raise
    finally:
        try:
            stderr_bytes = await stderr_task
        except (asyncio.CancelledError, Exception):
            stderr_bytes = b""

    await proc.wait()
    stdout_bytes = b"".join(chunks)
    logger.debug(
        "pi exec: returncode=%s stdout=%d stderr=%d bytes",
        proc.returncode,
        len(stdout_bytes),
        len(stderr_bytes),
    )

    parsed = parse_pi_stream(stdout_bytes)
    if proc.returncode != 0:
        detail = (
            parsed.get("error")
            or stderr_bytes.decode(errors="replace").strip()
            or f"Exit code {proc.returncode}"
        )
        raise RuntimeError(detail)
    if parsed.get("error"):
        raise RuntimeError(parsed["error"])
    return parsed


class PiBackend:
    """Drives the `pi` CLI in non-interactive (`-p --mode json`) mode.

    Three modes:
    - native (default): `pi -p --mode json …`
    - ollama (`launcher` set): wraps with `ollama launch pi --model X --yes --`
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
    ) -> Response:
        logger.info(
            "session: id=%s platform=%s user=%s context=%s workspace=%s backend=pi",
            session_uuid,
            platform,
            user_name,
            channel_context,
            workspace,
        )

        system_prompt = build_system_prompt(platform, user_name, channel_context)

        # pi's --system-prompt flag converts to a "developer" role message
        # which some providers (ollama, openai-compatible) don't support.
        # Prepend the system prompt into the user message instead — same
        # approach the codex backend uses.
        # On resume, pi already has the system prompt in its persisted
        # session, so we skip it to avoid re-sending ~4.7KB per turn.

        # Build base argv.
        provider = os.environ.get("PI_PROVIDER", "").strip().lower()
        model_env = os.environ.get("PI_MODEL", "").strip()

        prefix = self.launcher.prefix("pi") if self.launcher else []
        # `ollama launch pi` already invokes the pi binary; skip it.
        binary = [] if self.launcher else ["pi"]
        provider_args = ["--provider", provider] if provider else []
        # Don't pass --model when launcher is set (ollama overrides).
        model_args = (
            [] if self.launcher else (["--model", model_env] if model_env else [])
        )

        base = [
            *prefix,
            *binary,
            "-p",
            "--mode",
            "json",
            *provider_args,
            *model_args,
        ]

        # Determine if this is a resume.
        session_path = _find_pi_session_path(workspace, session_uuid)
        existing_session = _session_has_content(session_path) if session_path else False

        if existing_session:
            logger.debug("pi: resuming session=%s prompt=%s", session_uuid, prompt[:80])
            argv = [*base, "--session-id", session_uuid, prompt]
        elif session_path is not None and session_path.is_file():
            # Session file exists but is empty — re-supply system prompt.
            logger.warning(
                "pi: session=%s exists but is empty; re-supplying system prompt",
                session_uuid,
            )
            argv = [
                *base,
                "--session-id",
                session_uuid,
                f"{system_prompt}\n\n---\n\n{prompt}",
            ]
        else:
            logger.info("pi: no existing session %s, creating new", session_uuid)
            prompt = transcript.prepend_latest_handoff(
                workspace, prompt, exclude_uuid=session_uuid
            )
            argv = [
                *base,
                "--session-id",
                session_uuid,
                f"{system_prompt}\n\n---\n\n{prompt}",
            ]

        started_at = time.monotonic()
        result = await _run_pi_exec(workspace, argv, timeout=timeout)
        duration = time.monotonic() - started_at

        body = (result.get("body") or "").strip()
        if not body:
            logger.warning(
                "pi: empty result, retrying with nudge, session=%s", session_uuid
            )
            retry_started = time.monotonic()
            retry_result = await _run_pi_exec(
                workspace,
                [*base, "--session-id", session_uuid, NUDGE_PROMPT],
                timeout=timeout,
            )
            duration += time.monotonic() - retry_started
            # Merge: body from retry, usage summed (flat numeric fields only).
            retry_body = (retry_result.get("body") or "").strip()
            result["body"] = retry_body or "No response"
            # Sum flat numeric usage fields; skip nested dicts like `cost`.
            merged_usage = dict(result.get("usage") or {})
            retry_usage = retry_result.get("usage") or {}
            for k, v in retry_usage.items():
                existing = merged_usage.get(k, 0)
                if isinstance(v, (int, float)) and isinstance(existing, (int, float)):
                    merged_usage[k] = existing + v
            result["usage"] = merged_usage
            result["tool_counts"] = agent._sum_counts(
                result.get("tool_counts"), retry_result.get("tool_counts")
            )
            body = result["body"]

        usage = result.get("usage") or {}
        tokens_in = usage.get("input", 0) + usage.get("cacheRead", 0)
        tokens_out = usage.get("output", 0)
        model_id = result.get("model") or model_env or "pi"

        # pi's cost is always 0 in its own output (it doesn't compute pricing).
        # Look it up from OpenRouter's price table like we do for codex.
        cost = (
            await asyncio.to_thread(pricing.cost_for, model_id, tokens_in, tokens_out)
            or 0
        )

        return Response(
            body=body,
            cost=cost,
            duration=duration,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model_id,
            tool_counts=result.get("tool_counts", {}),
            skill_counts={},
        )

    def takeover_command(self, workspace: Path, session_uuid: str) -> str | None:
        """`pi --resume <uuid>` when a session exists for this workspace+uuid."""
        path = self.session_log_path(workspace, session_uuid)
        if path is None:
            return None
        return f"pi --resume {session_uuid}"

    def session_log_path(self, workspace: Path, session_uuid: str) -> Path | None:
        """Live JSONL pi appends to as the session runs."""
        return _find_pi_session_path(workspace, session_uuid)
