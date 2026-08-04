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
import stat
from contextlib import suppress
from pathlib import Path

from claude_on_the_fly.agent import DATA_DIR

MAPPINGS_DIR = DATA_DIR / "codex-sessions"
_MAX_THREAD_ID = 512


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
    """Best-effort removal of all daemon-owned mappings for ``workspace``."""
    canonical_workspace = workspace.resolve(strict=False)
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
