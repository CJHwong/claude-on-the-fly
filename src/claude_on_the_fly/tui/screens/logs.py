"""Logs screen — pick any .log file under ~/.claude-on-the-fly/logs/, tail it.

Complements the dashboard's inline log pane (which is glued to the highlighted
frontend). This screen lets you browse every log file in the directory, newest
first: one per (role, host, day), so it also lists earlier days and per-job
cron logs (`cron-<entry>-<host>-<date>.log`).
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, ListItem, ListView, RichLog, Static

from claude_on_the_fly.agent import DATA_DIR
from claude_on_the_fly.tui.render import tail_lines
from claude_on_the_fly.tui.screens.overlay import OverlayScreen

LOG_DIR = DATA_DIR / "logs"
TAIL_LINES = 500


def _available_logs() -> list[Path]:
    if not LOG_DIR.is_dir():
        return []
    return sorted(
        (p for p in LOG_DIR.iterdir() if p.is_file() and p.suffix == ".log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


class LogsScreen(OverlayScreen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.pop_screen", "Back"),
        ("r", "refresh_now", "Refresh"),
    ]

    def __init__(self, preselect: Path | None = None) -> None:
        super().__init__()
        self._files: list[Path] = []
        self._selected: Path | None = None
        self._rendered_mtime: float | None = None
        # Which file to open on. Set by a caller that already knows which log
        # answers the question, so the operator does not have to find it in a
        # list sorted by mtime.
        self._preselect = preselect

    def compose(self) -> ComposeResult:
        with Horizontal(id="overlay-box"):
            with Vertical(id="logs-sidebar"):
                yield Static("Logs", id="logs-title")
                yield ListView(id="logs-list")
            with Vertical(id="logs-main"):
                yield RichLog(id="logs-content", wrap=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self._reload_list()
        self.set_interval(1.0, self._tail_selected_if_changed)

    def action_refresh_now(self) -> None:
        self._reload_list()
        self._render_selected_full()

    def _reload_list(self) -> None:
        self._files = _available_logs()
        # A named file is shown even when the listing rule would exclude it. The
        # listing covers `.log` only, and the one file worth opening after a
        # daemon refused to start is its `.stdout` capture -- the tracebacks the
        # daemon wrote before any log handler existed. First, because it is what
        # the caller opened this screen to show.
        if (
            self._preselect is not None
            and self._preselect not in self._files
            and self._preselect.is_file()
        ):
            self._files.insert(0, self._preselect)
        view = self.query_one("#logs-list", ListView)
        view.clear()
        for p in self._files:
            view.append(ListItem(Static(p.name)))
        if self._files and self._selected is None:
            # The newest log is what a bare open wants; a caller that named a
            # file wants that one, when it is still on disk.
            target = (
                self._preselect if self._preselect in self._files else self._files[0]
            )
            self._selected = target
            view.index = self._files.index(target)
            self._render_selected_full()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is None or idx >= len(self._files):
            return
        new_selection = self._files[idx]
        if new_selection != self._selected:
            self._selected = new_selection
            self._rendered_mtime = None  # force render of the new file
            self._render_selected_full()

    def _render_selected_full(self) -> None:
        if self._selected is None:
            return
        log = self.query_one("#logs-content", RichLog)
        log.clear()
        log.write(f"=== {self._selected} ===")
        for line in tail_lines(self._selected, TAIL_LINES):
            log.write(line.rstrip("\n"))
        try:
            self._rendered_mtime = self._selected.stat().st_mtime
        except OSError:
            self._rendered_mtime = None

    def _tail_selected_if_changed(self) -> None:
        # Only re-read when mtime advanced — saves a full-file read every
        # second when the daemon is quiet.
        if self._selected is None:
            return
        try:
            mtime = self._selected.stat().st_mtime
        except OSError:
            return
        if mtime == self._rendered_mtime:
            return
        self._render_selected_full()
