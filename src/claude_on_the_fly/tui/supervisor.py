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

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from claude_on_the_fly import checks
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

# Frontend name → module path runnable via `python -m`. Skips the `uv run`
# launcher (saves ~3s of startup delay) and gives us the real interpreter pid.
_FRONTEND_MODULE: dict[str, str] = {
    "telegram": "claude_on_the_fly.telegram",
    "slack": "claude_on_the_fly.slack",
    "gmail": "claude_on_the_fly.gmail",
    "schedule": "claude_on_the_fly.scheduler",
    "symphony": "claude_on_the_fly.symphony.cli",
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
        bad = [r for r in self.results if r.status != "ok"]
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
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{frontend}.stdout"


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
    try:
        _pid_file(frontend).unlink()
    except FileNotFoundError:
        pass


def _load_env(env_file: Path | None) -> dict[str, str]:
    """Merge os.environ with the env file (if it exists). File wins on conflicts."""
    merged: dict[str, str] = dict(os.environ)
    if env_file is not None and env_file.is_file():
        for k, v in dotenv_values(env_file).items():
            if v is not None:
                merged[k] = v
    return merged


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
        dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age < HEARTBEAT_FRESH_WINDOW_S
    except (OSError, json.JSONDecodeError, ValueError):
        return False


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
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
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
        raise NotRunning(f"{frontend} is not running")

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        logger.warning("SIGTERM to %s pid=%d failed: %s", frontend, pid, exc)

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            _remove_pid(frontend)
            logger.info("%s pid=%d exited cleanly", frontend, pid)
            return pid
        time.sleep(KILL_POLL_INTERVAL_S)

    # Grace expired — escalate to SIGKILL.
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        logger.warning("SIGKILL to %s pid=%d failed: %s", frontend, pid, exc)

    # Best-effort second wait so callers don't race on file cleanup.
    for _ in range(20):
        if not _process_exists(pid):
            break
        time.sleep(KILL_POLL_INTERVAL_S)

    _remove_pid(frontend)
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
    try:
        stop(frontend, grace_s=grace_s)
    except NotRunning:
        pass
    # Remove stale heartbeat so spawn's wait_for_heartbeat checks a fresh write
    # rather than the one the old daemon wrote before exiting.
    try:
        _heartbeat_file(frontend).unlink()
    except FileNotFoundError:
        pass
    return spawn(
        frontend,
        env_file=env_file,
        env=env,
        popen_factory=popen_factory,
        wait_for_heartbeat=wait_for_heartbeat,
        spawn_timeout_s=spawn_timeout_s,
    )
