"""Rendering helpers for the snapshot — used by both the `status` subcommand
and the interactive dashboard. Also hosts shared filesystem helpers used by
multiple TUI screens (tail_lines)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from claude_on_the_fly.tui.state import FrontendStatus, JobInfo, Snapshot

_STATE_STYLES = {
    "running": "bold green",
    "stopped": "dim",
    "broken": "bold yellow",
}
_STATE_GLYPH = {"running": "●", "stopped": "○", "broken": "⚠"}


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


# Backward-compat alias for prior callers.
_fmt_age = fmt_age


def state_cell(state_str: str) -> Text:
    """One-cell renderable for a frontend state (running/stopped/broken)."""
    return Text(
        f"{_STATE_GLYPH.get(state_str, '?')} {state_str}",
        style=_STATE_STYLES.get(state_str, ""),
    )


def tail_lines(path: Path, n: int) -> list[str]:
    """Return the last n lines of a text file. Empty list on read error.

    Reads backwards from EOF in growing chunks instead of streaming the whole
    file — matters for large JSONLs (session logs reach 10MB+) tailed every
    second from the TUI.
    """
    if n <= 0:
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)  # SEEK_END
            size = f.tell()
            if size == 0:
                return []
            data = b""
            cursor = size
            chunk = 8192
            while cursor > 0 and data.count(b"\n") <= n:
                read = min(chunk, cursor)
                cursor -= read
                f.seek(cursor)
                data = f.read(read) + data
                chunk *= 2  # Exponential growth caps long-line worst cases.
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        # Drop trailing empty entry from the final newline if present.
        if lines and lines[-1] == "":
            lines.pop()
        return [ln + "\n" for ln in lines[-n:]]
    except OSError:
        return []


def _fmt_uptime(started_at: str | None, now: datetime) -> str:
    if not started_at:
        return "-"
    try:
        s = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return "-"
    return _fmt_age((now - s).total_seconds())


def _fmt_next_fire(when: datetime, now: datetime) -> str:
    # scheduler.next_fire returns a naive local datetime; the caller-supplied
    # `now` may be tz-aware UTC. Convert both to naive local for the delta.
    when_local = when.replace(tzinfo=None)
    now_local = datetime.now().replace(microsecond=0)
    delta = (when_local - now_local).total_seconds()
    when_str = when.strftime("%a %H:%M")
    if delta <= 0:
        return f"{when_str}  (now)"
    if delta < 60:
        return f"{when_str}  (in {delta:.0f}s)"
    if delta < 3600:
        return f"{when_str}  (in {delta / 60:.0f}m)"
    if delta < 86400:
        return f"{when_str}  (in {delta / 3600:.1f}h)"
    return f"{when_str}  (in {delta / 86400:.1f}d)"


def _format_extra_notes(extra: dict) -> str:
    """Flatten scalar heartbeat extras into `k=v, k=v` notes. Nested structures
    (lists/dicts like symphony's running_tickets) are rendered elsewhere."""
    scalars = {k: v for k, v in extra.items() if not isinstance(v, (list, dict))}
    return ", ".join(f"{k}={v}" for k, v in sorted(scalars.items()))


def frontends_table(frontends: list[FrontendStatus], now: datetime) -> Table:
    table = Table(title="Frontends", show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("state")
    table.add_column("pid", justify="right")
    table.add_column("uptime", justify="right")
    table.add_column("heartbeat", justify="right")
    table.add_column("notes", overflow="fold")

    for f in frontends:
        notes = f.error or ""
        if not notes and f.extra:
            notes = _format_extra_notes(f.extra)
        table.add_row(
            f.name,
            state_cell(f.state),
            str(f.pid) if f.pid else "-",
            _fmt_uptime(f.started_at, now),
            _fmt_age(f.last_heartbeat_age_s),
            notes,
        )
    return table


def jobs_table(jobs: list[JobInfo], now: datetime) -> Table:
    table = Table(title="Scheduled jobs", show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("cron")
    table.add_column("kind")
    table.add_column("next fire", overflow="fold")

    for j in jobs:
        table.add_row(j.name, j.cron, j.kind, _fmt_next_fire(j.next_fire, now))
    return table


def render_snapshot_rich(snap: Snapshot, console: Console | None = None) -> None:
    c = console or Console()
    c.print(frontends_table(snap.frontends, snap.timestamp))
    if snap.jobs:
        c.print(jobs_table(snap.jobs, snap.timestamp))
    elif snap.schedule_error:
        c.print(f"[red]Scheduler config error:[/red] {snap.schedule_error}")
    else:
        c.print("[dim]No schedule.yaml found.[/dim]")


def render_snapshot_json(snap: Snapshot) -> str:
    """Stable JSON shape for scripts. Datetimes serialized as ISO 8601."""

    def encode(o):
        if isinstance(o, datetime):
            return o.strftime("%Y-%m-%dT%H:%M:%SZ") if o.tzinfo else o.isoformat()
        raise TypeError(repr(o))

    payload = {
        "timestamp": snap.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frontends": [asdict(f) for f in snap.frontends],
        "jobs": [
            {**asdict(j), "next_fire": j.next_fire.isoformat()} for j in snap.jobs
        ],
        "schedule_error": snap.schedule_error,
    }
    return json.dumps(payload, indent=2, default=encode)
