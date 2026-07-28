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
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from claude_on_the_fly.agent import DATA_DIR

logger = logging.getLogger(__name__)

STATE_DIR = DATA_DIR / "state"
DEFAULT_INTERVAL_S = 5.0
# How recent a heartbeat must be for its pid to count as alive. Generous
# relative to DEFAULT_INTERVAL_S so a daemon briefly starved of the event loop
# is not declared dead.
DEFAULT_LIVENESS_WINDOW_S = 30.0


def _package_version() -> str:
    try:
        return version("claude-on-the-fly")
    except PackageNotFoundError:
        return "unknown"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    age_s = (
        datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)
    ).total_seconds()
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
        self._path = self._state_dir / f"{frontend}.json"

    @property
    def path(self) -> Path:
        return self._path

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
                "started_at": self._started_at,
                "last_heartbeat": _utcnow_iso(),
                "version": self._version,
                "executable": self._executable,
                "extra": extra,
            }
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._path)
            logger.debug("heartbeat: wrote %s pid=%d", self._frontend, self._pid)
        except Exception as exc:
            logger.warning("heartbeat write failed for %s: %s", self._frontend, exc)

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
