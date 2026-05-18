"""Codex CLI backend."""

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

logger = logging.getLogger(__name__)


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


def parse_codex_stream(stdout: bytes) -> dict:
    """Parse the JSONL emitted by `codex exec --json`.

    Returns a dict with `thread_id`, `body`, `usage`, `tool_counts`, and
    `error` keys. `error` is set only when codex emits `turn.failed`.
    """
    thread_id: str | None = None
    body = ""
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
            elif item_type and item_type != "reasoning":
                tool_counts[item_type] = tool_counts.get(item_type, 0) + 1
        elif kind == "turn.completed":
            usage = msg.get("usage") or {}
        elif kind == "turn.failed":
            error = (msg.get("error") or {}).get("message") or "codex turn failed"
        # "error" events are reconnect noise; only turn.failed is terminal
    return {
        "thread_id": thread_id,
        "body": body,
        "usage": usage,
        "error": error,
        "tool_counts": tool_counts,
    }


async def _run_codex_exec(
    workspace: Path, cmd: list[str], timeout: float | None
) -> dict:
    """Run codex, collect stdout, parse JSONL. Raises RuntimeError on failure."""
    logger.debug("codex exec: cwd=%s cmd=%s", workspace, " ".join(cmd[:8]) + "...")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
    )
    try:
        if timeout is not None:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        else:
            stdout_bytes, stderr_bytes = await proc.communicate()
    except asyncio.TimeoutError:
        logger.warning("codex exec: timed out after %ss", timeout)
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            except Exception:
                logger.exception("codex exec: failed to reap subprocess")
        raise RuntimeError(f"Codex CLI timed out after {timeout}s")

    parsed = parse_codex_stream(stdout_bytes)
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


class CodexBackend:
    """Drives the `codex exec` CLI in non-interactive (`--json`) mode.

    Codex assigns its own `thread_id` and offers no flag to pre-seed it, so
    we persist a `<workspace>/.codex_sessions/<our-session-uuid>` mapping file
    after the first turn and pass `resume <thread_id>` on follow-ups.
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
            "session: id=%s platform=%s user=%s context=%s workspace=%s",
            session_uuid,
            platform,
            user_name,
            channel_context,
            workspace,
        )
        sessions_dir = workspace / ".codex_sessions"
        session_file = sessions_dir / session_uuid
        existing_thread = (
            session_file.read_text().strip() if session_file.exists() else None
        )

        # First codex turn for this session: forward any prior claude history,
        # and prepend the system prompt (codex has no --system-prompt flag, so
        # this is our only way to deliver it). On subsequent turns the system
        # prompt is already in the thread's persisted history — re-sending it
        # every turn inflates tokens (~4.7KB per turn) and bloats context.
        if existing_thread:
            composed_prompt = prompt
        else:
            user_payload = transcript.prepend_handoff(
                workspace,
                session_uuid,
                prompt,
                from_backend="claude",
                extractor=transcript.extract_claude,
            )
            system_prompt = build_system_prompt(platform, user_name, channel_context)
            composed_prompt = f"{system_prompt}\n\n---\n\n{user_payload}"

        # `ollama launch codex` already invokes the codex binary; repeating
        # "codex" after `--` would make it argv[1], which codex treats as the
        # subcommand. Skip the binary when the launcher is set.
        prefix = self.launcher.prefix("codex") if self.launcher else []
        binary = [] if self.launcher else ["codex"]
        model_env = os.environ.get("CODEX_MODEL", "").strip()
        model_args = [] if self.launcher else (["-m", model_env] if model_env else [])
        base = [
            *prefix,
            *binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--yolo",
            "-C",
            str(workspace),
            *model_args,
        ]
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
            sessions_dir.mkdir(parents=True, exist_ok=True)
            session_file.write_text(new_thread)
            logger.info(
                "codex: persisted new thread=%s for session=%s",
                new_thread,
                session_uuid,
            )

        body = (result.get("body") or "").strip()
        if not body:
            thread_for_retry = result.get("thread_id") or existing_thread
            if thread_for_retry:
                logger.warning(
                    "codex: empty result, retrying with nudge, session=%s",
                    session_uuid,
                )
                retry_cmd = [*base, "resume", thread_for_retry, NUDGE_PROMPT]
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
            self.launcher.model if self.launcher else (model_env or "codex")
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
            pre_in = (pre_totals or {}).get("input_tokens", 0)
            pre_out = (pre_totals or {}).get("output_tokens", 0)
            pre_reasoning = (pre_totals or {}).get("reasoning_output_tokens", 0)
            tokens_in = post_totals.get("input_tokens", 0) - pre_in
            tokens_out = (
                post_totals.get("output_tokens", 0)
                + post_totals.get("reasoning_output_tokens", 0)
                - pre_out
                - pre_reasoning
            )
        else:
            usage = result.get("usage") or {}
            tokens_in = usage.get("input_tokens", 0)
            tokens_out = usage.get("output_tokens", 0) + usage.get(
                "reasoning_output_tokens", 0
            )
        # Codex CLI doesn't emit cost; look it up from a price table off-thread
        # so the rare fetch never blocks the event loop. None coalesces to 0.
        computed_cost = (
            await asyncio.to_thread(
                pricing.cost_for, model_label, tokens_in, tokens_out
            )
            or 0
        )
        return Response(
            body=body,
            cost=computed_cost,
            duration=duration,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model_label,
            tool_counts=result.get("tool_counts", {}),
            skill_counts={},
        )
