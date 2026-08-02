"""AI job history screen — full-screen view of every recorded event.

The dashboard's Active AI jobs pane only shows what's currently running;
this screen surfaces the audit trail the EventLog has been accumulating
across every frontend (cron, slack, telegram): dispatch, done,
cancel, retry, worker crash. The intended workflow is "find an inactive
job from earlier today, copy its takeover command, attach in another
terminal."

Filter cycles via `s` over the frontend source (all → cron, then each chat
frontend in `checks.CHAT_FRONTENDS` order). The detail column collapses
event-type-specific fields into a one-line summary so the table stays readable
at typical terminal widths.
"""

from __future__ import annotations

import contextlib
import json
import shlex
from pathlib import Path
from typing import Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer, RichLog, Static

from claude_on_the_fly.agent import (
    DATA_DIR,
    get_backend,
    resolve_session_log,
)
from claude_on_the_fly.checks import CHAT_FRONTENDS
from claude_on_the_fly.events import EventLog
from claude_on_the_fly.tui import render, session_format
from claude_on_the_fly.tui.job_rows import (
    _aggregate_by_job,
    _compute_runtimes,
    _event_source,
    _format_detail,
    _format_local_time,
    _format_runtime,
    _parse_ts,
)
from claude_on_the_fly.tui.screens.overlay import OverlayScreen

# Re-exported above from tui.job_rows so existing imports of these helpers
# (tests, other screens) keep resolving through this module. Referenced here
# to document intent and silence "imported but unused" on the thin re-export.
_REEXPORTED = (
    _aggregate_by_job,
    _compute_runtimes,
    _event_source,
    _format_detail,
    _format_local_time,
    _format_runtime,
    _parse_ts,
)

# Cap on rows we hydrate into the table. Older entries stay on disk in
# events.jsonl and can be queried with jq. 1000 rows ~= weeks of activity.
HISTORY_ROWS = 1000
# Cap on JSONL events we tail into the watch pane per refresh.
WATCH_EVENTS = 80

SourceFilter = str
# "all" and cron first, then the chat frontends in their display order, so
# this cycle can never disagree with the dashboard about what exists or in what
# order (checks.CHAT_FRONTENDS is the one definition).
_SOURCE_CYCLE: tuple[SourceFilter, ...] = ("all", "cron", *CHAT_FRONTENDS)


class HistoryScreen(OverlayScreen):
    # When the watch pane is open it takes the bottom half, so the box
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
        ("a", "toggle_view", "Aggregated/Events"),
        ("o", "open_link", "Open PR/ticket"),
        ("t", "copy_takeover", "Copy takeover"),
        ("w", "toggle_watch", "Watch session"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._event_log = EventLog()
        self._filter: SourceFilter = "all"
        # Default to the aggregated "one row per job" view since the event
        # log gets noisy fast under retries; `a` toggles to the raw event
        # stream for debugging.
        self._view_mode: Literal["aggregated", "events"] = "aggregated"
        # Cache the identifier → source map of *visible* rows so the takeover
        # binding can resolve the highlighted row without re-tailing the log.
        # Lazy cache: jira source name -> base_url, for building browse URLs.
        # Cursor row key → resolved (workspace, session_uuid) for the watch
        # pane. Read from what each event recorded rather than derived, so
        # the event row's session_uuid field.
        self._row_watch: dict[str, tuple[Path, str]] = {}
        self._mtime: float | None = None
        # Watch pane state (mirrors dashboard's pattern).
        self._watch_open: bool = False
        self._watch_target: str | None = None  # cursor row key being watched
        self._watch_path: Path | None = None
        self._watch_mtime: float | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="overlay-box"):
            yield Static(id="history-header", markup=True)
            with Vertical(id="hist-table-wrap"):
                yield DataTable(
                    id="history-table", cursor_type="row", zebra_stripes=True
                )
            with Vertical(id="hist-watch-wrap"):
                yield Static(id="hist-watch-header", markup=True)
                yield RichLog(
                    id="hist-watch-pane",
                    wrap=False,
                    highlight=False,
                    markup=True,
                    auto_scroll=False,  # scroll driven via render.apply_scroll
                    max_lines=10_000,
                )
            yield Static(id="history-footer-hint", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self._apply_columns()
        # Watch pane stays hidden until user presses `w`.
        self.query_one("#hist-watch-wrap", Vertical).display = False
        self.query_one("#history-footer-hint", Static).update(
            "[dim]r refresh    s cycle source filter    a aggregated/events    "
            "o open PR/ticket    t copy takeover    w toggle watch    Esc back[/dim]"
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

    def action_toggle_view(self) -> None:
        self._view_mode = "events" if self._view_mode == "aggregated" else "aggregated"
        self._apply_columns()
        self._mtime = None  # force reload
        self._refresh()

    def _apply_columns(self) -> None:
        """Rebuild the table columns to match the current view mode.

        Textual's DataTable doesn't show/hide columns dynamically, so the
        toggle clears the whole table and re-adds the right set."""
        table = self.query_one("#history-table", DataTable)
        table.clear(columns=True)
        if self._view_mode == "aggregated":
            table.add_column("time", width=10)
            table.add_column("src", width=4)
            table.add_column("job", width=28)
            table.add_column("event", width=16)
            table.add_column("runs", width=5)
            table.add_column("runtime", width=8)
            table.add_column("detail", width=24)
        else:
            table.add_column("time", width=10)
            table.add_column("src", width=4)
            table.add_column("job", width=28)
            table.add_column("event", width=16)
            table.add_column("runtime", width=8)
            table.add_column("detail", width=28)

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
            # Row has no resolvable session (an event recorded without a
            # workspace or uuid). Leave the pane on its previous target.
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
        # Row keys are "evt:<idx>:<identifier>" or "agg:<src>:<identifier>".
        # The identifier is everything after the last colon — either form
        # parses the same way with rpartition.
        _, _, ident = row_key.rpartition(":")
        resolved = self._row_watch.get(row_key)
        if not ident or resolved is None:
            self._notify("no takeover for this row", "warning")
            return

        # Use the row-specific (workspace, session_uuid) pair so a takeover
        # copied from a row produced by, e.g., `claude:ollama:qwen2.5-coder`
        # resumes that exact session — not whatever the current env points at.
        workspace, sid = resolved
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
            # Backend values ultimately include data read from a session store.
            # Re-tokenize and re-quote both pieces before placing them in a
            # shell command copied to the clipboard.
            safe_cmd = shlex.join(shlex.split(cmd))
            takeover = f"cd -- {shlex.quote(str(workspace))} && {safe_cmd}"
            self.app.copy_to_clipboard(takeover)
        except Exception as exc:
            self._notify(f"clipboard write failed: {exc}", "error")
            return
        self._notify(
            f"copied takeover cmd for {ident}",
            "information",
        )

    def _row_url(self, identifier: str) -> str | None:
        """Reconstruct the browser URL for a row's PR/ticket.

        GitHub PRs are detected by identifier shape (`owner/repo#N`) alone, so
        this needs no configuration. A ticket key cannot be turned into a URL
        without knowing the instance it belongs to, and nothing in the package
        knows that any more — the cron entry that produced the row does.
        """
        if "/" in identifier and "#" in identifier:
            head, _, num = identifier.partition("#")
            if head.count("/") == 1 and num.isdigit():
                return f"https://github.com/{head}/pull/{num}"
        return None

    def action_open_link(self) -> None:
        """Open the highlighted row's PR / Jira ticket in the browser."""
        import webbrowser

        row_key = self._cursor_row_key()
        if not isinstance(row_key, str):
            self._notify("no row selected", "warning")
            return
        _, _, ident = row_key.rpartition(":")
        url = self._row_url(ident)
        if not url:
            self._notify(f"no link for {ident}", "warning")
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._notify(f"open failed: {exc}", "error")
            return
        self._notify(f"opening {url}", "information")

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
            f"[dim]({len(events)} events, view={self._view_mode}, "
            f"filter={self._filter})[/dim]"
        )

        table = self.query_one("#history-table", DataTable)
        table.clear()
        # `_row_watch` resolves per-row (workspace, session_uuid), read off
        # what the event itself recorded rather than re-derived per source.
        self._row_watch = {}

        if self._view_mode == "aggregated":
            self._render_aggregated(table, events)
        else:
            self._render_events(table, events)

    def _resolve_session(
        self, ident: str, src: str, e: dict
    ) -> tuple[Path, str] | None:
        """Map an event row to its (workspace, session_uuid). Picks the
        backend from the event so historical rows resolve to the right
        JSONL even after the dev switches CLAUDE_MODE / models."""
        session_uuid = e.get("session_uuid")
        if not session_uuid:
            return None
        # The dispatching side records the workspace it used, which is the only
        # thing that knows the layout for that source. Falling back to the chat
        # convention keeps rows written before that was recorded resolvable.
        recorded = e.get("workspace")
        workspace = Path(str(recorded)) if recorded else DATA_DIR / "workspaces" / ident
        return workspace, str(session_uuid)

    def _render_events(self, table: DataTable, events: list[dict]) -> None:
        """One row per raw event — the original audit-trail view."""
        if not events:
            table.add_row("—", "—", "no events", "—", "—", "—", key="__empty__")
            return
        oldest_first = list(reversed(events))
        runtime_by_oldest_idx = _compute_runtimes(oldest_first)
        total = len(events)
        for idx, e in enumerate(events):
            ident = str(e.get("identifier", "?"))
            src = _event_source(e)
            row_key = f"evt:{idx}:{ident}"
            resolved = self._resolve_session(ident, src, e)
            if resolved is not None:
                self._row_watch[row_key] = resolved
            badge = src[:1].upper() if src else "?"
            runtime = runtime_by_oldest_idx.get(total - 1 - idx)
            table.add_row(
                _format_local_time(e.get("ts")),
                badge,
                Text(ident),
                str(e.get("type", "?")),
                _format_runtime(runtime),
                _format_detail(e),
                key=row_key,
            )

    def _render_aggregated(self, table: DataTable, events: list[dict]) -> None:
        """One row per (identifier, source) — collapses retries into a
        single line showing the latest status + dispatch count."""
        rows = _aggregate_by_job(events)
        if not rows:
            table.add_row("—", "—", "no events", "—", "—", "—", "—", key="__empty__")
            return
        for row in rows:
            ident = row["identifier"]
            src = row["source"]
            e = row["last_event"]
            row_key = f"agg:{src}:{ident}"
            resolved = self._resolve_session(ident, src, e)
            if resolved is not None:
                self._row_watch[row_key] = resolved
            badge = src[:1].upper() if src else "?"
            table.add_row(
                _format_local_time(e.get("ts")),
                badge,
                Text(ident),
                str(e.get("type", "?")),
                str(row["runs"]),
                _format_runtime(row["runtime"]),
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

        # Resolve across backends: the row's job may have run under a different
        # backend than the TUI's env points at (e.g. codex vs claude).
        path = resolve_session_log(workspace, session_uuid)

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
        _, _, ident = self._watch_target.rpartition(":")
        header.update(
            Text.assemble(
                ("watch: ", "bold"),
                (ident, "bold"),
                (f" {path.name}", "dim"),
            )
        )
        was_bottom, prev_y = render.capture_scroll(pane)
        stick = switched or force_reload or was_bottom
        render.begin_scroll_aware_rewrite(pane, stick_to_bottom=stick)

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
            formatted = session_format.format_event(event)
            if formatted is None:
                continue
            for line in formatted.split("\n"):
                pane.write(line)
            any_rendered = True
        if not any_rendered:
            pane.write(f"[dim](no displayable events yet in {path.name})[/dim]")
        if not stick:
            render.restore_scroll(pane, prev_y=prev_y)

    def _notify(
        self, msg: str, severity: Literal["information", "warning", "error"]
    ) -> None:
        # Disable markup so remote/user-controlled notification text stays literal.
        with contextlib.suppress(Exception):
            self.app.notify(msg, severity=severity, markup=False)
