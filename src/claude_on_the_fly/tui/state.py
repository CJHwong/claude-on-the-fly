"""Snapshot of system state — what every TUI screen reads from.

Pure function: given a state_dir (heartbeat JSONs) and an optional
schedule.yaml, produce a Snapshot describing every frontend's liveness and
the scheduler's upcoming fires.

Liveness contract (must match heartbeat.HeartbeatWriter):
    running:  heartbeat fresh AND process exists
    broken:   heartbeat stale (older than threshold) but process still alive
    stopped:  no heartbeat file, OR file present but pid does not exist
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Literal

from claude_on_the_fly.agent import DATA_DIR
from claude_on_the_fly.checks import SUPERVISABLE_FRONTENDS
from claude_on_the_fly.heartbeat import STATE_DIR
from claude_on_the_fly.scheduler import load_config as load_schedule_config
from claude_on_the_fly.scheduler import next_fire as scheduler_next_fire

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE_YAML = DATA_DIR / "schedule.yaml"

# Per-frontend staleness threshold (seconds). Symphony's poll cadence and Jira
# call latency can occasionally starve the heartbeat coroutine, so we give it
# more headroom before we call it broken.
STALENESS_S: dict[str, int] = {
    "default": 15,
    "symphony": 60,
}

FrontendState = Literal["running", "stopped", "broken"]


@dataclass(frozen=True)
class FrontendStatus:
    name: str
    state: FrontendState
    pid: int | None = None
    started_at: str | None = None
    last_heartbeat: str | None = None
    last_heartbeat_age_s: float | None = None
    extra: dict = field(default_factory=dict)
    error: str | None = None  # parse error, etc.
    version: str | None = None
    executable: str | None = None
    stale: bool = False  # running but code/env differs from TUI's own


@dataclass(frozen=True)
class JobInfo:
    name: str
    cron: str
    kind: str  # "prompt" or "script"
    next_fire: datetime


@dataclass(frozen=True)
class Snapshot:
    timestamp: datetime
    frontends: list[FrontendStatus]
    jobs: list[JobInfo]
    schedule_error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# Backward-compat alias (some module-internal callers).
_process_exists = process_exists


def _staleness_threshold_s(frontend: str) -> int:
    return STALENESS_S.get(frontend, STALENESS_S["default"])


def parse_iso_utc(ts: str) -> datetime:
    """Parse a 'YYYY-MM-DDTHH:MM:SSZ' timestamp into a UTC datetime."""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


_parse_iso_utc = parse_iso_utc


def tui_version() -> str:
    """Resolved package version of the currently running TUI."""
    try:
        return _pkg_version("claude-on-the-fly")
    except PackageNotFoundError:
        return "unknown"


def _is_stale(
    state_str: FrontendState,
    daemon_version: str | None,
    daemon_executable: str | None,
    self_version: str,
    self_executable: str,
) -> bool:
    """A daemon is stale when it's running but its recorded version or
    executable path differs from the TUI's own. Missing fields (older
    daemons without `executable` in the heartbeat) are treated as
    "can't tell" so we don't false-positive during a rollout."""
    if state_str != "running":
        return False
    if daemon_version is not None and daemon_version != self_version:
        return True
    if daemon_executable is not None and daemon_executable != self_executable:
        return True
    return False


def _frontend_status_from_heartbeat(
    name: str,
    state_dir: Path,
    now: datetime,
    process_check: Callable[[int], bool],
    self_version: str,
    self_executable: str,
) -> FrontendStatus:
    path = state_dir / f"{name}.json"
    if not path.is_file():
        return FrontendStatus(name=name, state="stopped")

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return FrontendStatus(
            name=name, state="stopped", error=f"heartbeat parse failed: {exc}"
        )

    pid = payload.get("pid")
    last_heartbeat = payload.get("last_heartbeat")
    started_at = payload.get("started_at")
    extra = payload.get("extra") or {}
    daemon_version = (
        payload.get("version") if isinstance(payload.get("version"), str) else None
    )
    daemon_executable = (
        payload.get("executable")
        if isinstance(payload.get("executable"), str)
        else None
    )

    if not isinstance(pid, int) or not isinstance(last_heartbeat, str):
        return FrontendStatus(
            name=name,
            state="stopped",
            error="heartbeat missing pid or last_heartbeat",
        )

    try:
        hb_dt = _parse_iso_utc(last_heartbeat)
    except ValueError as exc:
        return FrontendStatus(
            name=name, state="stopped", error=f"bad heartbeat timestamp: {exc}"
        )

    age_s = (now - hb_dt).total_seconds()
    alive_proc = process_check(pid)
    fresh = age_s < _staleness_threshold_s(name)

    if alive_proc and fresh:
        state: FrontendState = "running"
    elif alive_proc and not fresh:
        state = "broken"
    else:
        state = "stopped"

    return FrontendStatus(
        name=name,
        state=state,
        pid=pid,
        started_at=started_at,
        last_heartbeat=last_heartbeat,
        last_heartbeat_age_s=age_s,
        extra=extra if isinstance(extra, dict) else {},
        version=daemon_version,
        executable=daemon_executable,
        stale=_is_stale(
            state, daemon_version, daemon_executable, self_version, self_executable
        ),
    )


# Memoize parsed schedule specs by (path, mtime) so the 1Hz dashboard refresh
# doesn't reparse YAML on every tick when the file hasn't changed.
_schedule_cache: tuple[Path, float, object] | None = None


def _load_schedule_cached(path: Path):
    global _schedule_cache
    mtime = path.stat().st_mtime
    if _schedule_cache and _schedule_cache[0] == path and _schedule_cache[1] == mtime:
        return _schedule_cache[2]
    specs = load_schedule_config(path)
    _schedule_cache = (path, mtime, specs)
    return specs


def _jobs_from_schedule(
    schedule_yaml: Path, now: datetime
) -> tuple[list[JobInfo], str | None]:
    if not schedule_yaml.is_file():
        return [], None
    try:
        specs = _load_schedule_cached(schedule_yaml)
    except ValueError as exc:
        return [], str(exc)

    # croniter wants a naive local datetime (matches scheduler.py behavior).
    local_now = datetime.now()
    jobs = [
        JobInfo(
            name=s.name,
            cron=s.cron,
            kind=s.kind,
            next_fire=scheduler_next_fire(s.cron, local_now),
        )
        for s in specs
    ]
    jobs.sort(key=lambda j: j.next_fire)
    return jobs, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def snapshot(
    state_dir: Path | None = None,
    schedule_yaml: Path | None = None,
    *,
    now: datetime | None = None,
    process_check: Callable[[int], bool] = _process_exists,
    self_version: str | None = None,
    self_executable: str | None = None,
) -> Snapshot:
    """Snapshot the current state of every supervisable frontend + scheduler.

    self_version / self_executable default to the live TUI's values; tests
    can override either to simulate a fresh upgrade.
    """
    sd = state_dir if state_dir is not None else STATE_DIR
    sy = schedule_yaml if schedule_yaml is not None else DEFAULT_SCHEDULE_YAML
    ts = now if now is not None else datetime.now(timezone.utc)
    sv = self_version if self_version is not None else tui_version()
    se = self_executable if self_executable is not None else sys.executable

    frontends = [
        _frontend_status_from_heartbeat(name, sd, ts, process_check, sv, se)
        for name in SUPERVISABLE_FRONTENDS
    ]
    jobs, schedule_error = _jobs_from_schedule(sy, ts)
    return Snapshot(
        timestamp=ts,
        frontends=frontends,
        jobs=jobs,
        schedule_error=schedule_error,
    )
