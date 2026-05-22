"""Dashboard screen — selectable frontends table + scheduler jobs + log tail.

Refresh model:
- The DataTable is rebuilt every second from a fresh state.snapshot().
- Cursor position is preserved across rebuilds by frontend name.
- Action keys (s/k/r) read the currently highlighted row and call the
  supervisor synchronously; refresh runs again immediately after.
- The log pane at the bottom tails the highlighted frontend's logfile,
  re-reading on the same 1s tick and on row-highlight events.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Literal

from rich.text import Text

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from claude_on_the_fly.agent import DATA_DIR, get_backend
from claude_on_the_fly.checks import SUPERVISABLE_FRONTENDS
from claude_on_the_fly.symphony import watch
from claude_on_the_fly.symphony.agent_runner import session_uuid_for
from claude_on_the_fly.symphony.workspace import WORKSPACES_ROOT, sanitize_key
from claude_on_the_fly.tui import env_editor, render, state, supervisor
from claude_on_the_fly.tui.screens.env_diff import EnvDiffScreen

LOG_DIR = DATA_DIR / "logs"
TAIL_LINES = 200
# When showing a symphony ticket watch, tail this many raw JSONL events; each
# formats to 1–4 visible lines so the rendered pane stays manageable.
WATCH_EVENTS = 80
# Cap on RichLog growth so a 24/7 dashboard doesn't accumulate unbounded memory.
LOG_PANE_MAX_LINES = 10_000
SYMPHONY_HINT = (
    "[dim]Press h to open the history view (takeover copy lives there).[/dim]"
)


class DashboardScreen(Screen):
    # Section spacing — the three logical zones (services / jobs+symphony /
    # logs) read better with a row of breathing room between them than they
    # do crammed together.
    DEFAULT_CSS = """
    #jobs-row {
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("l", "app.push_screen('logs')", "Logs"),
        ("d", "app.push_screen('doctor')", "Doctor"),
        # `h` opens the symphony event history full-screen — separate from
        # the live tickets pane so the dashboard stays focused on now.
        ("h", "app.push_screen('history')", "History"),
        ("s", "start", "Start"),
        ("k", "stop", "Stop"),
        ("r", "restart", "Restart"),
        ("K", "stop_all", "Stop all"),
        ("u", "resume", "Resume"),
        ("e", "edit_env", "Edit .env"),
        ("R", "refresh_now", "Refresh"),
        # `c` copies the tail of the highlighted log to the clipboard via
        # OSC 52 so you can share errors quickly. For partial selections,
        # hold Option (macOS) or Shift while click-dragging to bypass
        # Textual's mouse capture and use the terminal's native selection.
        ("c", "copy_log", "Copy tail"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Daemon log pane state.
        self._log_path: Path | None = None
        self._log_mtime: float | None = None
        # Watch pane state, tracked separately so the two panes refresh
        # independently. _watch_target encodes what's being watched, e.g.
        # "symphony:jira:PROJ-1" or "schedule:cleanup-job", so we know to
        # force a reload when the user navigates to a different item.
        self._watch_path: Path | None = None
        self._watch_mtime: float | None = None
        self._watch_target: str | None = None
        # Heartbeat publishes `source` per running ticket; cache the
        # ticket → source mapping so the watch pane knows which per-source
        # workspace dir to read from when the user highlights a row.
        self._ticket_sources: dict[str, str] = {}
        self._busy_msg: str | None = None
        self._busy_ticks: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="dashboard-body"):
            yield Static(id="stale-banner", markup=True)
            yield DataTable(id="frontends", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="jobs-row"):
                with Vertical(id="jobs-pane"):
                    yield Static(id="jobs-header", markup=True)
                    yield DataTable(
                        id="jobs-content",
                        cursor_type="row",
                        zebra_stripes=True,
                    )
                with Vertical(id="symphony-pane"):
                    yield Static(id="symphony-header", markup=True)
                    yield DataTable(
                        id="symphony-tickets",
                        cursor_type="row",
                        zebra_stripes=True,
                    )
                    yield Static(id="symphony-tickets-hint", markup=True)
            with Horizontal(id="log-row"):
                with Vertical(id="log-daemon-col"):
                    yield Static(id="log-header", markup=True)
                    yield RichLog(
                        id="log-pane",
                        wrap=False,
                        highlight=False,
                        auto_scroll=True,
                        max_lines=LOG_PANE_MAX_LINES,
                    )
                with Vertical(id="log-watch-col"):
                    yield Static(id="watch-header", markup=True)
                    yield RichLog(
                        id="watch-pane",
                        wrap=False,
                        highlight=False,
                        markup=True,
                        auto_scroll=True,
                        max_lines=LOG_PANE_MAX_LINES,
                    )
            yield Static(id="status-line", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#frontends", DataTable)
        table.add_column("name", width=10)
        table.add_column("state", width=10)
        table.add_column("pid", width=8)
        table.add_column("uptime", width=8)
        table.add_column("heartbeat", width=10)
        table.add_column("notes", width=40)
        jobs = self.query_one("#jobs-content", DataTable)
        jobs.add_column("name", width=12)
        jobs.add_column("cron", width=14)
        jobs.add_column("kind", width=10)
        jobs.add_column("next fire", width=24)
        tickets = self.query_one("#symphony-tickets", DataTable)
        # Explicit widths so column headers stay readable when rows are sparse.
        tickets.add_column("job", width=24)
        tickets.add_column("state", width=14)
        tickets.add_column("uptime", width=8)
        tickets.add_column("last turn", width=10)
        tickets.add_column("retries", width=8)
        self.query_one("#jobs-header", Static).update("[bold]Scheduled jobs[/bold]")
        self.query_one("#symphony-header", Static).update("[bold]Symphony jobs[/bold]")
        self.query_one("#symphony-tickets-hint", Static).update(SYMPHONY_HINT)
        self._refresh()
        self.set_interval(1.0, self._refresh)
        self.set_interval(1.0, self._refresh_log)
        self.set_interval(0.1, self._tick_busy)

    def _tick_busy(self) -> None:
        if self._busy_msg is None:
            return
        self._busy_ticks += 1
        self._render_busy_line()

    def action_refresh_now(self) -> None:
        self._refresh()
        self._refresh_log(force_reload=True)

    def _current_log_path(self) -> Path | None:
        """Whichever log pane is more specific takes precedence: the watch
        pane (per-ticket session log) when something's highlighted there,
        otherwise the daemon log for the highlighted frontend."""
        return self._watch_path if self._watch_path else self._log_path

    def action_copy_log(self) -> None:
        """Copy the tail of the currently-relevant log to the clipboard.

        For "share the whole error context with someone". For partial
        selections, hold Option (macOS) or Shift while click-dragging to
        bypass Textual's mouse capture and use the terminal's native
        selection + ⌘C.
        """
        path = self._current_log_path()
        if path is None:
            self._notify("no log selected to copy", "warning")
            return
        try:
            content = path.read_text(errors="replace")
        except Exception as exc:
            self._notify(f"copy failed: {exc}", "error")
            return
        lines = content.splitlines()
        tail_n = 500
        tail = "\n".join(lines[-tail_n:])
        try:
            # Textual's clipboard uses OSC 52 — works in iTerm2, kitty,
            # WezTerm, Alacritty out of the box.
            self.app.copy_to_clipboard(tail)
        except Exception as exc:
            self._notify(f"clipboard write failed: {exc}", "error")
            return
        shown = min(len(lines), tail_n)
        self._notify(
            f"copied last {shown} line(s) of {path.name} to clipboard",
            "information",
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Reload the log pane immediately when the cursor moves.
        self._refresh_log(force_reload=True)

    def _selected_frontend(self) -> str | None:
        table = self.query_one("#frontends", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return str(row_key.value) if row_key.value is not None else None

    def _selected_ticket(self) -> str | None:
        """Highlighted ticket identifier in the symphony tickets DataTable, or None."""
        return self._datatable_cursor_key("#symphony-tickets")

    def _selected_job(self) -> str | None:
        """Highlighted job name in the scheduled jobs DataTable, or None."""
        return self._datatable_cursor_key("#jobs-content")

    def _datatable_cursor_key(self, selector: str) -> str | None:
        try:
            table = self.query_one(selector, DataTable)
        except Exception:
            return None
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return str(row_key.value) if row_key.value is not None else None

    async def _run_supervisor_action(
        self,
        verb: str,  # "start" | "stop" | "restart"
        op: Callable[[str], int],
    ) -> None:
        name = self._selected_frontend()
        if name is None or self._busy_msg:
            return
        present = {"start": "starting", "stop": "stopping", "restart": "restarting"}[
            verb
        ]
        past = {"start": "started", "stop": "stopped", "restart": "restarted"}[verb]
        self._set_busy(f"{present} {name}")
        try:
            try:
                pid = await asyncio.to_thread(op, name)
            except supervisor.AlreadyRunning as exc:
                self._notify(f"{name}: already running (pid {exc.pid})", "warning")
                return
            except supervisor.NotRunning:
                self._notify(f"{name}: not running", "warning")
                return
            except supervisor.PreflightFailed as exc:
                bad = [r for r in exc.results if r.status != "ok"]
                detail = bad[0].name if bad else "checks failed"
                self._notify(
                    f"{name}: preflight failed ({detail}) — opening doctor", "error"
                )
                self.app.push_screen("doctor")
                return
            except supervisor.SpawnTimeout as exc:
                self._notify(f"{name}: spawn timed out — see {exc.log_path}", "error")
                return
            except Exception as exc:
                self._notify(f"{name}: {verb} failed: {exc}", "error")
                return
            self._notify(f"{past} {name} (pid {pid})", "information")
        finally:
            self._clear_busy()
            self._refresh()

    async def action_start(self) -> None:
        await self._run_supervisor_action("start", supervisor.spawn)

    async def action_stop(self) -> None:
        await self._run_supervisor_action("stop", supervisor.stop)

    async def action_restart(self) -> None:
        await self._run_supervisor_action("restart", supervisor.restart)

    async def action_stop_all(self) -> None:
        if self._busy_msg:
            return
        self._set_busy("stopping all daemons")
        try:
            stopped = await asyncio.to_thread(supervisor.stop_all)
            if not stopped:
                self._notify("nothing running", "warning")
                return
            names = ", ".join(name for name, _ in stopped)
            self._notify(
                f"stopped {len(stopped)}: {names} (press u to resume)",
                "information",
            )
        finally:
            self._clear_busy()
            self._refresh()

    async def action_resume(self) -> None:
        if self._busy_msg:
            return
        recorded = supervisor.read_last_running()
        if not recorded:
            self._notify("nothing to resume (no last-running record)", "warning")
            return
        self._set_busy(f"resuming {len(recorded)} daemon(s)")
        try:
            results = await asyncio.to_thread(supervisor.resume)
            successes = [name for name, _, exc in results if exc is None]
            failures = [(name, exc) for name, _, exc in results if exc is not None]
            if successes:
                self._notify(
                    f"started {len(successes)}: {', '.join(successes)}",
                    "information",
                )
            for name, exc in failures:
                self._notify(f"{name}: {exc}", "error")
        finally:
            self._clear_busy()
            self._refresh()

    def _set_busy(self, msg: str) -> None:
        self._busy_msg = msg
        self._busy_ticks = 0
        self._render_busy_line()

    def _clear_busy(self) -> None:
        self._busy_msg = None

    def _render_busy_line(self) -> None:
        if self._busy_msg is None:
            return
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self._busy_ticks % 10]
        line = self.query_one("#status-line", Static)
        line.update(f"[yellow]{spinner} {self._busy_msg}…[/yellow]")

    def action_edit_env(self) -> None:
        env_file = supervisor.DEFAULT_ENV_FILE
        with self.app.suspend():
            diff = env_editor.edit_and_diff(env_file)
        if diff.is_empty():
            self._notify("no env changes", "information")
            return
        self.app.push_screen(EnvDiffScreen(diff))
        self._refresh()

    def _refresh_log(self, *, force_reload: bool = False) -> None:
        """Refresh both panes: daemon log on the left, ticket watch on the right.

        The right column is hidden unless symphony is the selected frontend AND
        a ticket is highlighted in the tickets table — then the daemon log
        column gives up its space to the watch.
        """
        self._refresh_daemon_log(force_reload=force_reload)
        self._refresh_watch_pane(force_reload=force_reload)

    def _refresh_daemon_log(self, *, force_reload: bool) -> None:
        name = self._selected_frontend()
        header = self.query_one("#log-header", Static)
        pane = self.query_one("#log-pane", RichLog)

        if name is None:
            header.update("[dim]log: (no selection)[/dim]")
            return

        path = LOG_DIR / f"{name}.log"
        if not path.is_file():
            if path != self._log_path or force_reload:
                self._log_path = path
                self._log_mtime = None
                pane.clear()
                pane.write(f"[dim]no log yet at {path}[/dim]")
                header.update(f"[dim]log: {path.name} (missing)[/dim]")
            return

        try:
            mtime = path.stat().st_mtime
        except OSError:
            return

        switched = path != self._log_path
        if not switched and not force_reload and mtime == self._log_mtime:
            return

        self._log_path = path
        self._log_mtime = mtime
        header.update(f"[bold]log: {path.name}[/bold]")
        lines = render.tail_lines(path, TAIL_LINES)
        pane.clear()
        if not lines:
            pane.write(f"[dim]({path.name} is empty or unreadable)[/dim]")
            return
        for line in lines:
            pane.write(line.rstrip("\n"))

    def _refresh_watch_pane(self, *, force_reload: bool) -> None:
        """Show the watch column for two cases:
          - symphony selected + ticket highlighted → JSONL with format_event
          - schedule selected + job highlighted    → per-job schedule-<name>.log
        Otherwise the column is hidden so the daemon log gets full width.
        """
        col = self.query_one("#log-watch-col", Vertical)
        name = self._selected_frontend()

        target: str | None = None  # "symphony:<source>:<KEY>" or "schedule:NAME"
        if name == "symphony":
            ticket = self._selected_ticket()
            if ticket is not None:
                source = self._ticket_sources.get(ticket, "jira")
                target = f"symphony:{source}:{ticket}"
        elif name == "schedule":
            job = self._selected_job()
            if job is not None:
                target = f"schedule:{job}"

        if target is None:
            if col.display:
                col.display = False
                self._watch_path = None
                self._watch_mtime = None
                self._watch_target = None
            return

        if not col.display:
            col.display = True

        if target != self._watch_target:
            self._watch_target = target
            self._watch_path = None
            self._watch_mtime = None
            force_reload = True

        header = self.query_one("#watch-header", Static)
        pane = self.query_one("#watch-pane", RichLog)

        mode, _, rest = target.partition(":")
        if mode == "symphony":
            # rest = "<source>:<KEY>"
            source, _, target_id = rest.partition(":")
            self._refresh_watch_symphony(target_id, source, header, pane, force_reload)
        else:
            self._refresh_watch_scheduler(rest, header, pane, force_reload)

    def _refresh_watch_symphony(
        self,
        ticket: str,
        source: str,
        header: Static,
        pane: RichLog,
        force_reload: bool,
    ) -> None:
        workspace = WORKSPACES_ROOT / source / sanitize_key(ticket)
        session_uuid = session_uuid_for(ticket, source=source)
        try:
            path = get_backend().session_log_path(workspace, session_uuid)
        except Exception:
            path = None

        if path is None:
            if force_reload or self._watch_path is not None:
                self._watch_path = None
                self._watch_mtime = None
                pane.clear()
                pane.write(
                    f"[dim]no session log yet for {ticket} — "
                    f"agent hasn't run a turn[/dim]"
                )
                header.update(
                    f"[bold]watch: {ticket}[/bold] [dim](no session yet)[/dim]"
                )
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
        header.update(f"[bold]watch: {ticket}[/bold] [dim]{path.name}[/dim]")
        pane.clear()
        import json

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

    def _refresh_watch_scheduler(
        self, job: str, header: Static, pane: RichLog, force_reload: bool
    ) -> None:
        """Tail the per-job log at logs/schedule-<name>.log as plain text.

        Markup is bypassed via rich.text.Text so literal log brackets like
        [INFO] survive intact even though the pane has markup=True (which the
        symphony branch relies on for colors).
        """
        path = LOG_DIR / f"schedule-{job}.log"
        if not path.is_file():
            if force_reload or self._watch_path is not None:
                self._watch_path = None
                self._watch_mtime = None
                pane.clear()
                pane.write(
                    f"[dim]no log yet at {path} — job hasn't fired since startup[/dim]"
                )
                header.update(f"[bold]job: {job}[/bold] [dim](no log)[/dim]")
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
        header.update(f"[bold]job: {job}[/bold] [dim]{path.name}[/dim]")
        pane.clear()
        lines = render.tail_lines(path, TAIL_LINES)
        if not lines:
            pane.write(f"[dim]({path.name} is empty)[/dim]")
            return
        for line in lines:
            pane.write(Text(line.rstrip("\n")))

    def _notify(
        self, msg: str, severity: Literal["information", "warning", "error"]
    ) -> None:
        # Textual notify; also surface on the dashboard's bottom line.
        try:
            self.app.notify(msg, severity=severity)
        except Exception:
            pass
        line = self.query_one("#status-line", Static)
        line.update(f"[dim]{msg}[/dim]")

    def _refresh(self) -> None:
        snap = state.snapshot()

        table = self.query_one("#frontends", DataTable)
        # Preserve cursor position by remembered frontend name.
        previously_selected = self._selected_frontend() or SUPERVISABLE_FRONTENDS[0]

        table.clear()
        for f in snap.frontends:
            notes = f.error or ""
            if not notes and f.extra:
                notes = render._format_extra_notes(f.extra)
            table.add_row(
                f.name,
                render.state_cell(f.state),
                str(f.pid) if f.pid else "-",
                render.fmt_age(
                    (snap.timestamp - state.parse_iso_utc(f.started_at)).total_seconds()
                    if f.started_at
                    else None
                ),
                render.fmt_age(f.last_heartbeat_age_s),
                notes,
                key=f.name,
            )

        # Restore cursor row.
        for i, f in enumerate(snap.frontends):
            if f.name == previously_selected:
                try:
                    table.move_cursor(row=i)
                except Exception:
                    pass
                break

        jobs_table = self.query_one("#jobs-content", DataTable)
        jobs_header = self.query_one("#jobs-header", Static)
        previously_selected_job = self._selected_job()
        jobs_table.clear()
        # The scheduler frontend is what actually fires cron jobs; if it
        # isn't running, the cron-derived "next fire" is misleading because
        # nothing will execute it. Show `-` in that case.
        schedule_running = any(
            f.name == "schedule" and f.state == "running" for f in snap.frontends
        )
        if snap.schedule_error:
            jobs_header.update(
                f"[bold]Scheduled jobs[/bold] [red]({snap.schedule_error})[/red]"
            )
        elif not schedule_running:
            jobs_header.update(
                "[bold]Scheduled jobs[/bold] [dim](scheduler stopped)[/dim]"
            )
        else:
            jobs_header.update("[bold]Scheduled jobs[/bold]")
        for j in snap.jobs:
            next_fire_str = (
                render._fmt_next_fire(j.next_fire, snap.timestamp)
                if schedule_running
                else "-"
            )
            jobs_table.add_row(
                j.name,
                j.cron,
                j.kind,
                next_fire_str,
                key=j.name,
            )
        for i, j in enumerate(snap.jobs):
            if j.name == previously_selected_job:
                try:
                    jobs_table.move_cursor(row=i)
                except Exception:
                    pass
                break

        tickets_table = self.query_one("#symphony-tickets", DataTable)
        symphony = next((f for f in snap.frontends if f.name == "symphony"), None)
        tickets = (symphony.extra.get("running_tickets") if symphony else None) or []
        # Preserve cursor by ticket identifier across refreshes; if the previously
        # selected ticket disappeared (agent finished it), the cursor falls back
        # to whatever row sits at the same index.
        previously_selected_ticket = self._selected_ticket()
        tickets_table.clear()
        # Rebuild the identifier → source map from this heartbeat snapshot so
        # the watch pane resolves to the correct per-source workspace dir.
        self._ticket_sources = {}
        for t in tickets:
            identifier = str(t.get("identifier", "?"))
            source = str(t.get("source") or "jira")
            self._ticket_sources[identifier] = source
            # One-char source badge ("J", "G") prefixed to the ticket cell —
            # keeps the table narrow while making the source obvious.
            badge = source[:1].upper() if source else "?"
            tickets_table.add_row(
                f"{badge} {identifier}",
                str(t.get("state", "?")),
                render.fmt_age(t.get("uptime_s")),
                render.fmt_age(t.get("last_turn_end_age_s")),
                str(t.get("failure_attempt", 0)),
                key=identifier,
            )
        for i, t in enumerate(tickets):
            if str(t.get("identifier")) == previously_selected_ticket:
                try:
                    tickets_table.move_cursor(row=i)
                except Exception:
                    pass
                break

        stale = [f.name for f in snap.frontends if f.stale]
        banner = self.query_one("#stale-banner", Static)
        if stale:
            names = ", ".join(stale)
            banner.update(
                f"[bold yellow]⚠ {len(stale)} daemon(s) out of date "
                f"({names}) — press Shift+K then u to upgrade.[/bold yellow]"
            )
        else:
            banner.update("")
