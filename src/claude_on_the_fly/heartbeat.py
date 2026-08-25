"""Heartbeat writer — each daemon writes a small JSON file every few seconds
so the TUI can detect liveness without an IPC channel.

State file lives at ~/.claude-on-the-fly/state/<frontend>.json.

Liveness contract (consumed by tui.state.snapshot):
    alive = (now - last_heartbeat) < staleness_threshold[frontend]
            AND process_exists(pid)

`process_exists` and `live_pid` evaluate that contract here rather than in the
TUI, because a daemon needs it too — to refuse to start a second copy of itself
— and daemons must not import the `tui` package.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from claude_on_the_fly.agent import DATA_DIR

logger = logging.getLogger(__name__)

STATE_DIR = DATA_DIR / "state"
DEFAULT_INTERVAL_S = 5.0
# How recent a heartbeat must be for its pid to count as alive. Generous
# relative to DEFAULT_INTERVAL_S so a daemon briefly starved of the event loop
# is not declared dead.
DEFAULT_LIVENESS_WINDOW_S = 30.0


def resolved_runtime_dir(executable: str) -> str | None:
    """The real directory a Python entry point lives in, symlinks followed.

    The `bin` directory rather than the binary: a virtualenv's python is often
    a symlink to one shared system interpreter, which would make every release
    look identical. None when the path cannot be resolved, which reads as
    "cannot tell" to every caller rather than as a difference.
    """
    try:
        return str(Path(executable).parent.resolve())
    except (OSError, RuntimeError):
        return None


class InstanceAlreadyClaimed(RuntimeError):
    """A second daemon tried to own the same frontend state."""

    def __init__(self, frontend: str, holder: str = "unknown") -> None:
        super().__init__(
            f"{frontend} daemon is already running (instance lock held by {holder})"
        )
        self.frontend = frontend
        self.holder = holder


class InstanceLockUnavailable(RuntimeError):
    """The state directory cannot hold the lock that makes ownership exclusive."""

    def __init__(self, frontend: str, lock_path: Path, cause: OSError) -> None:
        super().__init__(
            f"cannot lock {lock_path} for the {frontend} daemon ({cause}); "
            "the state directory is on a filesystem without file locking. "
            "Point DATA_DIR at a local disk."
        )
        self.frontend = frontend
        self.lock_path = lock_path


def _package_version() -> str:
    try:
        return version("claude-on-the-fly")
    except PackageNotFoundError:
        return "unknown"


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def process_exists(pid: int) -> bool:
    """True if a process with this pid is currently running.

    Uses signal 0, which performs no-op delivery but raises if the process is
    gone (or we lack permission to signal it). For our purpose (single-user
    macOS dev tool), permission errors are vanishingly rare.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def live_pid(
    frontend: str,
    *,
    state_dir: Path | None = None,
    liveness_window_s: float = DEFAULT_LIVENESS_WINDOW_S,
) -> int | None:
    """The pid of a live daemon for `frontend`, or None if none is running.

    Both halves of the liveness contract are required. The freshness check
    alone would trust a heartbeat from a process that has since died; the pid
    check alone would trust a pid the OS has recycled onto something unrelated,
    which for a startup guard means refusing to ever start again.

    Any unreadable, unparseable, or incomplete state file reads as "nothing
    running": this answers "may I start?", and a corrupt file is not evidence
    that something else holds the resource.
    """
    path = (state_dir or STATE_DIR) / f"{frontend}.json"
    try:
        payload = json.loads(path.read_text())
        pid = payload["pid"]
        last = datetime.strptime(payload["last_heartbeat"], "%Y-%m-%dT%H:%M:%SZ")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(pid, int):
        return None
    age_s = (datetime.now(UTC) - last.replace(tzinfo=UTC)).total_seconds()
    if age_s > liveness_window_s:
        return None
    return pid if process_exists(pid) else None


class HeartbeatWriter:
    """Writes a JSON heartbeat to disk on a fixed cadence.

    Use as a long-lived asyncio task:

        writer = HeartbeatWriter("telegram")
        task = asyncio.create_task(writer.run())
        # ... on shutdown ...
        task.cancel()
    """

    def __init__(
        self,
        frontend: str,
        *,
        state_dir: Path | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        extra_provider: Callable[[], dict] | None = None,
        pid: int | None = None,
    ) -> None:
        self._frontend = frontend
        self._state_dir = state_dir or STATE_DIR
        self._interval_s = interval_s
        self._extra_provider = extra_provider
        self._pid = pid if pid is not None else os.getpid()
        self._started_at = _utcnow_iso()
        self._version = _package_version()
        self._executable = sys.executable
        # Resolved once, here, while this process is starting. `sys.executable`
        # under a managed launcher is the symlink path (`.../current/.venv/bin/
        # python`), so resolving it later answers "where does current point
        # NOW" rather than "which release is this process running". A reader
        # comparing two live resolutions of one symlink can never see a
        # difference, however many releases have come and gone.
        self._resolved_executable = resolved_runtime_dir(sys.executable)
        self._path = self._state_dir / f"{frontend}.json"
        self._lock_path = self._state_dir / f"{frontend}.instance.lock"
        self._lock_fd: int | None = None
        self._instance_id = uuid4().hex

    @property
    def path(self) -> Path:
        return self._path

    def claim(self) -> None:
        """Atomically acquire process-lifetime ownership of this frontend."""
        if self._lock_fd is not None:
            return
        self._state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = os.read(fd, 128).decode(errors="replace").strip()
            except OSError:
                holder = "unknown"
            os.close(fd)
            raise InstanceAlreadyClaimed(self._frontend, holder or "unknown") from None
        except OSError as exc:
            # A state dir on a filesystem without flock (some NFS and FUSE
            # mounts) raises ENOLCK/EOPNOTSUPP rather than BlockingIOError.
            # Catching only the latter leaked this fd and stopped every daemon
            # on an install that worked before the lock existed.
            #
            # Refusing rather than degrading is deliberate: the whole point of
            # the claim is that two daemons cannot both own one frontend, and a
            # mount that cannot answer the question cannot be assumed to say no.
            os.close(fd)
            raise InstanceLockUnavailable(self._frontend, self._lock_path, exc) from exc
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(self._pid).encode())
            os.fsync(fd)
        except Exception:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        self._lock_fd = fd

    def release(self) -> None:
        """Release a lock acquired by :meth:`claim`. Idempotent."""
        fd = self._lock_fd
        if fd is None:
            return
        self._lock_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def set_extra_provider(self, provider: Callable[[], dict] | None) -> None:
        """Attach data that was not available when the instance was claimed."""
        self._extra_provider = provider

    def write_once(self) -> None:
        """Synchronous single write — used at startup and from the run loop.

        Exceptions are caught and logged; a failed heartbeat write should
        never crash the host daemon.
        """
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            extra = self._extra_provider() if self._extra_provider else {}
            payload = {
                "frontend": self._frontend,
                "pid": self._pid,
                "process_group": os.getpgrp() if self._pid == os.getpid() else None,
                "started_at": self._started_at,
                "last_heartbeat": _utcnow_iso(),
                "version": self._version,
                "executable": self._executable,
                "resolved_executable": self._resolved_executable,
                "instance_id": self._instance_id,
                "extra": extra,
            }
            tmp = self._path.with_name(
                f"{self._path.name}.{self._pid}.{self._instance_id}.tmp"
            )
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._path)
            logger.debug("heartbeat: wrote %s pid=%d", self._frontend, self._pid)
        except Exception as exc:
            logger.warning("heartbeat write failed for %s: %s", self._frontend, exc)

    def remove_owned(self) -> None:
        """Remove the heartbeat only if it still belongs to this writer."""
        try:
            payload = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("instance_id") != self._instance_id:
            return
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    async def run(self) -> None:
        """Loop until cancelled, writing the heartbeat every interval_s seconds.

        The first write happens immediately so the TUI sees the daemon alive
        right after startup, not after the first sleep.
        """
        self.write_once()
        try:
            while True:
                await asyncio.sleep(self._interval_s)
                self.write_once()
        except asyncio.CancelledError:
            logger.debug("heartbeat cancelled for %s", self._frontend)
            raise
