"""Dashboard screen — two hero panels (symphony / scheduler) + a compact chat
strip + log tail.

Layout (top → bottom):
- SYMPHONY panel: bordered, rich header (running/cap + tracker labels), inner
  tickets table.
- SCHEDULER panel: bordered, rich header (state + next fire), inner jobs table.
- Chat strip: compact one-row-per-daemon table (telegram / slack / gmail).
- Log row: daemon log (left) + per-ticket / per-job watch (right).

Selection model: Tab cycles the three inner tables. `_active_daemon()` reads
whichever table is focused; the supervisor keys (s/k/r) act on that daemon, and
the log/watch panes follow it. Refresh runs at 1Hz, rebuilding tables from a
fresh state.snapshot() while preserving each table's cursor by row key.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Literal

from rich.text import Text

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from claude_on_the_fly.agent import DATA_DIR, current_backend_key, get_backend
from claude_on_the_fly.symphony import watch
from claude_on_the_fly.symphony.agent_runner import session_uuid_for
from claude_on_the_fly.symphony.workspace import WORKSPACES_ROOT, sanitize_key
from claude_on_the_fly.tui import env_editor, render, state, supervisor
from claude_on_the_fly.tui.screens.config_picker import ConfigPickerScreen
from claude_on_the_fly.tui.screens.env_diff import EnvDiffScreen

LOG_DIR = DATA_DIR / "logs"
SYMPHONY_CONFIG = DATA_DIR / "symphony.yaml"
SCHEDULE_CONFIG = DATA_DIR / "schedule.yaml"
TAIL_LINES = 200
# When showing a symphony ticket watch, tail this many raw JSONL events; each
# formats to 1–4 visible lines so the rendered pane stays manageable.
WATCH_EVENTS = 80
# Cap on RichLog growth so a 24/7 dashboard doesn't accumulate unbounded memory.
LOG_PANE_MAX_LINES = 10_000

# Reactive, user-driven daemons. Demoted to the compact strip so the two
# autonomous engines (symphony, scheduler) own the top of the dashboard.
CHAT_FRONTENDS: tuple[str, ...] = ("telegram", "slack", "gmail")

# Memoize parsed symphony config by (path, mtime) so the 1Hz header refresh
# doesn't reparse YAML every tick. Mirrors state._load_schedule_cached.
_symphony_cfg_cache: tuple[Path, float, tuple] | None = None


def _load_symphony_cached(path: Path) -> tuple[object | None, str | None]:
    """Return (config, error). Missing file → (None, None); parse failure →
    (None, message). Cached by mtime so a broken file isn't reparsed each tick.
    """
    global _symphony_cfg_cache
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None, None
    if (
        _symphony_cfg_cache
        and _symphony_cfg_cache[0] == path
        and _symphony_cfg_cache[1] == mtime
    ):
        return _symphony_cfg_cache[2]

    from claude_on_the_fly.symphony.config import load_config

    try:
        result: tuple = (load_config(path), None)
    except Exception as exc:
        result = (None, str(exc))
    _symphony_cfg_cache = (path, mtime, result)
    return result


class DashboardScreen(Screen):
    DEFAULT_CSS = """
    #symphony-panel, #scheduler-panel {
        border: round grey;
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
    }
    #symphony-panel:focus-within, #scheduler-panel:focus-within {
        border: round $accent;
    }
    #symphony-header, #scheduler-header, #chat-strip-header {
        height: auto;
        padding: 0 0 0 1;
    }
    #symphony-tickets, #jobs-content, #chat-strip {
        height: auto;
    }
    #chat-strip:focus {
        border-left: thick $accent;
    }
    #action-cue {
        height: auto;
        padding: 0 0 0 1;
    }
    """

    # Every key is shown in the footer, ordered by how often it's reached for:
    # daemon lifecycle, then views, then config/diagnostics, then utilities,
    # then the rare/destructive Stop-all, then Quit. Lifecycle keys act on the
    # focused panel (see the action cue). `c` copies the highlighted log tail
    # via OSC 52 (iTerm2/kitty/WezTerm/Alacritty); hold Option (macOS) or Shift
    # while click-dragging for the terminal's own partial-selection copy.
    BINDINGS = [
        ("s", "start", "Start"),
        ("k", "stop", "Stop"),
        ("r", "restart", "Restart"),
        ("u", "resume", "Resume"),
        ("l", "app.push_screen('logs')", "Logs"),
        ("h", "app.push_screen('history')", "History"),
        # `g` edits the focused panel's config: symphony → symphony.yaml
        # (preview), scheduler → schedule.yaml, chat → .env.
        ("g", "open_config", "Config"),
        ("d", "app.push_screen('doctor')", "Doctor"),
        ("c", "copy_log", "Copy tail"),
        ("R", "refresh_now", "Refresh"),
        ("K", "stop_all", "Stop all"),
        ("q", "app.quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Daemon log pane state.
        self._log_path: Path | None = None
        self._log_mtime: float | None = None
        # Watch pane state, tracked separately so the two panes refresh
        # independently. _watch_target encodes what's being watched, e.g.
        # "session:symphony:PROJ-1" or "schedule:cleanup-job", so we know to
        # force a reload when the user navigates to a different item.
        self._watch_path: Path | None = None
        self._watch_mtime: float | None = None
        self._watch_target: str | None = None
        # ticket identifier → tracker source (jira | github), so the watch pane
        # resolves the per-tracker workspace dir.
        self._ticket_sources: dict[str, str] = {}
        # "<frontend>:<identifier>" → session_uuid for the watch pane.
        self._job_sessions: dict[str, str] = {}
        # chat daemon name → list of (identifier, session_uuid) running jobs,
        # so selecting a chat daemon can still drill into its live session.
        self._chat_jobs: dict[str, list[tuple[str, str]]] = {}
        self._busy_msg: str | None = None
        self._busy_ticks: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="dashboard-body"):
            yield Static(id="stale-banner", markup=True)
            with Vertical(id="symphony-panel"):
                yield Static(id="symphony-header", markup=True)
                yield DataTable(
                    id="symphony-tickets", cursor_type="row", zebra_stripes=True
                )
            with Vertical(id="scheduler-panel"):
                yield Static(id="scheduler-header", markup=True)
                yield DataTable(
                    id="jobs-content", cursor_type="row", zebra_stripes=True
                )
            yield Static(id="chat-strip-header", markup=True)
            yield DataTable(id="chat-strip", cursor_type="row", zebra_stripes=True)
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
            yield Static(id="action-cue", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        tickets = self.query_one("#symphony-tickets", DataTable)
        tickets.add_column("job", width=24)
        tickets.add_column("state", width=14)
        tickets.add_column("uptime", width=8)
        tickets.add_column("last turn", width=10)
        tickets.add_column("retries", width=8)
        jobs = self.query_one("#jobs-content", DataTable)
        jobs.add_column("name", width=14)
        jobs.add_column("cron", width=16)
        jobs.add_column("kind", width=8)
        jobs.add_column("next fire", width=24)
        chat = self.query_one("#chat-strip", DataTable)
        chat.add_column("daemon", width=10)
        chat.add_column("state", width=12)
        chat.add_column("heartbeat", width=10)
        chat.add_column("active", width=8)
        self.query_one("#chat-strip-header", Static).update(
            "[bold]Chat frontends[/bold] [dim](Tab to cycle panels)[/dim]"
        )
        # The log panes shouldn't grab Tab focus — Tab cycles the three zones
        # (symphony tickets / scheduler jobs / chat strip) only.
        self.query_one("#log-pane", RichLog).can_focus = False
        self.query_one("#watch-pane", RichLog).can_focus = False
        self._refresh()
        self.set_interval(1.0, self._refresh)
        self.set_interval(1.0, self._refresh_log)
        self.set_interval(0.1, self._tick_busy)
        # Land focus on the symphony tickets table so the hero engine is the
        # default supervisor target and Tab cycles from there.
        self.query_one("#symphony-tickets", DataTable).focus()
        self._update_action_cue()

    # ------------------------------------------------------------------
    # Selection / focus
    # ------------------------------------------------------------------

    def _active_daemon(self) -> str | None:
        """Which daemon the supervisor keys act on — the one whose table has
        focus. Chat strip resolves to the highlighted chat daemon row."""
        focused = self.app.focused
        wid = getattr(focused, "id", None)
        if wid == "symphony-tickets":
            return "symphony"
        if wid == "jobs-content":
            return "schedule"
        if wid == "chat-strip":
            return self._datatable_cursor_key("#chat-strip")
        return None

    def _selected_ticket(self) -> str | None:
        """Highlighted symphony ticket key ("symphony:<identifier>"), or None."""
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

    @staticmethod
    def _restore_cursor(
        table: DataTable, keys: list[str], previously: str | None
    ) -> None:
        if previously is None:
            return
        for i, key in enumerate(keys):
            if key == previously:
                try:
                    table.move_cursor(row=i)
                except Exception:
                    pass
                return

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Reload the log pane immediately when the cursor moves. Moving within
        # the chat strip also changes which daemon the lifecycle keys target.
        self._refresh_log(force_reload=True)
        self._update_action_cue()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # Tabbing between zones changes which daemon the lifecycle keys target.
        self._update_action_cue()

    def _update_action_cue(self) -> None:
        target = self._active_daemon()
        try:
            cue = self.query_one("#action-cue", Static)
        except Exception:
            return  # focus event fired before the cue mounted
        if target:
            cue.update(f"[dim]acting on:[/dim] [bold]{target}[/bold]")
        else:
            cue.update("[dim]acting on: (Tab to a panel)[/dim]")

    def action_refresh_now(self) -> None:
        self._refresh()
        self._refresh_log(force_reload=True)

    # ------------------------------------------------------------------
    # Supervisor actions
    # ------------------------------------------------------------------

    async def _run_supervisor_action(
        self,
        verb: str,  # "start" | "stop" | "restart"
        op: Callable[[str], int],
    ) -> None:
        name = self._active_daemon()
        if self._busy_msg:
            return
        if name is None:
            self._notify("no daemon selected (Tab to a panel first)", "warning")
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

    def action_open_config(self) -> None:
        """Pick a config to edit via a modal. Decoupled from focus on purpose —
        config edits are rare and deliberate, so an explicit pick beats a key
        whose meaning shifts with whatever panel is focused."""
        self.app.push_screen(ConfigPickerScreen(), self._open_config_target)

    def _open_config_target(self, choice: str | None) -> None:
        if choice == "symphony":
            self.app.push_screen("config")
        elif choice == "schedule":
            self._edit_schedule_config()
        elif choice == "env":
            self._edit_env()

    def _edit_schedule_config(self) -> None:
        from claude_on_the_fly.scheduler import EXAMPLE_YAML

        with self.app.suspend():
            env_editor.open_in_editor(SCHEDULE_CONFIG, seed=EXAMPLE_YAML)
        self._refresh()
        self._notify(f"edited {SCHEDULE_CONFIG.name}", "information")

    def _edit_env(self) -> None:
        env_file = supervisor.DEFAULT_ENV_FILE
        with self.app.suspend():
            diff = env_editor.edit_and_diff(env_file)
        if diff.is_empty():
            self._notify("no env changes", "information")
            return
        self.app.push_screen(EnvDiffScreen(diff))
        self._refresh()

    # ------------------------------------------------------------------
    # Busy spinner
    # ------------------------------------------------------------------

    def _tick_busy(self) -> None:
        if self._busy_msg is None:
            return
        self._busy_ticks += 1
        self._render_busy_line()

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

    # ------------------------------------------------------------------
    # Log + watch panes
    # ------------------------------------------------------------------

    def _current_log_path(self) -> Path | None:
        """Whichever log pane is more specific takes precedence: the watch
        pane (per-ticket session log) when something's highlighted there,
        otherwise the daemon log for the active daemon."""
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

    def _refresh_log(self, *, force_reload: bool = False) -> None:
        """Refresh both panes: daemon log on the left, ticket watch on the right."""
        self._refresh_daemon_log(force_reload=force_reload)
        self._refresh_watch_pane(force_reload=force_reload)

    def _refresh_daemon_log(self, *, force_reload: bool) -> None:
        name = self._active_daemon() or "symphony"
        header = self.query_one("#log-header", Static)
        pane = self.query_one("#log-pane", RichLog)

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
        """Show the watch column when the active daemon has something to drill
        into: a highlighted symphony ticket, a highlighted scheduler job, or a
        chat daemon with a running job. Otherwise the column is hidden so the
        daemon log gets full width.
        """
        col = self.query_one("#log-watch-col", Vertical)
        name = self._active_daemon()

        # "session:<frontend>:<identifier>" for an AI job, "schedule:<name>"
        # for a cron job tail.
        target: str | None = None
        if name == "symphony":
            cursor = self._selected_ticket()
            if cursor is not None and cursor.startswith("symphony:"):
                target = f"session:{cursor}"
        elif name == "schedule":
            job = self._selected_job()
            if job is not None and job != "__empty__":
                target = f"schedule:{job}"
        elif name in CHAT_FRONTENDS:
            jobs = self._chat_jobs.get(name) or []
            if jobs:
                target = f"session:{name}:{jobs[0][0]}"

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
        if mode == "session":
            # rest = "<frontend>:<identifier>"
            source, _, identifier = rest.partition(":")
            self._refresh_watch_session(source, identifier, header, pane, force_reload)
        else:
            self._refresh_watch_scheduler(rest, header, pane, force_reload)

    def _refresh_watch_session(
        self,
        source: str,
        identifier: str,
        header: Static,
        pane: RichLog,
        force_reload: bool,
    ) -> None:
        """Tail the live backend session JSONL for any AI job row.

        Symphony workspaces live under `WORKSPACES_ROOT/<tracker>/<key>`;
        chat workspaces live under `DATA_DIR/workspaces/<frontend>/<user>`.
        The backend itself is agnostic — it just needs (workspace, uuid).
        """
        if source == "symphony":
            tracker = self._ticket_sources.get(identifier, "jira")
            workspace = WORKSPACES_ROOT / tracker / sanitize_key(identifier)
        else:
            # `identifier` is the chat workspace_name, e.g. "telegram/H".
            workspace = DATA_DIR / "workspaces" / identifier

        session_uuid = self._job_sessions.get(f"{source}:{identifier}")
        if not session_uuid:
            if force_reload or self._watch_path is not None:
                self._watch_path = None
                self._watch_mtime = None
                pane.clear()
                pane.write(f"[dim]no session uuid for {identifier} yet[/dim]")
                header.update(f"[bold]watch: {identifier}[/bold] [dim](pending)[/dim]")
            return

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
                    f"[dim]no session log yet for {identifier} — "
                    f"agent hasn't run a turn[/dim]"
                )
                header.update(
                    f"[bold]watch: {identifier}[/bold] [dim](no session yet)[/dim]"
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
        header.update(f"[bold]watch: {identifier}[/bold] [dim]{path.name}[/dim]")
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

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        snap = state.snapshot()
        by_name = {f.name: f for f in snap.frontends}
        # Rebuilt every tick from the heartbeat; reset before repopulating.
        self._ticket_sources = {}
        self._job_sessions = {}
        self._chat_jobs = {}

        self._refresh_symphony(by_name.get("symphony"))
        self._refresh_scheduler(snap, by_name.get("schedule"))
        self._refresh_chat_strip(by_name)
        self._refresh_stale_banner(snap)

    def _refresh_symphony(self, sym: state.FrontendStatus | None) -> None:
        cfg, cfg_error = _load_symphony_cached(SYMPHONY_CONFIG)
        labels = render.tracker_labels(cfg) if cfg else []
        cap = render.symphony_cap(cfg) if cfg else 0
        extra = (sym.extra or {}) if sym else {}
        running = int(extra.get("running", 0) or 0)
        self.query_one("#symphony-header", Static).update(
            render.symphony_header(
                state=sym.state if sym else "stopped",
                running=running,
                cap=cap,
                labels=labels,
                hb_age_s=sym.last_heartbeat_age_s if sym else None,
                error=(sym.error if sym and sym.error else cfg_error),
                stale=bool(sym.stale) if sym else False,
            )
        )

        table = self.query_one("#symphony-tickets", DataTable)
        previously = self._selected_ticket()
        table.clear()
        keys: list[str] = []
        for ticket in extra.get("running_tickets") or []:
            identifier = str(ticket.get("identifier", "?"))
            tracker = str(ticket.get("source") or "jira")
            self._ticket_sources[identifier] = tracker
            # Derive the session uuid with the daemon's current backend_key —
            # mirrors what the orchestrator does when dispatching.
            self._job_sessions[f"symphony:{identifier}"] = session_uuid_for(
                identifier, source=tracker, backend_key=current_backend_key()
            )
            key = f"symphony:{identifier}"
            keys.append(key)
            table.add_row(
                identifier,
                str(ticket.get("state", "?")),
                render.fmt_age(ticket.get("uptime_s")),
                render.fmt_age(ticket.get("last_turn_end_age_s")),
                str(ticket.get("failure_attempt", 0)),
                key=key,
            )
        if not keys:
            # Keep one row so the table still reads as "here, but empty" rather
            # than a bare header. A placeholder row also keeps the table
            # focusable for Tab / lifecycle keys.
            table.add_row(
                Text("no active jobs", style="dim"), "", "", "", "", key="__empty__"
            )
        else:
            self._restore_cursor(table, keys, previously)

    def _refresh_scheduler(
        self, snap: state.Snapshot, sched: state.FrontendStatus | None
    ) -> None:
        sched_running = bool(sched and sched.state == "running")
        next_fire_str: str | None = None
        if snap.jobs and sched_running:
            nxt = min(snap.jobs, key=lambda j: j.next_fire)
            next_fire_str = render._fmt_next_fire(nxt.next_fire, snap.timestamp)
        self.query_one("#scheduler-header", Static).update(
            render.scheduler_header(
                state=sched.state if sched else "stopped",
                next_fire_str=next_fire_str,
                schedule_error=snap.schedule_error,
            )
        )

        table = self.query_one("#jobs-content", DataTable)
        previously = self._selected_job()
        table.clear()
        keys: list[str] = []
        for job in snap.jobs:
            next_str = (
                render._fmt_next_fire(job.next_fire, snap.timestamp)
                if sched_running
                else "-"
            )
            table.add_row(job.name, job.cron, job.kind, next_str, key=job.name)
            keys.append(job.name)
        if not keys:
            # Short enough to fit the 14-wide name column; `g` opens the config.
            table.add_row(Text("no jobs (g)", style="dim"), "", "", "", key="__empty__")
        else:
            self._restore_cursor(table, keys, previously)

    def _refresh_chat_strip(self, by_name: dict[str, state.FrontendStatus]) -> None:
        table = self.query_one("#chat-strip", DataTable)
        previously = self._datatable_cursor_key("#chat-strip")
        table.clear()
        keys: list[str] = []
        for name in CHAT_FRONTENDS:
            frontend = by_name.get(name)
            if frontend is None:
                continue
            extra = frontend.extra or {}
            jobs: list[tuple[str, str]] = []
            for job in extra.get("running_jobs") or []:
                identifier = str(job.get("identifier", "?"))
                session = job.get("session_uuid")
                if session:
                    self._job_sessions[f"{name}:{identifier}"] = str(session)
                jobs.append((identifier, str(session) if session else ""))
            self._chat_jobs[name] = jobs
            table.add_row(
                name,
                render.state_cell(frontend.state),
                render.fmt_age(frontend.last_heartbeat_age_s),
                str(len(jobs)) if jobs else "-",
                key=name,
            )
            keys.append(name)
        self._restore_cursor(table, keys, previously)

    def _refresh_stale_banner(self, snap: state.Snapshot) -> None:
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
