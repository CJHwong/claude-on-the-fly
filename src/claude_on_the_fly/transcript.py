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
- `find_latest_prior_transcript(workspace, exclude_uuid)` scans both backends'
  per-workspace session stores and returns (turns, from_backend) for the
  newest one — used to seed handoff after a model/backend switch mints a
  fresh session UUID
- `prepend_latest_handoff(workspace, prompt, exclude_uuid)` higher-level
  wrapper that combines find + format + prepend, swallowing scan errors
- `remove_workspace_sessions(workspace)` deletes the session directories a
  backend keyed to a workspace path but stored outside it
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

logger = logging.getLogger(__name__)

BackendName = Literal["claude", "codex", "pi", "opencode"]

# Same separator CodexBackend uses to fence the system prompt off from the user
# prompt. We rsplit on it to recover the raw user text from a codex transcript.
_CODEX_PROMPT_SEPARATOR = "\n\n---\n\n"

CLAUDE_PROJECTS_DIR = (
    Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
)
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
# opencode maps our session_uuid -> its ses_ id under this per-workspace dir;
# the actual session content lives in opencode's global SQLite db, read back
# via `opencode export <ses_id>`.
OPENCODE_SESSIONS_DIRNAME = ".opencode_sessions"


@dataclass(frozen=True)
class Turn:
    role: str  # "user" or "assistant"
    text: str


def _workspace_to_claude_hash(workspace: Path) -> str:
    """`/Users/me/.claude-on-the-fly/foo_bar` -> `-Users-me--claude-on-the-fly-foo-bar`.

    Mirrors claude's own scheme: `/`, `.`, and `_` are all replaced with `-`.
    A leading dotted directory like `.claude-on-the-fly` produces a double
    dash where the dot used to be; underscores in sanitized identifiers
    (like the github PR workspace `owner_repo_123`) are also normalised so
    the hash matches what the claude CLI computes for the same path.

    Resolve symlinks first — the claude CLI resolves `/tmp` → `/private/tmp`
    on macOS before computing the hash.
    """
    return (
        str(workspace.resolve()).replace("/", "-").replace(".", "-").replace("_", "-")
    )


def _workspace_to_pi_hash(workspace: Path) -> str:
    """`/Users/me/Workspace` -> `--Users-me-Workspace--`.

    pi's session directory naming: `--` prefix + path[1:] with `/` → `-` + `--` suffix.
    e.g. `/Users/hoss/ws` → `--Users-hoss-ws--`.

    Resolves symlinks first (macOS `/tmp` → `/private/tmp`).
    """
    resolved = str(workspace.resolve())
    return "--" + resolved[1:].replace("/", "-") + "--"


def remove_workspace_sessions(workspace: Path) -> None:
    """Delete the session directories a backend keys to `workspace` but keeps
    outside it.

    claude and pi both name a directory in their own config tree after the
    workspace path, so a caller that deletes a throwaway workspace still leaves
    that directory behind — and because the name encodes a path that will never
    exist again, nothing can ever reclaim it. codex and opencode keep their
    per-workspace mapping *inside* the workspace, so removing the workspace is
    already enough for them and there is nothing to do here.

    Call this before deleting the workspace: both names are derived from
    `workspace.resolve()`, and resolution is only reliable while the path is
    still there.

    Best-effort by design — a cleanup that cannot run must not mask the
    caller's real outcome.
    """
    for directory in (
        CLAUDE_PROJECTS_DIR / _workspace_to_claude_hash(workspace),
        PI_SESSIONS_DIR / _workspace_to_pi_hash(workspace),
    ):
        shutil.rmtree(directory, ignore_errors=True)


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


def _read_first_jsonl(path: Path) -> dict | None:
    """Parse just the first JSONL record (codex's session_meta) without reading
    the whole file — the rollout can be large and we only need its cwd."""
    try:
        with path.open("rb") as f:
            line = f.readline()
    except OSError:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _find_codex_rollout_by_cwd(cwd: str, *, max_age_s: float = 300.0) -> Path | None:
    """Locate the rollout codex is actively writing for a workspace, by the cwd
    in its session_meta. Needed for *live* tailing: codex only reveals its
    thread id (and we only persist the uuid->thread mapping) after the first
    turn finishes, so a fresh session has no mapping to look up yet.

    Bounded for a 1Hz caller: cheap stat-filter to recently-written rollouts (a
    live run keeps its mtime current), then read only the freshest candidate's
    first line. Old rollouts are skipped without being opened."""
    if not cwd:
        return None
    cutoff = time.time() - max_age_s
    freshest: tuple[float, Path] | None = None
    for path in CODEX_SESSIONS_DIR.glob("**/rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        if freshest is None or mtime > freshest[0]:
            freshest = (mtime, path)
    if freshest is None:
        return None
    meta = _read_first_jsonl(freshest[1])
    if (
        meta is not None
        and meta.get("type") == "session_meta"
        and (meta.get("payload") or {}).get("cwd") == cwd
    ):
        return freshest[1]
    return None


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


# ---------------------------------------------------------------------------
# pi session extraction
# ---------------------------------------------------------------------------


def _find_pi_session_file(workspace: Path, session_uuid: str) -> Path | None:
    """Locate the pi session JSONL for a workspace+uuid combination.

    pi names session files as `<ISO-timestamp>_<uuid>.jsonl`.
    """
    session_dir = PI_SESSIONS_DIR / _workspace_to_pi_hash(workspace)
    if not session_dir.is_dir():
        return None
    candidates = sorted(
        session_dir.glob(f"*_{session_uuid}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def extract_pi(workspace: Path, session_uuid: str) -> list[Turn] | None:
    """Return the user/assistant turns from pi's session JSONL, or None."""
    session_path = _find_pi_session_file(workspace, session_uuid)
    if session_path is None:
        return None
    turns: list[Turn] = []
    for msg in _iter_jsonl(session_path):
        kind = msg.get("type")
        if kind != "message":
            continue
        message = msg.get("message") or {}
        role = message.get("role")
        if role == "user":
            content = message.get("content")
            if isinstance(content, list):
                text_blocks = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                text = " ".join(text_blocks).strip()
            elif isinstance(content, str):
                text = content.strip()
            else:
                text = ""
            if text:
                turns.append(Turn("user", text))
        elif role == "assistant":
            content = message.get("content") or []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = (c.get("text") or "").strip()
                    if text:
                        turns.append(Turn("assistant", text))
                    break
    return turns or None


def _list_pi_session_files(workspace: Path) -> list[tuple[Path, str, float]]:
    """Return (path, uuid, mtime) for every pi JSONL under the workspace's
    session dir. Missing dir → []."""
    session_dir = PI_SESSIONS_DIR / _workspace_to_pi_hash(workspace)
    if not session_dir.is_dir():
        return []
    out: list[tuple[Path, str, float]] = []
    for path in session_dir.glob("*.jsonl"):
        # Filename format: <timestamp>_<uuid>.jsonl
        name = path.stem
        # UUID is the part after the last underscore.
        uuid = name.rsplit("_", 1)[-1]
        # Validate that it looks like a UUID.
        if "-" not in uuid or len(uuid) < 20:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        out.append((path, uuid, mtime))
    return out


# ---------------------------------------------------------------------------
# opencode session extraction
# ---------------------------------------------------------------------------


def _read_opencode_ses_id(workspace: Path, session_uuid: str) -> str | None:
    """Read opencode's ses_ id from our per-workspace mapping file, or None."""
    mapping = workspace / OPENCODE_SESSIONS_DIRNAME / session_uuid
    if not mapping.is_file():
        return None
    try:
        ses_id = mapping.read_text().strip()
    except OSError:
        return None
    return ses_id or None


def _opencode_export(ses_id: str) -> dict | None:
    """Return the parsed `opencode export <ses_id>` JSON, or None on failure.

    opencode stores sessions in a global SQLite db rather than a per-session
    file, so we read them back through the CLI's stable export format instead
    of poking at internal storage. Best-effort: any failure yields None and the
    handoff is simply skipped.
    """
    try:
        result = subprocess.run(
            ["opencode", "export", ses_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("transcript: opencode export failed for ses=%s", ses_id)
        return None
    if result.returncode != 0:
        logger.debug(
            "transcript: opencode export exit=%s for ses=%s",
            result.returncode,
            ses_id,
        )
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def extract_opencode(workspace: Path, session_uuid: str) -> list[Turn] | None:
    """Return the user/assistant turns from an opencode session, or None.

    Resolves our session_uuid to opencode's ses_ id via the mapping file, then
    reads the conversation through `opencode export`. Strips the system-prompt
    prefix we prepend to the first user message.
    """
    ses_id = _read_opencode_ses_id(workspace, session_uuid)
    if ses_id is None:
        return None
    data = _opencode_export(ses_id)
    if data is None:
        return None
    turns: list[Turn] = []
    for message in data.get("messages") or []:
        role = (message.get("info") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        text_blocks = [
            (part.get("text") or "").strip()
            for part in message.get("parts") or []
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = "\n".join(block for block in text_blocks if block).strip()
        if not text:
            continue
        if role == "user":
            # Strip our `<system_prompt>\n\n---\n\n<user_prompt>` prefix.
            text = text.split(_CODEX_PROMPT_SEPARATOR, 1)[-1].strip()
        if text:
            turns.append(Turn(role, text))
    return turns or None


def _list_opencode_session_files(workspace: Path) -> list[tuple[Path, str, float]]:
    """Return (mapping_path, our_uuid, mtime) for every opencode mapping under
    <workspace>/.opencode_sessions. The mapping is rewritten every turn, so its
    mtime tracks the session's recency for handoff ordering."""
    sessions_dir = workspace / OPENCODE_SESSIONS_DIRNAME
    if not sessions_dir.is_dir():
        return []
    out: list[tuple[Path, str, float]] = []
    for mapping in sessions_dir.iterdir():
        if not mapping.is_file():
            continue
        try:
            mtime = mapping.stat().st_mtime
        except OSError:
            continue
        out.append((mapping, mapping.name, mtime))
    return out


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


def _list_claude_session_files(workspace: Path) -> list[tuple[Path, str, float]]:
    """Return (path, uuid, mtime) for every claude JSONL under the workspace's
    project dir. Missing dir → []."""
    project_dir = CLAUDE_PROJECTS_DIR / _workspace_to_claude_hash(workspace)
    if not project_dir.is_dir():
        return []
    out: list[tuple[Path, str, float]] = []
    for path in project_dir.glob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        out.append((path, path.stem, mtime))
    return out


def _list_codex_session_files(workspace: Path) -> list[tuple[Path, str, float]]:
    """Return (rollout_path, our_uuid, rollout_mtime) for every codex mapping
    under <workspace>/.codex_sessions whose rollout still exists."""
    sessions_dir = workspace / ".codex_sessions"
    if not sessions_dir.is_dir():
        return []
    out: list[tuple[Path, str, float]] = []
    for mapping in sessions_dir.iterdir():
        if not mapping.is_file():
            continue
        try:
            thread_id = mapping.read_text().strip()
        except OSError:
            continue
        rollout = _find_codex_rollout(thread_id)
        if rollout is None:
            continue
        try:
            mtime = rollout.stat().st_mtime
        except OSError:
            continue
        out.append((rollout, mapping.name, mtime))
    return out


def find_latest_prior_transcript(
    workspace: Path,
    *,
    exclude_uuid: str | None = None,
) -> tuple[list[Turn], BackendName] | None:
    """Newest prior transcript for this workspace, across all backend_keys.

    Scans claude's per-workspace project dir, codex's per-workspace sessions
    dir, and pi's per-workspace sessions dir, picks the file with the newest
    mtime (excluding `exclude_uuid` so the current session never matches
    itself), runs the matching extractor, and returns (turns, from_backend).

    Returns None when no prior session exists, or the newest one yields no
    extractable turns. Used by all backends to seed a handoff preamble when
    a new (source, backend_key, ticket) combo starts fresh — typically right
    after a model switch.
    """
    candidates: list[tuple[float, str, BackendName, str]] = []
    for _path, uuid, mtime in _list_claude_session_files(workspace):
        if uuid == exclude_uuid:
            continue
        candidates.append((mtime, uuid, "claude", uuid))
    for _path, uuid, mtime in _list_codex_session_files(workspace):
        if uuid == exclude_uuid:
            continue
        candidates.append((mtime, uuid, "codex", uuid))
    for _path, uuid, mtime in _list_pi_session_files(workspace):
        if uuid == exclude_uuid:
            continue
        candidates.append((mtime, uuid, "pi", uuid))
    for _path, uuid, mtime in _list_opencode_session_files(workspace):
        if uuid == exclude_uuid:
            continue
        candidates.append((mtime, uuid, "opencode", uuid))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    extractors: dict[BackendName, Callable[[Path, str], list[Turn] | None]] = {
        "claude": extract_claude,
        "codex": extract_codex,
        "pi": extract_pi,
        "opencode": extract_opencode,
    }
    for _mtime, _uuid, backend, lookup_uuid in candidates:
        try:
            turns = extractors[backend](workspace, lookup_uuid)
        except Exception:
            logger.exception(
                "transcript: %s extraction failed for uuid=%s; trying next",
                backend,
                lookup_uuid,
            )
            continue
        if turns:
            return turns, backend
    return None


def prepend_latest_handoff(
    workspace: Path,
    prompt: str,
    *,
    exclude_uuid: str | None = None,
) -> str:
    """Return `prompt` with a preamble drawn from the newest prior transcript
    (any backend) for this workspace. No-op when nothing prior exists.

    Use this from both backends after a fresh-session branch (no JSONL for
    the current uuid) so the new model picks up context written by whatever
    backend ran last — including a different mode of the *same* CLI.

    Wraps the lookup in a broad except so a misbehaving transcript scan never
    crashes the caller; the user still gets a reply, just without handoff
    context."""
    try:
        found = find_latest_prior_transcript(workspace, exclude_uuid=exclude_uuid)
    except Exception:
        logger.exception("transcript: latest-prior lookup failed; starting clean")
        return prompt
    if found is None:
        return prompt
    turns, from_backend = found
    handoff = format_handoff(turns, from_backend=from_backend)
    if not handoff:
        return prompt
    logger.info(
        "transcript: forwarding %d %s turn(s) to next backend for workspace=%s",
        len(turns),
        from_backend,
        workspace,
    )
    return f"{handoff}{prompt}"


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
