"""Fold one per-thread workspace into the shared workspace it now belongs to.

Workspaces used to be one directory per conversation thread. They are now one
directory per conversation (a Slack DM, group DM or channel; a Telegram chat), and
every thread in it is a separate session running in that one directory. The old
directories keep their sessions resumable only while they exist under their old
path, because both backends key their session store on the directory path:
claude under `projects/<hash of path>/<uuid>.jsonl`, codex through a mapping
keyed on the path and a `CODEX_HOME` named after it.

So a thread's first message after the upgrade moves three things: the claude
transcript to the new hash directory, the codex mapping and rollout to the new
workspace's home, and whatever files the thread left behind to
`<new>/threads/<key>/`. Both CLIs were measured resuming from a moved directory
with the files moved this way; they record the new cwd from then on.

Every step is idempotent and best effort. A move that fails leaves its source in
place and is logged, and the turn still runs in the new workspace: a forgetful
turn beats a failed one, which is the same call `codex_state.adopt_rollout` makes.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from claude_on_the_fly import codex_state, transcript
from claude_on_the_fly.agent import PERSONA_FILENAMES, workspace_path

logger = logging.getLogger(__name__)

THREADS_DIRNAME = "threads"


def migrate_thread(
    old_workspace: Path,
    new_workspace: Path,
    session_uuids: Iterable[str],
    thread_key: str,
) -> bool:
    """Move one thread's sessions and files out of `old_workspace`.

    `session_uuids` are the sessions to carry; the live path passes the one it is
    about to run, the bulk pass everything it found. `thread_key` names the
    subdirectory the leftover files go under. Returns True when anything moved.
    """
    if not old_workspace.is_dir():
        return False
    if old_workspace.resolve() == new_workspace.resolve(strict=False):
        return False
    moved = False
    for session_uuid in session_uuids:
        logger.info(
            "migrating %s -> %s (session %s)",
            old_workspace,
            new_workspace,
            session_uuid,
        )
        moved = (
            _move_claude_session(old_workspace, new_workspace, session_uuid) or moved
        )
        moved = _move_codex_session(old_workspace, new_workspace, session_uuid) or moved
    moved = (
        _move_files(old_workspace, new_workspace / THREADS_DIRNAME / thread_key)
        or moved
    )
    return moved


def _move(source: Path, target: Path) -> bool:
    """Move one entry, refusing to overwrite. False when nothing moved."""
    if target.exists() or target.is_symlink():
        logger.warning("migration: %s already exists, leaving %s", target, source)
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as exc:
        logger.warning("migration: cannot move %s -> %s: %s", source, target, exc)
        return False
    return True


def _move_claude_session(old: Path, new: Path, session_uuid: str) -> bool:
    """The transcript and its subagent directory, nothing else in the project dir.

    `.continuation_cache.json` and other sessions stay: they belong to the old
    directory, not to this thread.
    """
    source_dir = transcript.claude_session_dir(old)
    target_dir = transcript.claude_session_dir(new)
    moved = False
    for name in (f"{session_uuid}.jsonl", session_uuid):
        source = source_dir / name
        if source.exists():
            moved = _move(source, target_dir / name) or moved
    return moved


def _move_codex_session(old: Path, new: Path, session_uuid: str) -> bool:
    """Re-key the mapping, and carry the rollout when homes are per workspace."""
    thread_id = codex_state.read_thread_id(old, session_uuid)
    if thread_id is None:
        return False
    old_home = codex_state.home_dir(old)
    new_home = codex_state.home_dir(new)
    if old_home != new_home:
        _move_rollout(old_home, new_home, thread_id)
    codex_state.write_thread_id(new, session_uuid, thread_id)
    codex_state.clear_thread_id(old, session_uuid)
    return True


def _move_rollout(old_home: Path, new_home: Path, thread_id: str) -> None:
    pattern = f"sessions/**/{codex_state.rollout_glob(thread_id)}"
    found = sorted(old_home.glob(pattern))
    if not found:
        logger.warning(
            "migration: no rollout under %s for thread=%s", old_home, thread_id
        )
        return
    for rollout in found:
        _move(rollout, new_home / rollout.relative_to(old_home))


def _move_files(old: Path, thread_dir: Path) -> bool:
    """Everything the thread left in its directory, then the directory itself.

    The persona links are dropped rather than moved: the new workspace gets its
    own from `ensure_persona`, and a link carried along would point the thread
    directory at a persona no config names for it.
    """
    moved = False
    for entry in sorted(old.iterdir()):
        if entry.name in PERSONA_FILENAMES and entry.is_symlink():
            entry.unlink()
            continue
        moved = _move(entry, thread_dir / entry.name) or moved
    try:
        old.rmdir()
    except OSError as exc:
        logger.warning("migration: %s not removed: %s", old, exc)
    return moved


# ---------------------------------------------------------------------------
# The bulk pass: every old directory at once, for the threads that never get
# another message. Same moves as above, driven by a plan an operator reads first.
# ---------------------------------------------------------------------------

# `dm-<handle>-<ts>` or `<channel name>-<ts>`, where the ts is the thread_ts with
# its dot replaced (`1786342813-662689`), a bare integer second from older builds,
# or `root` for a message with no thread.
_LEGACY_SLACK = re.compile(r"\A(?P<label>.+)-(?P<key>\d{10}(?:-\d+)?|\d+-\d+|root)\Z")
# A chat id is numeric and only `/new` ever appended to it, so anything after the
# first dash is a token: today's `YYYYMMDD-HHMMSS`, and the counter and short hex
# forms earlier builds minted.
_LEGACY_TELEGRAM = re.compile(r"\A(?P<chat>\d+)-(?P<key>.+)\Z")
_UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


@dataclass
class ThreadPlan:
    """One old directory and where it goes. `new_name` is None when it cannot be
    placed, and `reason` says why."""

    old: Path
    thread_key: str
    new_name: str | None
    reason: str = ""
    session_uuids: list[str] = field(default_factory=list)


# (kind, label) -> new workspace name, or None when the label is unknown. `kind`
# is "dm" (label is a user handle) or "conversation" (label is a channel or
# group-DM name).
Resolver = Callable[[str, str], str | None]


def sessions_for(workspace: Path) -> list[str]:
    """Every session uuid recorded against this directory, on either backend."""
    found: list[str] = []
    for _path, uuid, _mtime in transcript._list_claude_session_files(workspace):
        if _UUID.match(uuid) and uuid not in found:
            found.append(uuid)
    for _path, uuid, _mtime in codex_state.mappings_for_workspace(workspace):
        if uuid not in found:
            found.append(uuid)
    return found


def plan_slack(data_dir: Path, resolve: Resolver) -> list[ThreadPlan]:
    """A plan for every directory under `workspaces/slack` in the old layout."""
    plans: list[ThreadPlan] = []
    for old in _old_directories(data_dir / "workspaces" / "slack"):
        match = _LEGACY_SLACK.match(old.name)
        if match is None:
            plans.append(ThreadPlan(old, "", None, "not a thread directory"))
            continue
        label, key = match["label"], match["key"]
        kind = "dm" if label.startswith("dm-") else "conversation"
        label = label.removeprefix("dm-")
        new_name = resolve(kind, label)
        if new_name is None:
            plans.append(ThreadPlan(old, key, None, f"{kind} {label!r} not on Slack"))
            continue
        plans.append(ThreadPlan(old, key, f"slack/{new_name}", "", sessions_for(old)))
    return plans


def plan_telegram(data_dir: Path) -> list[ThreadPlan]:
    """A plan for every `<chat id>-<token>` directory under `workspaces/telegram`."""
    plans: list[ThreadPlan] = []
    for old in _old_directories(data_dir / "workspaces" / "telegram"):
        match = _LEGACY_TELEGRAM.match(old.name)
        if match is None:
            plans.append(ThreadPlan(old, "", None, "not a /new session directory"))
            continue
        new_name = f"telegram/{match['chat']}"
        plans.append(ThreadPlan(old, match["key"], new_name, "", sessions_for(old)))
    return plans


def _old_directories(root: Path) -> list[Path]:
    """Top-level directories only. The new layout nests one level deeper
    (`dm/<id>`), and a top-level name with no thread suffix is left alone."""
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.iterdir() if path.is_dir() and not path.is_symlink()
    )


def apply_plans(plans: Iterable[ThreadPlan], data_dir: Path) -> int:
    """Run every placeable plan. Returns how many directories moved."""
    moved = 0
    for plan in plans:
        if plan.new_name is None:
            continue
        new = workspace_path(plan.new_name, data_dir)
        if migrate_thread(plan.old, new, plan.session_uuids, plan.thread_key):
            moved += 1
    return moved


def render_plans(plans: list[ThreadPlan]) -> str:
    """One line per directory, then the totals. What `--migrate-workspaces`
    prints, with or without `--apply`."""
    lines: list[str] = []
    placed = skipped = sessions = 0
    for plan in plans:
        if plan.new_name is None:
            skipped += 1
            lines.append(f"{plan.old.name}  SKIP: {plan.reason}")
            continue
        placed += 1
        sessions += len(plan.session_uuids)
        lines.append(
            f"{plan.old.name} -> {plan.new_name}/threads/{plan.thread_key}"
            f"  sessions={len(plan.session_uuids)}"
        )
    lines.append(
        f"{placed} directories to move ({sessions} sessions), {skipped} skipped"
    )
    return "\n".join(lines)


class SlackDirectory:
    """Handle and channel name lookups against one Slack workspace, fetched once.

    The old directory names carry display names and the new ones need ids. The
    transcripts on disk do not carry the ids (measured: 73 of 74 DM transcripts on
    one deployment had no sender marker), so Slack is the only source. Two list
    calls cover everything; nothing is guessed, an unknown name is skipped.
    """

    def __init__(self, client) -> None:
        self._users: dict[str, str] = {}
        self._conversations: dict[str, tuple[str, bool]] = {}
        for page in _paginate(client.users_list):
            for user in page.get("members", []):
                self._users[user["name"]] = user["id"]
        for page in _paginate(
            client.conversations_list,
            types="public_channel,private_channel,mpim",
            exclude_archived=False,
        ):
            for channel in page.get("channels", []):
                self._conversations[channel["name"]] = (
                    channel["id"],
                    bool(channel.get("is_mpim")),
                )

    def __call__(self, kind: str, label: str) -> str | None:
        if kind == "dm":
            user_id = self._users.get(label)
            return f"dm/{user_id}" if user_id else None
        found = self._conversations.get(label)
        if found is None:
            return None
        channel_id, is_mpim = found
        return f"{'mpim' if is_mpim else 'channel'}/{channel_id}"


def _paginate(method, **kwargs):
    cursor = ""
    while True:
        page = method(limit=1000, cursor=cursor or None, **kwargs)
        yield page
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return
