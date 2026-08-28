"""Cross-backend conversation transcript extraction and handoff.

Each agent CLI writes its own session JSONL. When a daemon restarts under a
different backend, we look up the prior backend's transcript for the same
session_uuid and prepend a short handoff preamble to the next user prompt so
context survives the switch.

Public surface:
- `Turn` dataclass
- `extract_claude(workspace, session_uuid)` reads ~/.claude/projects/<hash>/<uuid>.jsonl
- `extract_codex(workspace, session_uuid)` reads the codex rollout matching the
  thread_id persisted in the daemon-owned ~/.claude-on-the-fly/codex-sessions/
  store, outside the agent-writable workspace
- `format_handoff(turns, from_backend)` renders a labeled preamble, capped by
  turn count and char budget from the most recent backward
- `find_latest_prior_transcript(workspace, exclude_uuid)` scans both backends'
  per-workspace session stores and returns (turns, from_backend) for the
  newest one, used to seed handoff after a model/backend switch mints a
  fresh session UUID
- `prepend_latest_handoff(workspace, prompt, exclude_uuid)` higher-level
  wrapper that combines find + format + prepend, swallowing scan errors
- `remove_workspace_sessions(workspace)` deletes the session directory a
  backend keyed to a workspace path but stored outside it
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from claude_on_the_fly import codex_state, envfile

logger = logging.getLogger(__name__)

BackendName = Literal["claude", "codex"]

# Same separator CodexBackend uses to fence the system prompt off from the user
# prompt. We rsplit on it to recover the raw user text from a codex transcript.
_CODEX_PROMPT_SEPARATOR = "\n\n---\n\n"


def codex_sessions_dirs() -> list[Path]:
    """Every directory a codex rollout may be in, newest layout first.

    Resolved per call rather than bound at import, for the reason
    `claude_projects_dir` documents: a module constant answers according to
    whoever imported the module, and the daemon that writes and the viewer that
    reads are not the same process.

    Each workspace now gets its own `CODEX_HOME` (see `codex_state.home_dir`), so
    the rollouts are spread across one directory per thread. The daemon is not
    jailed and owns all of them, so it searches the lot: the isolation is enforced
    by the jail granting one home per turn, not by narrowing this lookup. Keeping
    the search wide is what lets `_find_codex_rollout(thread_id)` and its five
    callers stay as they are -- several hold only a thread id, never a workspace.

    The shared tree is still searched, and last: rollouts written before this
    existed live there, and dropping it would make old threads look empty.
    """
    dirs: list[Path] = []
    try:
        dirs = sorted(
            path / "sessions"
            for path in codex_state.HOMES_DIR.iterdir()
            if path.is_dir()
        )
    except OSError:
        dirs = []
    dirs.append(envfile.codex_home() / "sessions")
    return dirs


def _iter_rollouts(pattern: str):
    """`pattern` matched across every codex sessions directory."""
    for base in codex_sessions_dirs():
        yield from base.glob(pattern)


def claude_projects_dir() -> Path:
    """Where claude keeps session JSONL, resolved per call.

    A module constant read this from `os.environ` at import, which made the
    answer depend on who imported the module. The daemon writing the logs is
    spawned with `DATA_DIR/.env` merged in; the TUI reading them is not, so a
    deployment that sets `CLAUDE_CONFIG_DIR` in that file had the two processes
    looking at different directories, and the watch pane reported "agent hasn't
    run a turn" over a session that was streaming. Resolving through
    `envfile` per call is what makes the reader agree with the writer.
    """
    return envfile.claude_config_dir() / "projects"


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


def claude_session_dir(workspace: Path) -> Path:
    """The `projects/` subdirectory holding this workspace's session JSONL.

    One workspace is one chat thread (`orchestrator._process` derives it from the
    frontend's chat id), so this path is also the boundary between one thread's
    transcripts and every other thread's. The jail grants it by name for exactly
    that reason: the claude CLI runs *inside* the jail and writes its own session
    file, so it needs this directory and nothing else under `projects/`.
    """
    return claude_projects_dir() / _workspace_to_claude_hash(workspace)


def remove_workspace_sessions(workspace: Path) -> None:
    """Delete the session directory a backend keys to `workspace` but keeps
    outside it.

    claude names a directory in its own config tree after the workspace path, so
    a caller that deletes a throwaway workspace still leaves that directory
    behind — and because the name encodes a path that will never exist again,
    nothing can ever reclaim it. codex keeps its per-workspace mapping in the
    daemon-owned store, so remove those records by exact workspace identity too.

    Call this before deleting the workspace: the name is derived from
    `workspace.resolve()`, and resolution is only reliable while the path is
    still there.

    Best-effort by design — a cleanup that cannot run must not mask the
    caller's real outcome.
    """
    shutil.rmtree(claude_session_dir(workspace), ignore_errors=True)
    codex_state.remove_workspace(workspace)


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
    session_path = claude_session_dir(workspace) / f"{session_uuid}.jsonl"
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
        _iter_rollouts(f"**/{codex_state.rollout_glob(thread_id)}"),
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
    for path in _iter_rollouts("**/rollout-*.jsonl"):
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
    thread_id = codex_state.read_thread_id(workspace, session_uuid)
    if thread_id is None:
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


def codex_rollout_path(thread_id: str) -> Path | None:
    """The rollout file a token snapshot for this thread would be read from.

    Exposed so a caller can pin it. `_find_codex_rollout` picks the newest file
    by mtime across every sessions root, and one thread can legitimately have a
    copy in more than one root: `codex_state.adopt_rollout` puts one in the
    workspace home and leaves the original in the shared tree. The two then
    diverge, so a before/after pair read by thread id alone can come from two
    different histories and subtract to a negative.
    """
    return _find_codex_rollout(thread_id)


def extract_codex_usage_events(thread_id: str) -> list[dict]:
    """Every `last_token_usage` this thread has recorded, oldest first.

    Each entry is one model call's own counts. Counting how many exist before
    an exec and summing whatever it appends gives that exec's true cost, which
    a subtraction cannot: codex 0.150.1 writes the same figures into
    `total_token_usage`, so that field is per-call rather than a running total.
    Diffing it undercounted input by three orders of magnitude and rendered a
    negative whenever one call produced fewer output tokens than the one before.

    A call fanning out to several model calls appends several events, so the
    sum covers the whole exec rather than only its last step.
    """
    rollout = _find_codex_rollout(thread_id)
    if rollout is None:
        return []
    events: list[dict] = []
    for msg in _iter_jsonl(rollout):
        if msg.get("type") != "event_msg":
            continue
        payload = msg.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        last = (payload.get("info") or {}).get("last_token_usage")
        if isinstance(last, dict):
            events.append(last)
    return events


def extract_codex_prompt_tokens(thread_id: str) -> tuple[int, int] | None:
    """`(prompt_tokens, context_window)` for a codex thread's most recent turn.

    Unlike `total_token_usage` above, `last_token_usage.input_tokens` is that
    turn's own prompt — so it tracks how big the thread's context has become,
    which is the number a compaction is supposed to shrink. Compaction itself
    reports a turn with `input_tokens: 0`, so those are skipped: they describe
    the compaction pass, not the context it left behind.

    None when the rollout is missing or has no usable event. Codex publishes no
    in-band compaction signal in `--json`, so comparing this before and after is
    the only way to tell whether a compaction actually did anything.
    """
    rollout = _find_codex_rollout(thread_id)
    if rollout is None:
        return None
    prompt: int | None = None
    window = 0
    for msg in _iter_jsonl(rollout):
        if msg.get("type") != "event_msg":
            continue
        payload = msg.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        last = info.get("last_token_usage")
        if isinstance(last, dict) and int(last.get("input_tokens") or 0) > 0:
            prompt = int(last["input_tokens"])
        if info.get("model_context_window"):
            window = int(info["model_context_window"])
    return (prompt, window) if prompt is not None else None


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
    project_dir = claude_projects_dir() / _workspace_to_claude_hash(workspace)
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
    in the daemon-owned store whose rollout still exists."""
    out: list[tuple[Path, str, float]] = []
    for (
        _mapping_path,
        session_uuid,
        _mapping_mtime,
    ) in codex_state.mappings_for_workspace(workspace):
        thread_id = codex_state.read_thread_id(workspace, session_uuid)
        if thread_id is None:
            continue
        rollout = _find_codex_rollout(thread_id)
        if rollout is None:
            continue
        try:
            mtime = rollout.stat().st_mtime
        except OSError:
            continue
        out.append((rollout, session_uuid, mtime))
    return out


def find_latest_prior_transcript(
    workspace: Path,
    *,
    exclude_uuid: str | None = None,
) -> tuple[list[Turn], BackendName] | None:
    """Newest prior transcript for this workspace, across all backend_keys.

    Scans claude's per-workspace project dir and codex's per-workspace sessions
    dir, picks the file with the newest mtime (excluding `exclude_uuid` so the
    current session never matches itself), runs the matching extractor, and
    returns (turns, from_backend).

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
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    extractors: dict[BackendName, Callable[[Path, str], list[Turn] | None]] = {
        "claude": extract_claude,
        "codex": extract_codex,
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
