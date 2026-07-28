"""Log file naming, handlers, and retention for ~/.claude-on-the-fly/logs/.

One file per (role, host, day): `logs/<role>-<host>-<date>.log`. Three things
here exist because a single `<platform>.log` written by every process on every
machine is the worst possible shape for this directory:

- **The filename carries role + host + day**, so no two writers share a path. A
  file syncer cannot merge concurrent appends to one file: it keeps one version
  and drops the other beside it as `NAME.sync-conflict-<date>-<device>.log`.
- **A day rolls over by opening the next file, never by renaming.** A
  `TimedRotatingFileHandler` renames `slack.log` -> `slack.log.2026-07-24`,
  which to a syncer is two whole multi-megabyte files changing at once. A
  rolled file here is never written again.
- **No console handler when stderr is not a terminal.** `supervisor.spawn`
  redirects a daemon's stdout/stderr into `<role>.stdout`, so a console handler
  duplicated the entire log into a second file — 18 MB of pure copy for the
  slack daemon, and a second file to conflict over. Real pre-logging crash
  output (an import-time traceback) still lands there.

`role` is which process wrote it: `slack` / `telegram` / `symphony` / `jobs` /
`schedule`, plus `schedule-<job>` for a scheduled job's own output.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import sys
from datetime import date, timedelta
from pathlib import Path

from claude_on_the_fly import agent

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# Matches the retention the TimedRotatingFileHandler used to give (backupCount=7).
DEFAULT_KEEP_DAYS = 7

# `<role>-<host>-<YYYY-MM-DD>`. Parsed from the right: the day always has two
# dashes and the host tag never has any (see `host_tag`), so a role containing
# dashes ("schedule-my-job") still resolves unambiguously.
_NAME_RE = re.compile(r"^(?P<role>.+)-(?P<host>[^-]+)-(?P<day>\d{4}-\d{2}-\d{2})$")
_HOST_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def host_tag() -> str:
    """Short identifier for this machine. `COTF_HOST_TAG` overrides.

    Dashes collapse to underscores so `<role>-<host>-<date>` stays parseable
    from the right even when the role itself contains dashes.
    """
    raw = os.environ.get("COTF_HOST_TAG") or socket.gethostname().split(".")[0]
    tag = _HOST_UNSAFE.sub("_", raw.replace("-", "_")).strip("_")
    return tag or "unknown"


def log_dir() -> Path:
    """`~/.claude-on-the-fly/logs`, read through the agent module every call.

    `agent.DATA_DIR` binds `Path.home()` at import time, so tests redirect it by
    patching that attribute; a module-level constant here would freeze it.
    """
    return agent.DATA_DIR / "logs"


def log_name(role: str, *, day: date | None = None, suffix: str = ".log") -> str:
    """Filename this process should write for `role` on `day` (default today)."""
    stamp = (day or date.today()).isoformat()
    return f"{role}-{host_tag()}-{stamp}{suffix}"


def log_file(
    role: str,
    *,
    day: date | None = None,
    suffix: str = ".log",
    directory: Path | None = None,
) -> Path:
    """Path this process should write for `role`.

    `directory` lets a caller that owns its own `LOG_DIR` constant pass it in,
    so that module stays the single place its own path is redirected.
    """
    return (directory or log_dir()) / log_name(role, day=day, suffix=suffix)


def parse_log_name(path: Path) -> tuple[str, str, str] | None:
    """`(role, host, day)` for a file following the scheme, else None.

    Anything that does not match is left alone by retention, so a legacy
    `slack.log` or an operator's own file is never deleted.
    """
    if path.suffix not in (".log", ".stdout"):
        return None
    match = _NAME_RE.match(path.stem)
    if match is None:
        return None
    return match.group("role"), match.group("host"), match.group("day")


def find_log(role: str, *, directory: Path | None = None) -> Path:
    """Newest log for `role`, for a reader that wants to tail it.

    Prefers this host's files, since that is what a live local daemon writes,
    and falls back to any host so a synced-in log from another machine is still
    readable. When nothing has been written yet this returns the path the
    daemon *would* write today, so a caller can name the file it is waiting on
    rather than handling None.
    """
    root = directory or log_dir()
    fallback = root / log_name(role)
    if not root.is_dir():
        return fallback
    mine: list[tuple[str, Path]] = []
    theirs: list[tuple[str, Path]] = []
    this_host = host_tag()
    for path in root.iterdir():
        parsed = parse_log_name(path)
        if parsed is None or parsed[0] != role or path.suffix != ".log":
            continue
        (mine if parsed[1] == this_host else theirs).append((parsed[2], path))
    for group in (mine, theirs):
        if group:
            return max(group)[1]
    return fallback


class DailyRoleFileHandler(logging.FileHandler):
    """Appends to `log_file(role)`, reopening when the date changes.

    Unlike the stdlib's timed handler this never renames: the date is in the
    filename, so a rollover is just the next `open()`. Delay-opens so a
    short-lived process that logs nothing leaves no file behind.
    """

    def __init__(self, role: str) -> None:
        self._role = role
        self._day = date.today()
        super().__init__(log_file(role, day=self._day), encoding="utf-8", delay=True)

    def emit(self, record: logging.LogRecord) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self.close()
            self.baseFilename = str(log_file(self._role, day=today))
        super().emit(record)


def configure(
    role: str,
    *,
    console: bool | None = None,
    level: str | None = None,
    prune_old: bool = True,
) -> None:
    """Wire the root logger to this (role, host, day) file, plus a console
    stream when stderr is a terminal.

    `console=None` auto-detects: a terminal gets the stream, a redirect-to-file
    does not, so a supervised daemon writes its log exactly once. Pass False
    explicitly when a TUI is about to take over the terminal.

    Existing handlers are replaced, so this is safe to call after an earlier
    `basicConfig` and produces the same result either way.
    """
    log_dir().mkdir(parents=True, exist_ok=True)
    resolved = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()

    root = logging.getLogger()
    root.setLevel(getattr(logging, resolved, logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if console is None:
        console = sys.stderr is not None and sys.stderr.isatty()
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(_FMT))
        root.addHandler(stream)

    daily = DailyRoleFileHandler(role)
    daily.setFormatter(logging.Formatter(_FMT))
    root.addHandler(daily)

    if prune_old:
        prune()


def keep_days() -> int:
    """Retention window in days (`COTF_LOG_KEEP_DAYS`). 0 disables pruning."""
    raw = os.environ.get("COTF_LOG_KEEP_DAYS")
    if not raw:
        return DEFAULT_KEEP_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_KEEP_DAYS


def prune(*, days: int | None = None, directory: Path | None = None) -> list[Path]:
    """Delete logs older than the retention window; return what went.

    Prunes **every** host's old files, not just this one's — the point is to
    bound the directory, and a delete replicates like any other change. Only
    files matching the naming scheme are touched. Errors are ignored: retention
    must never take a daemon down.

    One exception: the **newest `.stdout` per (role, host) is never pruned**,
    however old. A capture is opened once at spawn and backs the daemon's
    inherited stderr for its whole life, which can outlast the window; unlinking
    it reclaims nothing until the process exits and destroys the one thing the
    file is for (a fatal traceback).
    """
    window = keep_days() if days is None else days
    root = directory or log_dir()
    if window <= 0 or not root.is_dir():
        return []
    cutoff = (date.today() - timedelta(days=window)).isoformat()
    named = [
        (path, parsed)
        for path in root.iterdir()
        if path.is_file() and (parsed := parse_log_name(path)) is not None
    ]
    held = _held_captures(named)
    removed: list[Path] = []
    for path, (_role, _host, day) in named:
        if day >= cutoff or path in held:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def _held_captures(named: list[tuple[Path, tuple[str, str, str]]]) -> set[Path]:
    """The `.stdout` captures a still-running daemon may still own.

    A newer capture for the same (role, host) would mean a newer spawn, so the
    newest is the only one a live daemon can be holding. That needs no liveness
    check, which keeps retention off the heartbeat files.
    """
    newest: dict[tuple[str, str], tuple[str, Path]] = {}
    for path, (role, host, day) in named:
        if path.suffix != ".stdout":
            continue
        key = (role, host)
        if key not in newest or day > newest[key][0]:
            newest[key] = (day, path)
    return {path for _, path in newest.values()}
