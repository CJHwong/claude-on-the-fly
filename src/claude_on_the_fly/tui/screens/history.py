"""AI job history screen — full-screen view of every recorded event.

The dashboard's Active AI jobs pane only shows what's currently running;
this screen surfaces the audit trail the EventLog has been accumulating
across every frontend (symphony, telegram, slack, gmail): dispatch, done,
cancel, retry, worker crash. The intended workflow is "find an inactive
job from earlier today, copy its takeover command, attach in another
terminal."

Filter cycles via `s` over the frontend source (all → symphony → telegram
→ slack → gmail → all). The detail column collapses event-type-specific
fields into a one-line summary so the table stays readable at typical
terminal widths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from claude_on_the_fly.agent import DATA_DIR, get_backend
from claude_on_the_fly.events import EventLog
from claude_on_the_fly.symphony import watch
from claude_on_the_fly.symphony.agent_runner import session_uuid_for
from claude_on_the_fly.symphony.workspace import WORKSPACES_ROOT, sanitize_key
from claude_on_the_fly.tui import render

# Cap on rows we hydrate into the table. Older entries stay on disk in
# events.jsonl and can be queried with jq. 1000 rows ~= weeks of activity.
HISTORY_ROWS = 1000
# Cap on JSONL events we tail into the watch pane per refresh.
WATCH_EVENTS = 80

SourceFilter = Literal["all", "symphony", "telegram", "slack", "gmail"]
_SOURCE_CYCLE: tuple[SourceFilter, ...] = (
    "all",
    "symphony",
    "telegram",
    "slack",
    "gmail",
)


def _event_source(e: dict) -> str:
    """Return the frontend source for an event row.

    Pre-unification symphony rows wrote the tracker name (jira / github) into
    `source` directly. Treat any such legacy row as a symphony event so the
    new filter vocabulary still surfaces it.
    """
    src = str(e.get("source") or "")
    if src in ("jira", "github"):
        return "symphony"
    return src


def _format_detail(e: dict) -> str:
    """Type-specific one-line summary. Mirrors dashboard._format_event_detail
    but kept independent so the screens can drift without coupling."""
    t = e.get("type")
    source = _event_source(e)
    if t == "cancelled":
        reason = e.get("reason") or ""
        st = e.get("state") or ""
        return f"{reason} ({st})" if st else reason
    if t == "worker_done":
        # Symphony rows carry reason (terminal/inactive) + state; chat rows
        # carry cost + tokens. Render whichever is present.
        reason = e.get("reason") or ""
        st = e.get("state") or ""
        if reason:
            return f"{reason} ({st})" if st else reason
        cost = e.get("cost")
        if cost is not None:
            return f"cost=${cost:.4f}"
        return ""
    if t == "worker_failed":
        return str(e.get("error", ""))[:100]
    if t == "retry_scheduled":
        kind = e.get("kind") or "?"
        attempt = e.get("attempt")
        return f"{kind} attempt={attempt}" if attempt else kind
    if t == "dispatched":
        if source == "symphony":
            st = e.get("state") or ""
            attempt = e.get("failure_attempt")
            if attempt:
                return f"{st} (retry {attempt})"
            return st
        # Chat dispatches don't have a tracker state; identifier already
        # tells the user which conversation, so leave detail blank.
        return ""
    return ""


class HistoryScreen(Screen):
    # When the watch pane is open it takes the bottom half, so the screen
    # splits 50/50 vertically. Closed, the table fills the available space.
    DEFAULT_CSS = """
    #hist-watch-wrap {
        height: 50%;
    }
    #hist-table-wrap {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.pop_screen", "Back"),
        ("r", "refresh_now", "Refresh"),
        ("s", "cycle_source", "Filter source"),
        ("t", "copy_takeover", "Copy takeover"),
        ("w", "toggle_watch", "Watch session"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._event_log = EventLog()
        self._filter: SourceFilter = "all"
        # Cache the identifier → source map of *visible* rows so the takeover
        # binding can resolve the highlighted row without re-tailing the log.
        self._row_sources: dict[str, str] = {}
        # Cursor row key → resolved (workspace, session_uuid) for the watch
        # pane. Symphony rows derive deterministically; chat rows read from
        # the event row's session_uuid field.
        self._row_watch: dict[str, tuple[Path, str]] = {}
        self._mtime: float | None = None
        # Watch pane state (mirrors dashboard's pattern).
        self._watch_open: bool = False
        self._watch_target: str | None = None  # cursor row key being watched
        self._watch_path: Path | None = None
        self._watch_mtime: float | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="history-header", markup=True)
        with Vertical(id="hist-table-wrap"):
            yield DataTable(id="history-table", cursor_type="row", zebra_stripes=True)
        with Vertical(id="hist-watch-wrap"):
            yield Static(id="hist-watch-header", markup=True)
            yield RichLog(
                id="hist-watch-pane",
                wrap=False,
                highlight=False,
                markup=True,
                auto_scroll=True,
                max_lines=10_000,
            )
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
        # Watch pane stays hidden until user presses `w`.
        self.query_one("#hist-watch-wrap", Vertical).display = False
        self.query_one("#history-footer-hint", Static).update(
            "[dim]r refresh    s cycle source filter    t copy takeover    "
            "w toggle watch    Esc back[/dim]"
        )
        self._refresh()
        self.set_interval(2.0, self._refresh_if_changed)
        self.set_interval(1.0, self._refresh_watch_if_changed)

    def action_refresh_now(self) -> None:
        self._mtime = None  # force reload
        self._refresh()

    def action_cycle_source(self) -> None:
        idx = _SOURCE_CYCLE.index(self._filter)
        self._filter = _SOURCE_CYCLE[(idx + 1) % len(_SOURCE_CYCLE)]
        self._mtime = None  # force reload
        self._refresh()

    def action_toggle_watch(self) -> None:
        col = self.query_one("#hist-watch-wrap", Vertical)
        if self._watch_open:
            self._watch_open = False
            self._watch_target = None
            self._watch_path = None
            self._watch_mtime = None
            col.display = False
            return
        row_key = self._cursor_row_key()
        if row_key is None or row_key not in self._row_watch:
            self._notify("no watchable session for this row", "warning")
            return
        self._watch_open = True
        self._watch_target = row_key
        self._watch_path = None
        self._watch_mtime = None
        col.display = True
        self._refresh_watch_pane(force_reload=True)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # When the cursor moves while watch is open, retarget the watch pane
        # to the newly highlighted row's session.
        if not self._watch_open:
            return
        row_key = self._cursor_row_key()
        if row_key is None or row_key == self._watch_target:
            return
        if row_key not in self._row_watch:
            # Row has no resolvable session (e.g. legacy symphony dispatched
            # without uuid + no tracker). Leave pane on previous target.
            return
        self._watch_target = row_key
        self._watch_path = None
        self._watch_mtime = None
        self._refresh_watch_pane(force_reload=True)

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
        tracker = self._row_sources.get(ident)
        if not ident or not tracker:
            self._notify("no takeover for this row", "warning")
            return

        workspace = WORKSPACES_ROOT / tracker / sanitize_key(ident)
        sid = session_uuid_for(ident, source=tracker)
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
            f"copied takeover cmd for {ident} (tracker={tracker})",
            "information",
        )

    def _cursor_row_key(self) -> str | None:
        table = self.query_one("#history-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(
                Coordinate(table.cursor_coordinate.row, 0)
            ).row_key.value
        except Exception:
            return None
        return row_key if isinstance(row_key, str) else None

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

    def _refresh_watch_if_changed(self) -> None:
        if not self._watch_open:
            return
        self._refresh_watch_pane(force_reload=False)

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
            events = [e for e in events if _event_source(e) == self._filter]

        header = self.query_one("#history-header", Static)
        header.update(
            f"[bold]AI job history[/bold] "
            f"[dim]({len(events)} rows, filter={self._filter})[/dim]"
        )

        table = self.query_one("#history-table", DataTable)
        table.clear()
        # `_row_sources` is the takeover lookup: identifier -> tracker (jira /
        # github). Only symphony rows populate it because takeover-resume is
        # a symphony concept today. Chat rows show up in the table but their
        # `t` keybind no-ops with a notice.
        self._row_sources = {}
        # `_row_watch` resolves per-row (workspace, session_uuid) for the
        # watch pane. Symphony rows derive the uuid; chat rows read it from
        # the event payload (orchestrator emits session_uuid on dispatch).
        self._row_watch = {}

        if not events:
            table.add_row("—", "—", "no events", "—", "—", key="__empty__")
            return

        for idx, e in enumerate(events):
            ident = str(e.get("identifier", "?"))
            src = _event_source(e)
            row_key = f"{idx}:{ident}"
            if src == "symphony":
                tracker = str(e.get("tracker") or e.get("source") or "jira")
                self._row_sources.setdefault(ident, tracker)
                self._row_watch[row_key] = (
                    WORKSPACES_ROOT / tracker / sanitize_key(ident),
                    session_uuid_for(ident, source=tracker),
                )
            elif src in ("telegram", "slack", "gmail"):
                session_uuid = e.get("session_uuid")
                if session_uuid:
                    self._row_watch[row_key] = (
                        DATA_DIR / "workspaces" / ident,
                        str(session_uuid),
                    )
            badge = src[:1].upper() if src else "?"
            ts = str(e.get("ts", ""))
            short_ts = ts[11:19] if len(ts) >= 19 else ts
            table.add_row(
                short_ts,
                badge,
                ident,
                str(e.get("type", "?")),
                _format_detail(e),
                key=row_key,
            )

    def _refresh_watch_pane(self, *, force_reload: bool) -> None:
        """Tail the live backend session JSONL for the row at `_watch_target`.

        Empty row map (e.g. row was filtered out by a source cycle) hides
        nothing — the pane stays on the last good target so the user can
        keep reading. If the target row's session log doesn't exist yet,
        show a placeholder.
        """
        header = self.query_one("#hist-watch-header", Static)
        pane = self.query_one("#hist-watch-pane", RichLog)

        if self._watch_target is None:
            return
        resolved = self._row_watch.get(self._watch_target)
        if resolved is None:
            return
        workspace, session_uuid = resolved

        try:
            path = get_backend().session_log_path(workspace, session_uuid)
        except Exception:
            path = None

        if path is None:
            if force_reload or self._watch_path is not None:
                self._watch_path = None
                self._watch_mtime = None
                pane.clear()
                pane.write("[dim]no session log yet — agent hasn't run a turn[/dim]")
                header.update("[bold]watch[/bold] [dim](no session yet)[/dim]")
            return

        try:
            mtime = path.stat().st_mtime
        except OSError:
            return

        switched = path != self._watch_path
        if not switched and not force_reload and mtime == self._watch_mtime:
            return

        self._watch_path = path
        self._watch_mtime = mtime
        _, _, ident = self._watch_target.partition(":")
        header.update(f"[bold]watch: {ident}[/bold] [dim]{path.name}[/dim]")
        pane.clear()

        raw_lines = render.tail_lines(path, WATCH_EVENTS)
        any_rendered = False
        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            formatted = watch.format_event(event)
            if formatted is None:
                continue
            for line in formatted.split("\n"):
                pane.write(line)
            any_rendered = True
        if not any_rendered:
            pane.write(f"[dim](no displayable events yet in {path.name})[/dim]")

    def _notify(self, msg: str, severity: str) -> None:
        try:
            self.app.notify(msg, severity=severity)  # type: ignore[arg-type]
        except Exception:
            pass
