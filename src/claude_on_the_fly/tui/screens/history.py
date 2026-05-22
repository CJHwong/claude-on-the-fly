"""Symphony history screen — full-screen view of every recorded event.

The dashboard's tickets pane only shows what's currently running; this
screen surfaces the audit trail the EventLog has been accumulating: every
dispatch, cancel (terminal/inactive/stall), retry, worker crash. The
intended workflow is "find an inactive job from earlier today, copy its
takeover command, attach in another terminal."

Filter cycles via `s` (all → jira → github → all). The detail column
collapses event-type-specific fields into a one-line summary so the
table stays readable at typical terminal widths.
"""

from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from claude_on_the_fly.agent import get_backend
from claude_on_the_fly.symphony.agent_runner import session_uuid_for
from claude_on_the_fly.symphony.events import EventLog
from claude_on_the_fly.symphony.workspace import WORKSPACES_ROOT, sanitize_key

# Cap on rows we hydrate into the table. Older entries stay on disk in
# events.jsonl and can be queried with jq. 1000 rows ~= weeks of activity.
HISTORY_ROWS = 1000

SourceFilter = Literal["all", "jira", "github"]


def _format_detail(e: dict) -> str:
    """Type-specific one-line summary. Mirrors dashboard._format_event_detail
    but kept independent so the screens can drift without coupling."""
    t = e.get("type")
    if t == "cancelled":
        reason = e.get("reason") or ""
        st = e.get("state") or ""
        return f"{reason} ({st})" if st else reason
    if t == "worker_done":
        reason = e.get("reason") or ""
        st = e.get("state") or ""
        return f"{reason} ({st})" if st else reason
    if t == "worker_failed":
        return str(e.get("error", ""))[:100]
    if t == "retry_scheduled":
        kind = e.get("kind") or "?"
        attempt = e.get("attempt")
        return f"{kind} attempt={attempt}" if attempt else kind
    if t == "dispatched":
        st = e.get("state") or ""
        attempt = e.get("failure_attempt")
        if attempt:
            return f"{st} (retry {attempt})"
        return st
    return ""


class HistoryScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.pop_screen", "Back"),
        ("r", "refresh_now", "Refresh"),
        ("s", "cycle_source", "Filter source"),
        ("t", "copy_takeover", "Copy takeover"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._event_log = EventLog()
        self._filter: SourceFilter = "all"
        # Cache the identifier → source map of *visible* rows so the takeover
        # binding can resolve the highlighted row without re-tailing the log.
        self._row_sources: dict[str, str] = {}
        # Cursor key → identifier mapping (rows can repeat identifiers).
        self._mtime: float | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="history-header", markup=True)
        yield DataTable(id="history-table", cursor_type="row", zebra_stripes=True)
        yield Static(id="history-footer-hint", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        # Explicit widths so column headers don't collapse when the column's
        # widest cell is empty (e.g. an empty `detail` column).
        table.add_column("time", width=10)
        table.add_column("src", width=4)
        table.add_column("job", width=36)
        table.add_column("event", width=18)
        table.add_column("detail", width=40)
        self.query_one("#history-footer-hint", Static).update(
            "[dim]r refresh    s cycle source filter    t copy takeover cmd    "
            "Esc back[/dim]"
        )
        self._refresh()
        self.set_interval(2.0, self._refresh_if_changed)

    def action_refresh_now(self) -> None:
        self._mtime = None  # force reload
        self._refresh()

    def action_cycle_source(self) -> None:
        cycle: dict[SourceFilter, SourceFilter] = {
            "all": "jira",
            "jira": "github",
            "github": "all",
        }
        self._filter = cycle[self._filter]
        self._mtime = None  # force reload
        self._refresh()

    def action_copy_takeover(self) -> None:
        table = self.query_one("#history-table", DataTable)
        if table.row_count == 0:
            self._notify("no row selected", "warning")
            return
        try:
            row_key = table.coordinate_to_cell_key(
                Coordinate(table.cursor_coordinate.row, 0)
            ).row_key.value
        except Exception:
            self._notify("no row selected", "warning")
            return
        if not isinstance(row_key, str):
            self._notify("no row selected", "warning")
            return
        # Row keys are "<idx>:<identifier>" so the same identifier can appear
        # multiple times without collision.
        _, _, ident = row_key.partition(":")
        source = self._row_sources.get(ident)
        if not ident or not source:
            self._notify("no row selected", "warning")
            return

        workspace = WORKSPACES_ROOT / source / sanitize_key(ident)
        sid = session_uuid_for(ident, source=source)
        try:
            cmd = get_backend().takeover_command(workspace, sid)
        except Exception as exc:
            self._notify(f"takeover failed: {exc}", "error")
            return
        if cmd is None:
            self._notify(
                f"no session yet for {ident} — agent hasn't run a turn",
                "warning",
            )
            return
        try:
            self.app.copy_to_clipboard(f"cd {workspace} && {cmd}")
        except Exception as exc:
            self._notify(f"clipboard write failed: {exc}", "error")
            return
        self._notify(
            f"copied takeover cmd for {ident} (source={source})",
            "information",
        )

    def _refresh_if_changed(self) -> None:
        try:
            mtime = self._event_log.path.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._refresh()

    def _refresh(self) -> None:
        try:
            self._mtime = self._event_log.path.stat().st_mtime
        except FileNotFoundError:
            self._mtime = None
        except OSError:
            pass

        events = self._event_log.tail(HISTORY_ROWS)
        # newest first reads more naturally
        events = list(reversed(events))
        if self._filter != "all":
            events = [e for e in events if e.get("source") == self._filter]

        header = self.query_one("#history-header", Static)
        header.update(
            f"[bold]Symphony history[/bold] "
            f"[dim]({len(events)} rows, filter={self._filter})[/dim]"
        )

        table = self.query_one("#history-table", DataTable)
        table.clear()
        self._row_sources = {}

        if not events:
            table.add_row("—", "—", "no events", "—", "—", key="__empty__")
            return

        for idx, e in enumerate(events):
            ident = str(e.get("identifier", "?"))
            src = str(e.get("source") or "jira")
            self._row_sources.setdefault(ident, src)
            badge = src[:1].upper() if src else "?"
            ts = str(e.get("ts", ""))
            short_ts = ts[11:19] if len(ts) >= 19 else ts
            table.add_row(
                short_ts,
                badge,
                ident,
                str(e.get("type", "?")),
                _format_detail(e),
                key=f"{idx}:{ident}",
            )

    def _notify(self, msg: str, severity: str) -> None:
        try:
            self.app.notify(msg, severity=severity)  # type: ignore[arg-type]
        except Exception:
            pass
