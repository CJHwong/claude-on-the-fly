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
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from claude_on_the_fly.agent import DATA_DIR
from claude_on_the_fly.checks import SUPERVISABLE_FRONTENDS
from claude_on_the_fly.tui import env_editor, render, state, supervisor
from claude_on_the_fly.tui.screens.env_diff import EnvDiffScreen

LOG_DIR = DATA_DIR / "logs"
TAIL_LINES = 200


class DashboardScreen(Screen):
    BINDINGS = [
        ("l", "app.push_screen('logs')", "Logs"),
        ("d", "app.push_screen('doctor')", "Doctor"),
        ("s", "start", "Start"),
        ("k", "stop", "Stop"),
        ("r", "restart", "Restart"),
        ("K", "stop_all", "Stop all"),
        ("u", "resume", "Resume"),
        ("e", "edit_env", "Edit .env"),
        ("R", "refresh_now", "Refresh"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._log_path: Path | None = None
        self._log_mtime: float | None = None
        self._busy_msg: str | None = None
        self._busy_ticks: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="dashboard-body"):
            yield Static(id="stale-banner", markup=True)
            yield DataTable(id="frontends", cursor_type="row", zebra_stripes=True)
            yield Static(id="jobs-content")
            yield Static(id="log-header", markup=True)
            yield RichLog(id="log-pane", wrap=False, highlight=False, auto_scroll=True)
            yield Static(id="status-line", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#frontends", DataTable)
        table.add_columns("name", "state", "pid", "uptime", "heartbeat", "notes")
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
            return  # nothing to update

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
                notes = ", ".join(f"{k}={v}" for k, v in sorted(f.extra.items()))
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

        jobs_widget = self.query_one("#jobs-content", Static)
        if snap.jobs:
            jobs_widget.update(render.jobs_table(snap.jobs, snap.timestamp))
        elif snap.schedule_error:
            jobs_widget.update(
                Text(f"Scheduler config error: {snap.schedule_error}", style="red")
            )
        else:
            jobs_widget.update(Text("No schedule.yaml found.", style="dim"))

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
