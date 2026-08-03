"""Process supervisor — spawn detached daemons, stop them, find them again.

Liveness model:
- Heartbeat file (~/.claude-on-the-fly/state/<frontend>.json) is the source of
  truth for pid and aliveness. The daemon writes it; anyone can read it.
- PID file (~/.claude-on-the-fly/state/<frontend>.pid) is a "we spawned this"
  marker that lets the TUI claim ownership across restarts. Optional — if a
  daemon was started outside the TUI (e.g. `uv run claude-telegram` in a shell),
  there's no PID file but the heartbeat still works.

Spawn semantics: detached via os.setsid (subprocess.Popen start_new_session=True),
stdout/stderr redirected to per-frontend log files so the child survives TUI
exit. We spawn via `sys.executable -m <module>` so the pid is the real Python
interpreter — not a `uv run` launcher — and so heartbeat writes start
immediately. After Popen, spawn blocks until the daemon writes its first
heartbeat (or a timeout fires), so callers get a real "did it start" signal.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from claude_on_the_fly import checks, envfile, logs
from claude_on_the_fly.agent import DATA_DIR
from claude_on_the_fly.checks import CheckResult
from claude_on_the_fly.heartbeat import STATE_DIR
from claude_on_the_fly.tui.state import process_exists as _process_exists

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILE = DATA_DIR / ".env"
LOG_DIR = DATA_DIR / "logs"
DEFAULT_GRACE_S = 5.0
DEFAULT_SPAWN_TIMEOUT_S = 20.0
KILL_POLL_INTERVAL_S = 0.1
HEARTBEAT_POLL_INTERVAL_S = 0.1
HEARTBEAT_FRESH_WINDOW_S = 30.0


def _last_running_file() -> Path:
    """Resolved lazily so tests can monkeypatch STATE_DIR."""
    return STATE_DIR / "last_running.json"


# Frontend name → module path runnable via `python -m`. Skips the `uv run`
# launcher (saves ~3s of startup delay) and gives us the real interpreter pid.
_FRONTEND_MODULE: dict[str, str] = {
    "telegram": "claude_on_the_fly.telegram",
    "slack": "claude_on_the_fly.slack",
    "cron": "claude_on_the_fly.cron",
    "jobs": "claude_on_the_fly.jobs.cli",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SupervisorError(Exception):
    """Base for all supervisor failures."""


@dataclass
class PreflightFailed(SupervisorError):
    """Preflight refused the spawn. results is the structured CheckResult list."""

    frontend: str
    results: list[CheckResult]

    def __str__(self) -> str:
        bad = [r for r in self.results if checks.is_blocking(r)]
        if not bad:
            return f"preflight failed for {self.frontend}"
        first = bad[0]
        return f"{self.frontend}: {first.name} — {first.detail}"


class AlreadyRunning(SupervisorError):
    """Refusing to spawn — heartbeat says one is already alive."""

    def __init__(self, frontend: str, pid: int) -> None:
        super().__init__(f"{frontend} already running (pid {pid})")
        self.frontend = frontend
        self.pid = pid


class NotRunning(SupervisorError):
    """stop() called but no live daemon found."""


class SpawnTimeout(SupervisorError):
    """Child did not write a heartbeat within the timeout. Child has been killed."""

    def __init__(
        self,
        frontend: str,
        pid: int,
        log_path: Path,
        *,
        log_tail: str = "",
        exited: bool = False,
        returncode: int | None = None,
    ) -> None:
        cause = (
            f"exited (rc={returncode}) before heartbeat"
            if exited
            else "did not heartbeat within timeout"
        )
        msg = f"{frontend} {cause} (pid {pid}); see {log_path}"
        if log_tail:
            msg += f"\n--- last lines ---\n{log_tail}"
        super().__init__(msg)
        self.frontend = frontend
        self.pid = pid
        self.log_path = log_path
        self.log_tail = log_tail
        self.exited = exited
        self.returncode = returncode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pid_file(frontend: str) -> Path:
    return STATE_DIR / f"{frontend}.pid"


def _heartbeat_file(frontend: str) -> Path:
    return STATE_DIR / f"{frontend}.json"


def _stdout_file(frontend: str) -> Path:
    """Where this spawn's inherited stdout/stderr goes.

    Same `<role>-<host>-<date>` contract as the log files so retention can see
    it; `logs.prune` never removes the newest capture per (role, host), since a
    live daemon still holds it open.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return logs.log_file(frontend, suffix=".stdout", directory=LOG_DIR)


def _resolve_pid(frontend: str) -> int | None:
    """Heartbeat first, PID file fallback. None if neither has a parseable pid."""
    hb = _heartbeat_file(frontend)
    if hb.is_file():
        try:
            data = json.loads(hb.read_text())
            pid = data.get("pid")
            if isinstance(pid, int):
                return pid
        except (OSError, json.JSONDecodeError):
            pass
    pid_file = _pid_file(frontend)
    if pid_file.is_file():
        try:
            return int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return None
    return None


def _write_pid(frontend: str, pid: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _pid_file(frontend)
    tmp = path.with_suffix(".pid.tmp")
    tmp.write_text(str(pid))
    tmp.replace(path)


def _remove_pid(frontend: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        _pid_file(frontend).unlink()


def _remove_heartbeat(frontend: str) -> None:
    """Delete the heartbeat file. A daemon unlinks its own on a clean exit, but
    a force-killed one never runs that cleanup — leaving a stale heartbeat whose
    pid reads as alive (os.kill(pid, 0) is true for an unreaped zombie), so the
    TUI shows it running, then broken as the timestamp ages. Removing it here
    makes a stopped daemon read as stopped immediately."""
    with contextlib.suppress(FileNotFoundError):
        _heartbeat_file(frontend).unlink()


def _load_env(env_file: Path | None) -> dict[str, str]:
    """Merge os.environ with the env file (if it exists). File wins on conflicts.

    Kept as the spawn path's name for the operation; the operation itself lives
    in `envfile` so `transcript` and `checks` can ask the same question without
    importing the TUI.
    """
    return envfile.merged(env_file)


def _heartbeat_fresh(frontend: str) -> bool:
    """True iff a fresh heartbeat file exists for this frontend."""
    path = _heartbeat_file(frontend)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
        last = data.get("last_heartbeat")
        if not isinstance(last, str):
            return False
        dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        age = (datetime.now(UTC) - dt).total_seconds()
        return age < HEARTBEAT_FRESH_WINDOW_S
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _agent_group_owned(frontend: str, pid: int) -> bool:
    """Whether the daemon advertises a private process group we may signal."""
    try:
        data = json.loads(_heartbeat_file(frontend).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("pid") == pid and data.get("process_group") == pid


def _signal_daemon(frontend: str, pid: int, sig: signal.Signals) -> None:
    """Signal the daemon and its children when the daemon owns a process group."""
    try:
        if _agent_group_owned(frontend, pid):
            os.killpg(pid, sig)
        else:
            # Older/external daemons do not prove that their group is private;
            # signaling a shared shell group could kill unrelated processes.
            os.kill(pid, sig)
    except OSError as exc:
        logger.warning("signal %s pid=%d failed: %s", sig.name, pid, exc)


def _sweep_agent_groups(frontend: str) -> None:
    """Reap detached agent groups recorded by a chat daemon."""
    path = DATA_DIR / "state" / f"{frontend}.pids"
    if not path.is_file():
        return
    try:
        from claude_on_the_fly.jobs.orphans import ProcessLedger

        ProcessLedger(path).sweep()
    except Exception:
        # Stopping the daemon must still complete if the optional recovery pass
        # cannot inspect a stale ledger.
        logger.exception("could not sweep detached agent groups for %s", frontend)


def _tail_file(path: Path, n_lines: int = 25) -> str:
    """Read the last n_lines of a file as a single string. Empty on read error."""
    from claude_on_the_fly.tui.render import tail_lines

    return "".join(tail_lines(path, n_lines))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_running(frontend: str) -> bool:
    pid = _resolve_pid(frontend)
    return pid is not None and _process_exists(pid)


def read_pid(frontend: str) -> int | None:
    """Return the resolved pid (heartbeat-first), or None if no daemon known."""
    return _resolve_pid(frontend)


def spawn(
    frontend: str,
    *,
    env_file: Path | None = DEFAULT_ENV_FILE,
    env: Mapping[str, str] | None = None,
    popen_factory=subprocess.Popen,
    wait_for_heartbeat: bool = True,
    spawn_timeout_s: float = DEFAULT_SPAWN_TIMEOUT_S,
) -> int:
    """Start a detached daemon for `frontend`. Returns its pid.

    Blocks until the daemon writes its first heartbeat (or `spawn_timeout_s`
    elapses, in which case the child is killed and SpawnTimeout is raised).
    Set wait_for_heartbeat=False to skip the wait — only safe for tests.

    Raises:
        PreflightFailed: env checks failed.
        AlreadyRunning: a live daemon is already known.
        SpawnTimeout: daemon did not heartbeat in time.
        ValueError: unknown frontend name.
    """
    if frontend not in _FRONTEND_MODULE:
        raise ValueError(f"unknown frontend: {frontend!r}")

    existing = _resolve_pid(frontend)
    if existing is not None and _process_exists(existing):
        raise AlreadyRunning(frontend, existing)

    resolved_env: dict[str, str] = dict(env) if env is not None else _load_env(env_file)
    results = checks.check_frontend(frontend, resolved_env)
    if not checks.all_ok(results):
        raise PreflightFailed(frontend=frontend, results=results)

    stdout = _stdout_file(frontend)
    log_handle = stdout.open("ab")
    try:
        proc = popen_factory(
            [sys.executable, "-m", _FRONTEND_MODULE[frontend]],
            start_new_session=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=resolved_env,
            close_fds=True,
            stdin=subprocess.DEVNULL,
        )
    finally:
        # The child inherits the fd via Popen; we can close ours.
        log_handle.close()

    _write_pid(frontend, proc.pid)
    logger.info("spawned %s pid=%d", frontend, proc.pid)

    if wait_for_heartbeat:
        deadline = time.monotonic() + spawn_timeout_s
        while time.monotonic() < deadline:
            if _heartbeat_fresh(frontend):
                return proc.pid
            rc = proc.poll()
            if rc is not None:
                # Child died before heartbeating — surface the actual error.
                _remove_pid(frontend)
                raise SpawnTimeout(
                    frontend=frontend,
                    pid=proc.pid,
                    log_path=stdout,
                    log_tail=_tail_file(stdout, n_lines=25),
                    exited=True,
                    returncode=rc,
                )
            time.sleep(HEARTBEAT_POLL_INTERVAL_S)

        # Timed out without a heartbeat — child still running but unresponsive.
        _signal_daemon(frontend, proc.pid, signal.SIGTERM)
        _remove_pid(frontend)
        raise SpawnTimeout(
            frontend=frontend,
            pid=proc.pid,
            log_path=stdout,
            log_tail=_tail_file(stdout, n_lines=25),
            exited=False,
        )

    return proc.pid


def stop(frontend: str, *, grace_s: float = DEFAULT_GRACE_S) -> int:
    """Signal a daemon to exit. SIGTERM, wait up to grace_s, then SIGKILL.

    Returns the pid that was signalled. Cleans up the PID file on success.

    Raises NotRunning if no daemon found.
    """
    pid = _resolve_pid(frontend)
    if pid is None or not _process_exists(pid):
        _remove_pid(frontend)
        _remove_heartbeat(frontend)
        raise NotRunning(f"{frontend} is not running")

    _signal_daemon(frontend, pid, signal.SIGTERM)

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            _remove_pid(frontend)
            _remove_heartbeat(frontend)
            logger.info("%s pid=%d exited cleanly", frontend, pid)
            return pid
        time.sleep(KILL_POLL_INTERVAL_S)

    # Grace expired — escalate to SIGKILL.
    _signal_daemon(frontend, pid, signal.SIGKILL)
    _sweep_agent_groups(frontend)

    # Best-effort second wait so callers don't race on file cleanup.
    for _ in range(20):
        if not _process_exists(pid):
            break
        time.sleep(KILL_POLL_INTERVAL_S)

    _remove_pid(frontend)
    _remove_heartbeat(frontend)
    logger.info("%s pid=%d force-killed", frontend, pid)
    return pid


def restart(
    frontend: str,
    *,
    env_file: Path | None = DEFAULT_ENV_FILE,
    env: Mapping[str, str] | None = None,
    grace_s: float = DEFAULT_GRACE_S,
    popen_factory=subprocess.Popen,
    wait_for_heartbeat: bool = True,
    spawn_timeout_s: float = DEFAULT_SPAWN_TIMEOUT_S,
) -> int:
    """Stop (if running) and respawn. Returns the new pid."""
    with contextlib.suppress(NotRunning):
        stop(frontend, grace_s=grace_s)
    # Remove stale heartbeat so spawn's wait_for_heartbeat checks a fresh write
    # rather than the one the old daemon wrote before exiting.
    with contextlib.suppress(FileNotFoundError):
        _heartbeat_file(frontend).unlink()
    return spawn(
        frontend,
        env_file=env_file,
        env=env,
        popen_factory=popen_factory,
        wait_for_heartbeat=wait_for_heartbeat,
        spawn_timeout_s=spawn_timeout_s,
    )


# ---------------------------------------------------------------------------
# Bulk operations — stop_all / resume
# ---------------------------------------------------------------------------


def _write_last_running(frontends: list[str]) -> None:
    """Persist the list of just-stopped daemons so `resume` can restore them."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = _last_running_file()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"frontends": frontends}))
    tmp.replace(target)


def read_last_running() -> list[str]:
    """Read the last-running list, or [] if absent/corrupt."""
    path = _last_running_file()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    fronts = data.get("frontends")
    if not isinstance(fronts, list):
        return []
    return [
        f for f in fronts if isinstance(f, str) and f in checks.SUPERVISABLE_FRONTENDS
    ]


def stop_all(*, grace_s: float = DEFAULT_GRACE_S) -> list[tuple[str, int]]:
    """Stop every currently-running daemon. Returns (frontend, pid) per stop.

    Sequential (one at a time) so a single misbehaving daemon does not block
    the others' grace windows. Records the list of stopped daemons to the
    last_running file so `resume` can restore them.
    """
    stopped: list[tuple[str, int]] = []
    for name in checks.SUPERVISABLE_FRONTENDS:
        if not is_running(name):
            continue
        try:
            pid = stop(name, grace_s=grace_s)
        except NotRunning:
            continue
        stopped.append((name, pid))
    if stopped:
        _write_last_running([name for name, _ in stopped])
    return stopped


def resume(
    *,
    env_file: Path | None = DEFAULT_ENV_FILE,
    env: Mapping[str, str] | None = None,
    popen_factory=subprocess.Popen,
    wait_for_heartbeat: bool = True,
    spawn_timeout_s: float = DEFAULT_SPAWN_TIMEOUT_S,
) -> list[tuple[str, int | None, Exception | None]]:
    """Spawn every frontend recorded by the most recent stop_all().

    Skips daemons that are already running. Returns (frontend, pid, error)
    triples — error is None on success, the raised exception otherwise.
    Per-frontend failures do not abort the rest.
    """
    results: list[tuple[str, int | None, Exception | None]] = []
    for name in read_last_running():
        if is_running(name):
            results.append((name, _resolve_pid(name), None))
            continue
        try:
            pid = spawn(
                name,
                env_file=env_file,
                env=env,
                popen_factory=popen_factory,
                wait_for_heartbeat=wait_for_heartbeat,
                spawn_timeout_s=spawn_timeout_s,
            )
            results.append((name, pid, None))
        except Exception as exc:
            results.append((name, None, exc))
    return results
