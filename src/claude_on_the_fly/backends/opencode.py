"""opencode CLI backend.

opencode (https://github.com/sst/opencode) is an AI coding agent CLI with its
own provider system and tool set. This backend drives `opencode run --format
json` in non-interactive mode, plus optional ollama-mode wrapping via
`ollama launch opencode`.

Unlike claude/codex/pi, opencode reports cost natively in its `step_finish`
events, so no OpenRouter pricing lookup is needed. Session ids are
opencode-minted (`ses_...`), not UUIDs we control — so resume is codex-shaped:
run fresh, capture the `sessionID`, persist a
`<workspace>/.opencode_sessions/<our-uuid>` -> `ses_xxx` mapping, then
`opencode run -s <ses_xxx>` on follow-ups. The mapping is rewritten every turn
so its mtime tracks recency for the cross-backend handoff ordering.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from claude_on_the_fly import agent, transcript
from claude_on_the_fly.agent import (
    DEFAULT_TIMEOUT,
    NUDGE_PROMPT,
    OllamaLauncher,
    Response,
    build_system_prompt,
)

logger = logging.getLogger(__name__)

# Where we map our session_uuid -> opencode's ses_ id, per workspace.
SESSIONS_DIRNAME = ".opencode_sessions"


def parse_opencode_stream(stdout: bytes) -> dict:
    """Parse the NDJSON emitted by `opencode run --format json`.

    Returns a dict with `session_id`, `body`, `cost`, `tokens_in`,
    `tokens_out`, `tool_counts`, and `error` keys.

    Events seen:
    - `step_start`  — ignored
    - `text`        — `part.text` is assistant prose; last non-empty wins (the
                      final reply, mirroring how codex takes the last
                      agent_message)
    - `tool_use`    — `part.tool` is the tool name; counted once on completion
    - `step_finish` — carries native `part.cost` and `part.tokens` (summed
                      across steps, since a tool-using turn emits several)
    - `error`       — top-level terminal error
    """
    session_id: str | None = None
    body = ""
    cost = 0.0
    tokens_in = 0
    tokens_out = 0
    error: str | None = None
    tool_counts: dict[str, int] = {}

    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("opencode: skipping malformed line: %s", line[:120])
            continue

        session_id = msg.get("sessionID") or session_id
        kind = msg.get("type")
        part = msg.get("part") or {}

        if kind == "text":
            text = (part.get("text") or "").strip()
            if text:
                body = text
        elif kind == "tool_use":
            # Count each tool once, on completion, to avoid double-counting any
            # interim state events.
            status = (part.get("state") or {}).get("status")
            if status in (None, "completed"):
                name = part.get("tool", "unknown")
                tool_counts[name] = tool_counts.get(name, 0) + 1
        elif kind == "step_finish":
            cost += part.get("cost") or 0
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            tokens_in += (tokens.get("input") or 0) + (cache.get("read") or 0)
            tokens_out += (tokens.get("output") or 0) + (tokens.get("reasoning") or 0)
        elif kind == "error":
            err = msg.get("error") or {}
            error = (
                (err.get("data") or {}).get("message")
                or err.get("name")
                or ("opencode run failed")
            )

    return {
        "session_id": session_id,
        "body": body,
        "cost": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "error": error,
        "tool_counts": tool_counts,
    }


def _merge_opencode_results(first: dict, second: dict) -> dict:
    """Combine an initial result with a nudge retry: body from retry, cost and
    tokens and tool_counts summed, session_id preserved."""
    return {
        "session_id": first.get("session_id") or second.get("session_id"),
        "body": second.get("body") or "",
        "cost": (first.get("cost") or 0) + (second.get("cost") or 0),
        "tokens_in": (first.get("tokens_in") or 0) + (second.get("tokens_in") or 0),
        "tokens_out": (first.get("tokens_out") or 0) + (second.get("tokens_out") or 0),
        "error": None,
        "tool_counts": agent._sum_counts(
            first.get("tool_counts"), second.get("tool_counts")
        ),
    }


async def _run_opencode_exec(
    workspace: Path, cmd: list[str], timeout: float | None
) -> dict:
    """Run opencode, collect stdout, parse NDJSON. Raises RuntimeError on failure."""
    logger.debug("opencode exec: cwd=%s cmd=%s", workspace, " ".join(cmd[:8]) + "...")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
        limit=16 * 1024 * 1024,
    )
    try:
        if timeout is not None:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        else:
            stdout_bytes, stderr_bytes = await proc.communicate()
    except asyncio.TimeoutError:
        logger.warning("opencode exec: timed out after %ss", timeout)
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            except Exception:
                logger.exception("opencode exec: failed to reap subprocess")
        raise RuntimeError(f"opencode timed out after {timeout}s")

    parsed = parse_opencode_stream(stdout_bytes)
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


class OpencodeBackend:
    """Drives the `opencode run` CLI in non-interactive (`--format json`) mode.

    Two modes:
    - native (default): `opencode run --format json …`
    - ollama (`launcher` set): wraps with `ollama launch opencode --model X --yes --`
    """

    def __init__(self, launcher: OllamaLauncher | None = None) -> None:
        self.launcher = launcher

    def _read_session_id(self, workspace: Path, session_uuid: str) -> str | None:
        path = workspace / SESSIONS_DIRNAME / session_uuid
        if not path.is_file():
            return None
        ses = path.read_text().strip()
        return ses or None

    def _write_session_id(
        self, workspace: Path, session_uuid: str, ses_id: str
    ) -> None:
        sessions_dir = workspace / SESSIONS_DIRNAME
        sessions_dir.mkdir(parents=True, exist_ok=True)
        # Rewritten every turn so the file mtime tracks the session's recency,
        # which the cross-backend handoff ordering relies on.
        (sessions_dir / session_uuid).write_text(ses_id)

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
            "session: id=%s platform=%s user=%s context=%s workspace=%s backend=opencode",
            session_uuid,
            platform,
            user_name,
            channel_context,
            workspace,
        )

        existing_session = self._read_session_id(workspace, session_uuid)

        # First opencode turn for this session: forward any prior history from
        # another backend and prepend the system prompt (opencode has no
        # --system-prompt run flag — it reads AGENTS.md, but the per-turn format
        # hint and persona still need to land in the message). On resume the
        # system prompt is already in opencode's persisted session, so re-sending
        # it every turn just inflates tokens.
        if existing_session:
            composed_prompt = prompt
        else:
            user_payload = transcript.prepend_latest_handoff(
                workspace, prompt, exclude_uuid=session_uuid
            )
            system_prompt = build_system_prompt(platform, user_name, channel_context)
            composed_prompt = f"{system_prompt}\n\n---\n\n{user_payload}"

        # `ollama launch opencode` already invokes the opencode binary; skip it
        # after `--`. Likewise drop our -m so ollama's --model wins.
        prefix = self.launcher.prefix("opencode") if self.launcher else []
        binary = [] if self.launcher else ["opencode"]
        model_env = os.environ.get("OPENCODE_MODEL", "").strip()
        model_args = [] if self.launcher else (["-m", model_env] if model_env else [])
        base = [
            *prefix,
            *binary,
            "run",
            "--format",
            "json",
            "--dangerously-skip-permissions",
            *model_args,
        ]
        if existing_session:
            logger.debug(
                "opencode: resuming session=%s ses=%s",
                session_uuid,
                existing_session,
            )
            cmd = [*base, "-s", existing_session, composed_prompt]
        else:
            logger.debug("opencode: starting new session=%s", session_uuid)
            cmd = [*base, composed_prompt]

        started_at = time.monotonic()
        result = await _run_opencode_exec(workspace, cmd, timeout=timeout)
        duration = time.monotonic() - started_at

        ses_id = result.get("session_id") or existing_session
        if ses_id:
            self._write_session_id(workspace, session_uuid, ses_id)
            if not existing_session:
                logger.info(
                    "opencode: persisted new ses=%s for session=%s",
                    ses_id,
                    session_uuid,
                )

        body = (result.get("body") or "").strip()
        if not body:
            if ses_id:
                logger.warning(
                    "opencode: empty result, retrying with nudge, session=%s",
                    session_uuid,
                )
                retry_cmd = [*base, "-s", ses_id, NUDGE_PROMPT]
                retry_started = time.monotonic()
                retry_result = await _run_opencode_exec(
                    workspace, retry_cmd, timeout=timeout
                )
                duration += time.monotonic() - retry_started
                result = _merge_opencode_results(result, retry_result)
                body = (result.get("body") or "").strip() or "No response"
            else:
                body = "No response"

        model_label = (
            self.launcher.model if self.launcher else (model_env or "opencode")
        )
        return Response(
            body=body,
            cost=result.get("cost") or 0,
            duration=duration,
            tokens_in=result.get("tokens_in") or 0,
            tokens_out=result.get("tokens_out") or 0,
            model=model_label,
            tool_counts=result.get("tool_counts", {}),
            skill_counts={},
        )

    def takeover_command(self, workspace: Path, session_uuid: str) -> str | None:
        """`opencode -s <ses_id>` when a session mapping exists for this uuid."""
        ses_id = self._read_session_id(workspace, session_uuid)
        if ses_id is None:
            return None
        return f"opencode -s {ses_id}"

    def session_log_path(self, workspace: Path, session_uuid: str) -> Path | None:
        """opencode persists sessions in a SQLite db + a fanned-out filesystem
        store, not a single tailable JSONL — so there's nothing for the watch
        pane to follow. Explicitly None."""
        return None
