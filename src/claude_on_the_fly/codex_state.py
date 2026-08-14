"""Daemon-owned Codex session mappings.

The workspace is writable by the agent, so it is not a trustable place to store
the mapping from a COTF session to a Codex thread. This module keeps that mapping
under DATA_DIR, authenticates the workspace/session pair in the record, and uses
an atomic 0600 write.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from contextlib import suppress
from pathlib import Path

from claude_on_the_fly.agent import DATA_DIR

MAPPINGS_DIR = DATA_DIR / "codex-sessions"
HOMES_DIR = DATA_DIR / "codex-homes"
_MAX_THREAD_ID = 512

# Everything under a shared ~/.codex that the per-workspace home has to expose so
# codex starts and behaves the way the operator configured it. Linked rather than
# copied, so an operator's edit to the real file takes effect on the next turn and
# a jailed turn writing through the link lands on a path the profile already
# governs: the instruction and execution entries stay write-denied there, and
# auth.json stays writable so token refresh still works.
#
# Measured against codex-cli 0.147.0: a home holding only an auth.json link and an
# empty config.toml completed a real `codex exec` turn, so the rest of this list is
# about honouring the operator's configuration rather than about starting at all.
_SHARED_ENTRIES = (
    "config.toml",
    "AGENTS.md",
    "hooks.json",
    "rules",
    "plugins",
    "agents",
    "prompts",
    "auth.json",
)


def _home_key(workspace: Path) -> str:
    return hashlib.sha256(
        str(workspace.resolve(strict=False)).encode("utf-8")
    ).hexdigest()


def home_dir(workspace: Path) -> Path:
    """This workspace's own `CODEX_HOME`.

    codex names its rollouts by date and thread id in one flat tree, and it
    chooses the name at startup, so there is no per-workspace path to grant the
    way claude's `projects/<hash>` can be granted. Giving each workspace its own
    home is what makes the rollout location predictable before the run, and one
    workspace is one chat thread, so it is also the isolation boundary: a jailed
    turn is granted this directory and cannot see any other thread's rollouts.

    Daemon-owned under DATA_DIR, beside the mappings and outside the
    agent-writable workspace, for the reason this module exists.
    """
    return HOMES_DIR / _home_key(workspace)


def ensure_home(workspace: Path, shared: Path | None = None) -> Path:
    """Create this workspace's `CODEX_HOME` and link the shared entries into it.

    Idempotent: it runs before every codex spawn, and an operator who adds a
    prompts/ directory later gets it linked on the next turn without a restart.
    Absent shared entries are skipped rather than stubbed, because codex treats a
    missing config as "use the defaults" and a dangling link as an error.
    """
    from claude_on_the_fly import envfile

    home = home_dir(workspace)
    # sessions/ is created here rather than left to codex because the jail makes
    # $HOME opaque, and a recursive mkdir that cannot stat an ancestor walks up and
    # tries to create it instead: measured as `mkdir: /Users/hoss: Operation not
    # permitted` on a path whose leaf was granted. Creating the chain now leaves
    # codex writing files inside a directory that already exists.
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    # Resolved through envfile, not `Path.home() / ".codex"`. A deployment that sets
    # CODEX_HOME keeps its config and credential there, and the hardcoded default
    # pointed at a directory that may hold neither -- every link then skipped as an
    # absent target, leaving a home with no config and no credential at all. The
    # shared tree the jail denies resolves the same way, so the two cannot disagree.
    source_root = envfile.codex_home() if shared is None else shared
    for name in _SHARED_ENTRIES:
        link, target = home / name, source_root / name
        if not target.exists():
            continue
        if link.is_symlink():
            if link.readlink() == target:
                continue
            with suppress(OSError):
                link.unlink()
        elif link.exists():
            # A real file where the link belongs is removed, not left alone. This
            # home is writable by the jailed turn, so a real AGENTS.md or
            # config.toml here would be an execution-control file the agent can
            # write -- standing orders it leaves itself for the next run, which is
            # the thing the shared ~/.codex deny list exists to prevent. Replacing
            # it with the link puts the name back on a path the profile denies
            # writes to.
            with suppress(OSError):
                link.unlink()
        with suppress(OSError):
            link.symlink_to(target)
    return home


def _mapping_key(workspace: Path, session_uuid: str) -> str:
    identity = f"{workspace.resolve(strict=False)}\0{session_uuid}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def mapping_path(workspace: Path, session_uuid: str) -> Path:
    """Stable daemon-owned path for one canonical workspace/session pair."""
    return MAPPINGS_DIR / f"{_mapping_key(workspace, session_uuid)}.json"


def _read_record(path: Path) -> dict | None:
    """Read one mapping without following a replacement symlink."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            raw = os.read(fd, 64 * 1024)
        finally:
            os.close(fd)
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def read_thread_id(workspace: Path, session_uuid: str) -> str | None:
    """Read and validate the mapping for exactly this workspace/session."""
    path = mapping_path(workspace, session_uuid)
    record = _read_record(path)
    if record is None:
        return None
    if record.get("backend") != "codex":
        return None
    if record.get("session_uuid") != session_uuid:
        return None
    try:
        recorded_workspace = Path(str(record["workspace"])).resolve(strict=False)
    except (KeyError, OSError, ValueError):
        return None
    if recorded_workspace != workspace.resolve(strict=False):
        return None
    thread_id = record.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return None
    if len(thread_id) > _MAX_THREAD_ID or any(ord(c) < 32 for c in thread_id):
        return None
    return thread_id.strip()


def write_thread_id(workspace: Path, session_uuid: str, thread_id: object) -> Path:
    """Atomically persist a validated Codex thread mapping."""
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Codex returned an empty thread id")
    clean = thread_id.strip()
    if len(clean) > _MAX_THREAD_ID or any(ord(c) < 32 for c in clean):
        raise ValueError("Codex returned an invalid thread id")
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    MAPPINGS_DIR.chmod(stat.S_IRWXU)
    path = mapping_path(workspace, session_uuid)
    payload = json.dumps(
        {
            "backend": "codex",
            "workspace": str(workspace.resolve(strict=False)),
            "session_uuid": session_uuid,
            "thread_id": clean,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short write while storing Codex session mapping")
            offset += written
        os.fsync(fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp)
        raise
    finally:
        os.close(fd)
    os.replace(temp, path)
    os.chmod(path, 0o600)
    return path


def mappings_for_workspace(workspace: Path) -> list[tuple[Path, str, float]]:
    """Return validated mappings for a workspace, newest first."""
    found: list[tuple[Path, str, float]] = []
    canonical_workspace = workspace.resolve(strict=False)
    try:
        paths = list(MAPPINGS_DIR.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        record = _read_record(path)
        if record is None or record.get("backend") != "codex":
            continue
        session_uuid = record.get("session_uuid")
        if not isinstance(session_uuid, str):
            continue
        if path != mapping_path(workspace, session_uuid):
            continue
        try:
            recorded_workspace = Path(str(record["workspace"])).resolve(strict=False)
        except (KeyError, OSError, ValueError):
            continue
        if recorded_workspace != canonical_workspace:
            continue
        thread_id = read_thread_id(workspace, session_uuid)
        if thread_id is None:
            continue
        try:
            mtime = path.lstat().st_mtime
        except OSError:
            continue
        found.append((path, session_uuid, mtime))
    return sorted(found, key=lambda item: item[2], reverse=True)


def remove_workspace(workspace: Path) -> None:
    """Best-effort removal of all daemon-owned mappings for ``workspace``.

    The workspace's codex home goes too. It holds that thread's rollouts, and its
    name is derived from a path that will never exist again, so nothing else could
    ever reclaim it -- the same argument `transcript.remove_workspace_sessions`
    makes for claude's `projects/` directory.
    """
    canonical_workspace = workspace.resolve(strict=False)
    # Links first, so removing the tree cannot follow one into the shared ~/.codex.
    home = home_dir(workspace)
    for name in _SHARED_ENTRIES:
        link = home / name
        if link.is_symlink():
            with suppress(OSError):
                link.unlink()
    shutil.rmtree(home, ignore_errors=True)
    try:
        paths = list(MAPPINGS_DIR.glob("*.json"))
    except OSError:
        return
    for path in paths:
        record = _read_record(path)
        if record is None or record.get("backend") != "codex":
            continue
        session_uuid = record.get("session_uuid")
        if not isinstance(session_uuid, str) or path != mapping_path(
            workspace, session_uuid
        ):
            continue
        try:
            recorded_workspace = Path(str(record["workspace"])).resolve(strict=False)
        except (KeyError, OSError, ValueError):
            continue
        if recorded_workspace != canonical_workspace:
            continue
        with suppress(OSError):
            path.unlink()
