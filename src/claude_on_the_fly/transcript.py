"""Cross-backend conversation transcript extraction and handoff.

Each agent CLI writes its own session JSONL. When a daemon restarts under a
different backend, we look up the prior backend's transcript for the same
session_uuid and prepend a short handoff preamble to the next user prompt so
context survives the switch.

Public surface:
- `Turn` dataclass
- `extract_claude(workspace, session_uuid)` reads ~/.claude/projects/<hash>/<uuid>.jsonl
- `extract_codex(workspace, session_uuid)` reads the codex rollout matching the
  thread_id persisted under <workspace>/.codex_sessions/<session_uuid>
- `format_handoff(turns, from_backend)` renders a labeled preamble, capped by
  turn count and char budget from the most recent backward
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

logger = logging.getLogger(__name__)

BackendName = Literal["claude", "codex"]

# Same separator CodexBackend uses to fence the system prompt off from the user
# prompt. We rsplit on it to recover the raw user text from a codex transcript.
_CODEX_PROMPT_SEPARATOR = "\n\n---\n\n"

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class Turn:
    role: str  # "user" or "assistant"
    text: str


def _workspace_to_claude_hash(workspace: Path) -> str:
    """`/private/tmp/foo` -> `-private-tmp-foo`. Mirrors claude's own scheme.

    Resolve symlinks first — the claude CLI resolves `/tmp` → `/private/tmp`
    on macOS before computing the hash, so we must match.
    """
    return str(workspace.resolve()).replace("/", "-")


def _iter_jsonl(path: Path):
    try:
        raw = path.read_bytes()
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.debug("transcript: skipping malformed line in %s", path)
            continue


def extract_claude(workspace: Path, session_uuid: str) -> list[Turn] | None:
    """Return the user/assistant turns from claude's session JSONL, or None."""
    session_path = (
        CLAUDE_PROJECTS_DIR
        / _workspace_to_claude_hash(workspace)
        / f"{session_uuid}.jsonl"
    )
    if not session_path.is_file():
        return None
    turns: list[Turn] = []
    for msg in _iter_jsonl(session_path):
        kind = msg.get("type")
        if kind == "user":
            content = msg.get("message", {}).get("content")
            if isinstance(content, str) and content.strip():
                turns.append(Turn("user", content))
        elif kind == "assistant":
            for block in msg.get("message", {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        turns.append(Turn("assistant", text))
                    break
    return turns or None


def _find_codex_rollout(thread_id: str) -> Path | None:
    """Locate the codex session JSONL for a given thread_id (newest if multiple)."""
    if not thread_id:
        return None
    matches = sorted(
        CODEX_SESSIONS_DIR.glob(f"**/rollout-*-{thread_id}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def extract_codex(workspace: Path, session_uuid: str) -> list[Turn] | None:
    """Return the user/assistant turns from the codex rollout file, or None.

    Strips the system-prompt prefix we prepend to every codex user message.
    """
    session_file = workspace / ".codex_sessions" / session_uuid
    if not session_file.is_file():
        return None
    try:
        thread_id = session_file.read_text().strip()
    except OSError:
        return None
    rollout = _find_codex_rollout(thread_id)
    if rollout is None:
        logger.debug("transcript: no codex rollout for thread=%s", thread_id)
        return None
    turns: list[Turn] = []
    for msg in _iter_jsonl(rollout):
        if msg.get("type") != "event_msg":
            continue
        payload = msg.get("payload") or {}
        payload_type = payload.get("type")
        if payload_type == "user_message":
            text = payload.get("message") or ""
            # Strip our `<system_prompt>\n\n---\n\n<user_prompt>` prefix; the
            # rsplit form survives the (rare) case where the user typed `---`.
            stripped = text.split(_CODEX_PROMPT_SEPARATOR, 1)[-1].strip()
            if stripped:
                turns.append(Turn("user", stripped))
        elif payload_type == "agent_message":
            text = (payload.get("message") or "").strip()
            if text:
                turns.append(Turn("assistant", text))
    return turns or None


def extract_codex_model(thread_id: str) -> str | None:
    """Return the model codex actually used for a thread, or None.

    Codex's `--json` stdout omits the model; it only appears in the persisted
    session file as `turn_context.payload.model`. Needed because our backend
    can otherwise only label runs with whatever the user configured (which is
    blank in native mode without CODEX_MODEL).
    """
    rollout = _find_codex_rollout(thread_id)
    if rollout is None:
        return None
    for msg in _iter_jsonl(rollout):
        if msg.get("type") != "turn_context":
            continue
        model = (msg.get("payload") or {}).get("model")
        if isinstance(model, str) and model:
            return model
    return None


def extract_codex_cumulative_tokens(thread_id: str) -> dict | None:
    """Return the cumulative `total_token_usage` snapshot for a codex thread.

    Codex's stdout `turn.completed.usage` re-reports the thread's running
    total on every exec, not per-turn — so by turn N, `input_tokens` has
    been summed N times. The session file carries the same `total_token_usage`
    on every `event_msg/token_count` event, growing monotonically. Snapshot
    it before and after one exec call and the delta is that exec's true
    contribution (including any internal tool-fanout sub-calls).

    Returns the `total_token_usage` dict from the LAST `token_count` event,
    or None when no event exists yet.
    """
    rollout = _find_codex_rollout(thread_id)
    if rollout is None:
        return None
    latest: dict | None = None
    for msg in _iter_jsonl(rollout):
        if msg.get("type") != "event_msg":
            continue
        payload = msg.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        total = (payload.get("info") or {}).get("total_token_usage")
        if isinstance(total, dict):
            latest = total
    return latest


def _render_line(turn: Turn) -> str:
    return f"{turn.role.capitalize()}: {turn.text}"


def format_handoff(
    turns: list[Turn],
    from_backend: BackendName,
    max_turns: int = 20,
    max_chars: int = 10_000,
) -> str:
    """Render a labeled preamble from the most recent turns, within budget.

    Returns an empty string if there's nothing to forward.
    """
    if not turns:
        return ""
    selected: list[Turn] = []
    budget = max_chars
    # Walk newest-first so the most recent context survives truncation.
    for turn in reversed(turns[-max_turns:]):
        line_cost = len(_render_line(turn)) + 1  # +1 for the joining newline
        if line_cost > budget:
            break
        selected.append(turn)
        budget -= line_cost
    if not selected:
        return ""
    selected.reverse()
    body = "\n".join(_render_line(t) for t in selected)
    return (
        f"[Prior conversation via {from_backend}, last {len(selected)} turn(s)]\n\n"
        f"{body}\n\n"
        f"[Continue from here]\n\n"
    )


def prepend_handoff(
    workspace: Path,
    session_uuid: str,
    prompt: str,
    *,
    from_backend: BackendName,
    extractor: Callable[[Path, str], list[Turn] | None],
) -> str:
    """Return `prompt` with a labeled preamble from the prior backend prepended.

    Returns the original prompt unchanged on extraction failure or when no
    prior turns exist — never blocks the caller.
    """
    try:
        turns = extractor(workspace, session_uuid)
    except Exception:
        logger.exception(
            "transcript: %s extraction failed; starting clean", from_backend
        )
        return prompt
    if not turns:
        return prompt
    handoff = format_handoff(turns, from_backend=from_backend)
    if not handoff:
        return prompt
    logger.info(
        "transcript: forwarding %d %s turn(s) to next backend for session=%s",
        len(turns),
        from_backend,
        session_uuid,
    )
    return f"{handoff}{prompt}"
