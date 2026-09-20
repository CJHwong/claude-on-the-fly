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
- `find_latest_prior_transcript(workspace, exclude_uuid, only_uuid)` scans both
  backends' per-workspace session stores and returns (turns, from_backend) for
  the newest one, used to seed handoff after a backend switch
- `prepend_latest_handoff(workspace, prompt, session_uuid, platform)` higher-level
  wrapper that combines find + format + prepend, swallowing scan errors. On a
  shared chat workspace only the same session's other-backend file counts
- `remove_workspace_sessions(workspace)` deletes the session directory a
  backend keyed to a workspace path but stored outside it
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
    looking at different directories, and the live view reported "agent hasn't
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


def claude_session_size(workspace: Path, session_uuid: str) -> int:
    """Bytes in this session's JSONL right now. 0 when the file does not exist.

    Taken before a turn spawns so `extract_claude_turn_tokens` can tell this
    turn's assistant records from every earlier turn's. A session that does not
    exist yet reports 0, which makes a first turn read its whole file.
    """
    path = claude_session_dir(workspace) / f"{session_uuid}.jsonl"
    try:
        return path.stat().st_size
    except OSError:
        return 0


def extract_claude_turn_tokens(
    workspace: Path, session_uuid: str, offset: int
) -> tuple[int, int] | None:
    """Sum `(tokens_in, tokens_out)` over assistant records written after
    `offset`. None when the file is unreadable or added no priced record.

    This exists because claude-pty's envelope cannot answer it. Its `usage` is
    the last assistant message alone, and its `modelUsage` is pty's own pass
    over the *whole session*, so a footer built from either is wrong in a
    different direction: the first undercounts a multi-message turn, the second
    reports every turn the session has ever run. A daemon thread footered
    `up-arrow 990272` while its context read 8 percent, because the number was
    the session's life story rather than the turn's cost.

    Reading by byte offset rather than re-deriving pty's total is deliberate. It
    needs no agreement with how pty defines its sum; it counts what this turn
    appended, the same way the codex backend reads its rollout from a
    pre-spawn size.
    """
    path = claude_session_dir(workspace) / f"{session_uuid}.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read()
    except OSError:
        return None
    tokens_in = 0
    tokens_out = 0
    found = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A seek into the middle of a line is the ordinary cause: the
            # snapshot was taken while claude was mid-write. Skipping it loses
            # one record's tokens, which beats discarding the turn's whole count.
            logger.debug("transcript: skipping malformed line in %s", path)
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        usage = (record.get("message") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        found = True
        tokens_in += _as_int(usage.get("input_tokens")) + _as_int(
            usage.get("cache_read_input_tokens")
        )
        tokens_out += _as_int(usage.get("output_tokens"))
    return (tokens_in, tokens_out) if found else None


def _as_int(value: object) -> int:
    """Non-negative int, or 0. A transcript is another process's output."""
    return value if isinstance(value, int) and value >= 0 else 0


def extract_claude_model(workspace: Path, session_uuid: str) -> str | None:
    """Return the model that produced the last assistant message, or None.

    Only pty needs this. The native path reads the model off the stream it
    already parses, but claude-pty's envelope carries no per-message model, so
    the only name available there is `modelUsage`, which is keyed by every model
    the turn touched in first-touched order. An auxiliary call that ran before
    the answer therefore names itself: a daemon turn opus answered footered as
    `claude-haiku-4-5-20251001`, and the session JSONL for the same second
    recorded `claude-opus-5`.

    The last assistant message is the one that produced the answer, so its own
    `model` is the name to report. A long turn writes many assistant records,
    so every one is read and the last named model wins.
    """
    session_path = claude_session_dir(workspace) / f"{session_uuid}.jsonl"
    if not session_path.is_file():
        return None
    model: str | None = None
    for msg in _iter_jsonl(session_path):
        if msg.get("type") != "assistant":
            continue
        name = (msg.get("message") or {}).get("model")
        if isinstance(name, str) and name:
            model = name
    return model


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


def snapshot_rollouts() -> frozenset[Path]:
    """Every rollout on disk right now, for `_find_codex_rollout_by_cwd`'s
    `exclude`. A turn takes this before it spawns, so the rollouts of the other
    threads already running in the same directory cannot be mistaken for its own."""
    return frozenset(_iter_rollouts("**/rollout-*.jsonl"))


def _find_codex_rollout_by_cwd(
    cwd: str, *, max_age_s: float = 300.0, exclude: frozenset[Path] = frozenset()
) -> Path | None:
    """Locate the rollout codex is actively writing for a workspace, by the cwd
    in its session_meta. Needed for *live* tailing: codex only reveals its
    thread id (and we only persist the uuid->thread mapping) after the first
    turn finishes, so a fresh session has no mapping to look up yet.

    Bounded for a 1Hz caller: cheap stat-filter to recently-written rollouts (a
    live run keeps its mtime current), then read only the freshest candidate's
    first line. Old rollouts are skipped without being opened.

    A workspace is shared by every thread of one conversation, so the cwd alone
    no longer names one session. `exclude` holds the rollouts that existed when
    this turn started: a sibling thread resumed in the same directory keeps its
    own rollout fresh, and it would otherwise be the freshest match."""
    if not cwd:
        return None
    cutoff = time.time() - max_age_s
    freshest: tuple[float, Path] | None = None
    for path in _iter_rollouts("**/rollout-*.jsonl"):
        if path in exclude:
            continue
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
    if meta is None or meta.get("type") != "session_meta":
        return None
    recorded = (meta.get("payload") or {}).get("cwd")
    if isinstance(recorded, str) and _same_dir(recorded, cwd):
        return freshest[1]
    return None


def _same_dir(recorded: str, wanted: str) -> bool:
    """Whether two cwd strings name the same directory, symlinks resolved.

    A plain string equality was wrong in both directions, because codex writes
    back the `-C` argument verbatim rather than a path it resolved. Measured on
    codex-cli 0.155.1: a turn given `-C /private/tmp/x/link_ws`, where `link_ws`
    is a symlink, records exactly that, so a caller comparing the resolved name
    matched nothing and the turn returned "No response" -- after codex had run
    the work and billed for it. The control through the real path answered
    normally.

    Resolving both sides is what makes the comparison independent of which form
    the caller happened to hold. `realpath` needs no existing path: it resolves
    the components that do exist and normalises the rest.
    """
    return os.path.realpath(recorded) == os.path.realpath(wanted)


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

    The last `turn_context` wins, not the first. A resumed thread appends one
    per turn, so the first records the model the thread opened with and keeps
    naming it after a switch. That is not hypothetical: a thread started on
    `gpt-5.6-luna` and moved to `gpt-5.6-terra` ran terra, by both its argv and
    its own rollout, and every later footer still said luna.
    """
    rollout = _find_codex_rollout(thread_id)
    if rollout is None:
        return None
    model: str | None = None
    for msg in _iter_jsonl(rollout):
        if msg.get("type") != "turn_context":
            continue
        name = (msg.get("payload") or {}).get("model")
        if isinstance(name, str) and name:
            model = name
    return model


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
    only_uuid: str | None = None,
) -> tuple[list[Turn], BackendName] | None:
    """Newest prior transcript for this workspace, across all backend_keys.

    Scans claude's per-workspace project dir and codex's per-workspace sessions
    dir, picks the file with the newest mtime (excluding `exclude_uuid` so the
    current session never matches itself), runs the matching extractor, and
    returns (turns, from_backend).

    `only_uuid` narrows the scan to one session's files. A chat workspace holds
    every thread of a conversation, so "the newest transcript here" is usually a
    different thread; the handoff a backend switch wants is this same session as
    the other backend wrote it, and that file carries the same uuid.

    Returns None when no prior session exists, or the newest one yields no
    extractable turns. Used by all backends to seed a handoff preamble when
    a new (source, backend_key, ticket) combo starts fresh — typically right
    after a model switch.
    """
    candidates: list[tuple[float, str, BackendName, str]] = []
    for _path, uuid, mtime in _list_claude_session_files(workspace):
        if _wanted(uuid, exclude_uuid, only_uuid):
            candidates.append((mtime, uuid, "claude", uuid))
    for _path, uuid, mtime in _list_codex_session_files(workspace):
        if _wanted(uuid, exclude_uuid, only_uuid):
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


def _wanted(uuid: str, exclude_uuid: str | None, only_uuid: str | None) -> bool:
    if uuid == exclude_uuid:
        return False
    return only_uuid is None or uuid == only_uuid


def prepend_latest_handoff(
    workspace: Path,
    prompt: str,
    *,
    session_uuid: str,
    platform: str,
) -> str:
    """Return `prompt` with a preamble drawn from the prior transcript for this
    session, or for this workspace. No-op when nothing prior exists.

    Use this from both backends after a fresh-session branch (no JSONL for
    the current uuid) so the new model picks up context written by whatever
    backend ran last — including a different mode of the *same* CLI.

    Which transcript counts as prior depends on the platform. On a platform in
    `agent.SHARED_WORKSPACE_PLATFORMS` only the same session's file on another
    backend does. Elsewhere (cron) any other session in the workspace does, and
    the current uuid is excluded so it never matches itself.

    Wraps the lookup in a broad except so a misbehaving transcript scan never
    crashes the caller; the user still gets a reply, just without handoff
    context."""
    from claude_on_the_fly.agent import SHARED_WORKSPACE_PLATFORMS

    if platform in SHARED_WORKSPACE_PLATFORMS:
        scope: dict[str, str] = {"only_uuid": session_uuid}
    else:
        scope = {"exclude_uuid": session_uuid}
    try:
        found = find_latest_prior_transcript(workspace, **scope)
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


# --- failure diagnosis (experimental) ---

# A rollout is looked up after its run already died, so the mtime filter
# `_find_codex_rollout_by_cwd` uses for live tailing is far too tight. A job may
# have run for its whole timeout before failing, and the alert is built after
# that. One hour covers the longest configured timeout with room to spare.
DIAGNOSE_ROLLOUT_MAX_AGE_S = 3600.0
# Upper bound on rollouts opened while looking for one run's, so a busy
# store cannot turn a diagnosis into a full scan of the sessions tree.
_MAX_ROLLOUT_CANDIDATES = 200
# Below this share of the wall clock spent inside tool calls, the run was
# waiting on the model, not doing work. Measured, not guessed: a healthy fire of
# the same entry spends whole seconds in execs, a stalled one spends tenths.
STALL_TOOL_SHARE = 0.05
# A stall claim needs most of the budget consumed, or a slow-but-working run
# reads as a stall. Runs with no configured timeout fall back to the constant.
STALL_TIMEOUT_SHARE = 0.8
DEFAULT_STALL_FLOOR_S = 300.0
# One failing exec is noise; a run that keeps hitting the same wall is missing a
# capability. Three is the smallest count that cannot be a retry pair.
CAPABILITY_GAP_ERRORS = 3
# Substrings that mark a tool result as a failure. Lowercased before matching.
_TOOL_ERROR_MARKERS = (
    "not safe to open",
    "command not found",
    "no such file",
    "permission denied",
)
# Paths in the prompt that name something to execute. A cron entry says "run
# this script"; if the script never appears in a tool call, the run never
# started its actual work, which is a different failure from crashing during it.
_PAYLOAD_PATTERN = re.compile(r"[\w./~-]+\.(?:py|sh)\b")


def _find_finished_rollout_by_cwd(cwd: str, *, max_age_s: float) -> Path | None:
    """The rollout a *finished* run wrote for a workspace, newest first.

    Deliberately not `_find_codex_rollout_by_cwd`, which reads only the single
    freshest rollout in the store. That is right for its 1Hz live tailer, where
    the run being watched is by definition the freshest file. It is wrong here:
    a failed run is diagnosed after the fact, and on a host firing cron every
    15 minutes several newer rollouts already exist. Checking only the freshest
    found nothing on every real failure it was tried against.

    So this walks candidates newest first and stops at the first cwd match,
    reading one line each. It runs once per failed job rather than every
    second, and `_MAX_ROLLOUT_CANDIDATES` keeps a busy store from turning that
    into an unbounded scan.
    """
    if not cwd:
        return None
    cutoff = time.time() - max_age_s
    candidates: list[tuple[float, Path]] = []
    for path in _iter_rollouts("**/rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            candidates.append((mtime, path))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for _, path in candidates[:_MAX_ROLLOUT_CANDIDATES]:
        meta = _read_first_jsonl(path)
        if (
            meta is not None
            and meta.get("type") == "session_meta"
            and (meta.get("payload") or {}).get("cwd") == cwd
        ):
            return path
    return None


def _diagnose_rollout(workspace: Path, session_uuid: str) -> Path | None:
    """The rollout for a finished run, by thread id when one was persisted.

    A run that died inside its first turn never got a thread id written, which
    is the case this feature exists to explain, so the cwd scan is the path
    that matters rather than the fallback it looks like.
    """
    thread_id = codex_state.read_thread_id(workspace, session_uuid)
    if thread_id:
        rollout = _find_codex_rollout(thread_id)
        if rollout is not None:
            return rollout
    return _find_finished_rollout_by_cwd(
        str(workspace), max_age_s=DIAGNOSE_ROLLOUT_MAX_AGE_S
    )


@dataclass(frozen=True)
class _RunFacts:
    """What one finished run's transcript says, in the terms the rules need.

    The two backends write different files, so each has its own reader; every
    rule below reads this instead. `marker` is the backend's own name for "the
    agent finished" -- `task_complete` for codex, `end_turn` for claude -- so a
    signal names the event an operator will grep the transcript for.
    """

    marker: str
    completed: bool
    last_event: str
    span: float
    tool_wall: float
    calls: int
    outputs: int
    failed: int
    tool_inputs: str


def _codex_tool_spans(rows: list[dict]) -> tuple[float, int, int, int]:
    """`(seconds inside tool calls, calls, outputs, failed outputs)`.

    Codex writes a call and its output as separate records, so the time a tool
    actually took is the gap between the pair. Summing those and subtracting
    from the run's span is what separates "the model was slow" from "the work
    was slow", which no single field in the rollout answers.
    """
    tool_wall = 0.0
    calls = outputs = failed = 0
    open_calls: dict[object, float | None] = {}
    for row in rows:
        payload = row.get("payload") or {}
        kind = payload.get("type")
        if kind == "custom_tool_call":
            calls += 1
            open_calls[payload.get("call_id") or calls] = _row_time(row)
        elif kind == "custom_tool_call_output":
            outputs += 1
            started = open_calls.pop(payload.get("call_id") or outputs, None)
            ended = _row_time(row)
            if started is not None and ended is not None:
                tool_wall += max(0.0, ended - started)
            if _looks_like_tool_error(payload.get("output")):
                failed += 1
    return tool_wall, calls, outputs, failed


def _looks_like_tool_error(output: object) -> bool:
    """Whether a tool result reads as a failure rather than a result."""
    blob = json.dumps(output, default=str).lower()
    return any(marker in blob for marker in _TOOL_ERROR_MARKERS)


def _row_time(row: dict) -> float | None:
    """A rollout record's timestamp as epoch seconds, or None if unusable."""
    stamp = row.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _span(rows: list[dict]) -> float:
    """Wall clock across rows that carry a usable timestamp, or 0.0."""
    stamps = [stamp for stamp in (_row_time(row) for row in rows) if stamp is not None]
    return (max(stamps) - min(stamps)) if stamps else 0.0


def _codex_facts(rows: list[dict]) -> _RunFacts:
    """Read a codex rollout."""
    last = (rows[-1].get("payload") or {}).get("type") or rows[-1].get("type")
    tool_wall, calls, outputs, failed = _codex_tool_spans(rows)
    inputs = "\n".join(
        json.dumps((row.get("payload") or {}).get("input", ""), default=str)
        for row in rows
        if (row.get("payload") or {}).get("type") == "custom_tool_call"
    )
    return _RunFacts(
        marker="task_complete",
        completed=last == "task_complete",
        last_event=str(last),
        span=_span(rows),
        tool_wall=tool_wall,
        calls=calls,
        outputs=outputs,
        failed=failed,
        tool_inputs=inputs,
    )


def _claude_last_fire(rows: list[dict]) -> list[dict]:
    """The rows of the newest turn in a claude session file.

    A keyed job resumes its session, so the file holds every earlier fire too,
    and reading the lot would time the run in hours and explain today's failure
    with last week's tool calls. The boundary is the last prompt claude was
    given: a `user` row whose content is a plain string. A tool result is a
    `user` row as well, but its content is a list, and a subagent's prompt is a
    string but sits on a sidechain -- neither starts a fire.
    """
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if row.get("type") != "user" or row.get("isSidechain"):
            continue
        if isinstance((row.get("message") or {}).get("content"), str):
            return rows[index:]
    return rows


def _claude_blocks(row: dict, kind: str) -> list[dict]:
    """The content blocks of one type in a claude message row."""
    content = (row.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == kind
    ]


def _claude_tool_spans(rows: list[dict]) -> tuple[float, int, int, int]:
    """`(seconds inside tool calls, calls, outputs, failed outputs)`.

    Claude pairs a `tool_use` block on an assistant row with a `tool_result`
    block on the user row that answers it, so the gap between the two rows is
    what the tool took. Sidechain rows count: a subagent's execs are this run's
    work, and leaving them out would read a delegating run as a stalled one.

    A claude tool result says `is_error` outright, which codex's format does
    not. The substring markers still apply on top of it: a wrapper that reports
    a failure in its own text without setting the flag is the case they catch.
    """
    tool_wall = 0.0
    calls = outputs = failed = 0
    open_calls: dict[object, float | None] = {}
    for row in rows:
        for block in _claude_blocks(row, "tool_use"):
            calls += 1
            open_calls[block.get("id") or calls] = _row_time(row)
        for block in _claude_blocks(row, "tool_result"):
            outputs += 1
            started = open_calls.pop(block.get("tool_use_id") or outputs, None)
            ended = _row_time(row)
            if started is not None and ended is not None:
                tool_wall += max(0.0, ended - started)
            if block.get("is_error") or _looks_like_tool_error(block.get("content")):
                failed += 1
    return tool_wall, calls, outputs, failed


def _claude_facts(rows: list[dict]) -> _RunFacts:
    """Read a claude session file.

    Finishing is `end_turn` **anywhere** in the fire, not on its last row. A
    completed turn is followed by rows of its own: `system` rows for the stop
    hook and the turn duration, then untimestamped bookkeeping (`mode`,
    `ai-title`, `last-prompt`). Reading the stop reason off the last row called
    9 of 96 real finished fires unfinished, and `turn_duration` is written by
    exactly the runs that did finish.

    The event to report is the last message row, so an interrupted turn is named
    by what the agent was doing -- `assistant/tool_use` for one killed holding a
    tool call, `assistant/stop_sequence` for one the API cut off. A fire with no
    message row at all (the agent never answered) falls back to the last row
    that carries a timestamp.
    """
    spoke = [row for row in rows if row.get("type") in {"assistant", "user"}]
    timed = [row for row in rows if _row_time(row) is not None]
    last = (spoke or timed or rows)[-1]
    stop = (last.get("message") or {}).get("stop_reason")
    label = f"{last.get('type')}/{stop}" if stop else str(last.get("type"))
    completed = any(
        (row.get("message") or {}).get("stop_reason") == "end_turn"
        for row in rows
        if not row.get("isSidechain")
    )
    tool_wall, calls, outputs, failed = _claude_tool_spans(rows)
    inputs = "\n".join(
        json.dumps(block.get("input", ""), default=str)
        for row in rows
        for block in _claude_blocks(row, "tool_use")
    )
    return _RunFacts(
        marker="end_turn",
        completed=completed,
        last_event=label,
        span=_span(rows),
        tool_wall=tool_wall,
        calls=calls,
        outputs=outputs,
        failed=failed,
        tool_inputs=inputs,
    )


def _signals(facts: _RunFacts, prompt: str, timeout_s: float | None) -> list[str]:
    """The rules themselves, over whichever backend's facts."""
    signals: list[str] = []
    if facts.completed:
        # The agent finished and the job still failed, so the fault is on our
        # side of the CLI boundary. Without this the alert reads as an agent
        # crash and sends the operator to the wrong component.
        signals.append(
            f"agent reached {facts.marker} in {facts.span:.0f}s, failure is ours"
        )
    else:
        signals.append(f"no {facts.marker}, last event was {facts.last_event}")

    floor = timeout_s * STALL_TIMEOUT_SHARE if timeout_s else DEFAULT_STALL_FLOOR_S
    if facts.span >= floor and facts.tool_wall < facts.span * STALL_TOOL_SHARE:
        signals.append(
            f"{facts.span - facts.tool_wall:.0f}s model / {facts.tool_wall:.1f}s tool, "
            "stalled upstream"
        )

    if facts.calls:
        for payload_name in dict.fromkeys(_PAYLOAD_PATTERN.findall(prompt)):
            if payload_name not in facts.tool_inputs:
                signals.append(
                    f"payload never ran, {payload_name} absent from "
                    f"{facts.calls} tool calls"
                )

    if facts.failed >= CAPABILITY_GAP_ERRORS:
        signals.append(
            f"{facts.failed}/{facts.outputs} tool results were errors, capability gap?"
        )
    return signals


def diagnose_codex(
    workspace: Path,
    session_uuid: str,
    *,
    prompt: str = "",
    timeout_s: float | None = None,
) -> list[str]:
    """Deterministic signals about why a codex run failed, newest evidence only.

    See `diagnose` for what the signals are and why they exist. This arm reads
    a codex rollout, which is found by thread id when the run got far enough to
    persist one and by a cwd scan when it did not.
    """
    rollout = _diagnose_rollout(workspace, session_uuid)
    if rollout is None:
        return []
    rows = list(_iter_jsonl(rollout))
    if not rows:
        return []
    return _signals(_codex_facts(rows), prompt, timeout_s)


def diagnose_claude(
    workspace: Path,
    session_uuid: str,
    *,
    prompt: str = "",
    timeout_s: float | None = None,
) -> list[str]:
    """Deterministic signals about why a claude run failed, newest fire only.

    See `diagnose` for what the signals are and why they exist. This arm needs
    no search: claude names the session file after the id cotf gave it, under
    the directory it derives from the workspace path.
    """
    path = claude_session_dir(workspace) / f"{session_uuid}.jsonl"
    rows = _claude_last_fire(list(_iter_jsonl(path)))
    if not rows:
        return []
    return _signals(_claude_facts(rows), prompt, timeout_s)


_DIAGNOSERS = {"codex": diagnose_codex, "claude": diagnose_claude}


def diagnose(
    backend: str,
    workspace: Path,
    session_uuid: str,
    *,
    prompt: str = "",
    timeout_s: float | None = None,
) -> list[str]:
    """Deterministic signals about why a run failed, for either backend.

    Experimental. The alert a failed job already sends says what the CLI
    reported, which for a timeout is only that it timed out. These signals
    answer the next question an operator asks, and they are arithmetic over
    timestamps rather than an interpretation: whether the agent finished at all,
    where the wall clock went, whether the job's own payload ever ran, and
    whether the run kept hitting the same failing tool.

    Returns an empty list when the backend writes neither transcript format,
    when there is no transcript, or when nothing stands out, so a caller can
    append unconditionally.
    """
    reader = _DIAGNOSERS.get(backend)
    if reader is None:
        return []
    return reader(workspace, session_uuid, prompt=prompt, timeout_s=timeout_s)
