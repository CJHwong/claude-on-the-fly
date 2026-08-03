"""Snapshot of system state — what every TUI screen reads from.

Pure function: given a state_dir (heartbeat JSONs) and an optional
schedule.yaml, produce a Snapshot describing every frontend's liveness, the
cron's upcoming fires, and the background-job queue's depth. Every read
here is read-only — the snapshot never creates or mutates what it describes.

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
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Literal

from claude_on_the_fly import envfile
from claude_on_the_fly.agent import DATA_DIR
from claude_on_the_fly.checks import SUPERVISABLE_FRONTENDS
from claude_on_the_fly.cron import load_config as load_cron_config
from claude_on_the_fly.cron import next_fire as cron_next_fire
from claude_on_the_fly.heartbeat import STATE_DIR
from claude_on_the_fly.heartbeat import process_exists as heartbeat_process_exists
from claude_on_the_fly.jobs.file_queue import (
    DEFAULT_ROW_LIMIT,
    QueueDepth,
    QueueRow,
    read_queue_depth,
    read_queue_rows,
)

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE_YAML = DATA_DIR / "cron.yaml"  # see cron.resolve_config_path
# The background-job worker's maildir. A module constant, like STATE_DIR, so a
# test can redirect it (snapshot() takes no jobs argument).
DEFAULT_JOBS_DIR = DATA_DIR / "jobs"

# Per-frontend staleness threshold (seconds). A poll cadence and a tracker
# call latency can occasionally starve the heartbeat coroutine, so we give it
# more headroom before we call it broken.
STALENESS_S: dict[str, int] = {
    "default": 15,
    "jobs": 30,
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
class JobsQueueView:
    """What the background-job queue looks like right now.

    Read straight off the maildir rather than out of the worker's heartbeat, so
    queue depth still shows when the worker is stopped — the same reason the
    cron tab reads cron.yaml instead of asking the daemon.
    """

    depth: QueueDepth
    rows: list[QueueRow]
    # Unfinished jobs the row cap left out, so the table can say so itself
    # instead of making the operator subtract 20 from the header's count.
    hidden: int = 0


@dataclass(frozen=True)
class Snapshot:
    timestamp: datetime
    frontends: list[FrontendStatus]
    jobs: list[JobInfo]
    schedule_error: str | None = None
    # None when the configured queue is not the file adapter, so there is no
    # maildir to observe. Distinct from an empty queue.
    jobs_queue: JobsQueueView | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Re-exported, not defined here: the daemons evaluate the same liveness
# contract to refuse starting a second copy of themselves, and they cannot
# import this package. Callers keep importing it from either module.
process_exists = heartbeat_process_exists

# Backward-compat alias (some module-internal callers).
_process_exists = process_exists


def _staleness_threshold_s(frontend: str) -> int:
    return STALENESS_S.get(frontend, STALENESS_S["default"])


def parse_iso_utc(ts: str) -> datetime:
    """Parse a 'YYYY-MM-DDTHH:MM:SSZ' timestamp into a UTC datetime."""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


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
    return daemon_executable is not None and daemon_executable != self_executable


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
    specs = load_cron_config(path)
    _schedule_cache = (path, mtime, specs)
    return specs


# Memoize the queue kind on the env file's own mtime, plus whatever the process
# environment says: dotenv_values() reparses the file on every call and this is
# read at 1Hz, but an env override must still win on the next tick.
_queue_kind_cache: tuple[Path, int, str | None, str] | None = None


def _queue_kind() -> str:
    """`JOBS_QUEUE_KIND`, read the way the daemons actually receive it.

    The value lives in `~/.claude-on-the-fly/.env` and no TUI module calls
    `load_dotenv()` — the daemons see it because `supervisor.spawn` merges that
    file into the child's environment. So reading `os.environ` alone would
    report `file` for every deployment that configures anything else, which is
    the very lie the caller's guard exists to prevent. `envfile` is that merge
    (file wins on conflicts), reused rather than reimplemented.
    """
    global _queue_kind_cache
    env_file = envfile.default_env_file()
    try:
        mtime_ns = env_file.stat().st_mtime_ns
    except OSError:
        mtime_ns = -1
    override = os.environ.get("JOBS_QUEUE_KIND")
    cached = _queue_kind_cache
    if cached is not None and cached[:3] == (env_file, mtime_ns, override):
        return cached[3]
    kind = envfile.merged(env_file).get("JOBS_QUEUE_KIND", "file").lower()
    _queue_kind_cache = (env_file, mtime_ns, override, kind)
    return kind


def _jobs_queue_view() -> JobsQueueView | None:
    """Observe the background-job maildir, or None when there is nothing to
    observe.

    The kind check mirrors `jobs/registry.make_queue`: only the `file` adapter
    keeps its state in a directory we can read. Any other kind (a broker) lives
    somewhere this reader cannot see, and reporting zeros for it would be a
    lie — so the caller gets None and renders "queue unavailable".
    """
    if _queue_kind() != "file":
        return None
    depth = read_queue_depth(DEFAULT_JOBS_DIR)
    rows = read_queue_rows(DEFAULT_JOBS_DIR, DEFAULT_ROW_LIMIT)
    # Only a full page can have been truncated. Subtracting from depth alone
    # would report the job that finished between the two reads as hidden.
    hidden = (
        max(0, depth.new + depth.running - len(rows))
        if len(rows) >= DEFAULT_ROW_LIMIT
        else 0
    )
    return JobsQueueView(depth=depth, rows=rows, hidden=hidden)


def _jobs_from_schedule(
    schedule_yaml: Path, now: datetime
) -> tuple[list[JobInfo], str | None]:
    if not schedule_yaml.is_file():
        return [], None
    try:
        specs = _load_schedule_cached(schedule_yaml)
    except ValueError as exc:
        return [], str(exc)

    # croniter wants a naive local datetime (matches cron.py behavior).
    local_now = datetime.now()
    jobs = [
        JobInfo(
            name=s.name,
            cron=s.cron,
            kind=s.kind,
            next_fire=cron_next_fire(s.cron, local_now),
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
    """Snapshot the current state of every supervisable frontend + cron.

    self_version / self_executable default to the live TUI's values; tests
    can override either to simulate a fresh upgrade.
    """
    sd = state_dir if state_dir is not None else STATE_DIR
    sy = schedule_yaml if schedule_yaml is not None else DEFAULT_SCHEDULE_YAML
    ts = now if now is not None else datetime.now(UTC)
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
        jobs_queue=_jobs_queue_view(),
    )
