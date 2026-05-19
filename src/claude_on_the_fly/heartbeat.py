"""Heartbeat writer — each daemon writes a small JSON file every few seconds
so the TUI can detect liveness without an IPC channel.

State file lives at ~/.claude-on-the-fly/state/<frontend>.json.

Liveness contract (consumed by tui.state.snapshot):
    alive = (now - last_heartbeat) < staleness_threshold[frontend]
            AND process_exists(pid)
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


def _package_version() -> str:
    try:
        return version("claude-on-the-fly")
    except PackageNotFoundError:
        return "unknown"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
