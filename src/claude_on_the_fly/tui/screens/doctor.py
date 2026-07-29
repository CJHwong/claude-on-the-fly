"""Doctor screen — run checks.check_all() and render results with fix hints."""

from __future__ import annotations

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from claude_on_the_fly import checks
from claude_on_the_fly.tui import supervisor
from claude_on_the_fly.tui.screens.overlay import OverlayScreen

_STATUS_STYLES = {
    "ok": "green",
    "missing": "yellow",
    "invalid": "red",
    "warn": "yellow",
}


def _group_table(group_name: str, results: list[checks.CheckResult]) -> Table:
    table = Table(title=group_name, show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("status")
    table.add_column("detail")
    table.add_column("fix", overflow="fold")

    for r in results:
        status = Text(r.status, style=_STATUS_STYLES.get(r.status, ""))
        fix = r.fix_hint or ""
        table.add_row(r.name, status, r.detail, fix)
    return table


class DoctorScreen(OverlayScreen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.pop_screen", "Back"),
        ("r", "refresh_now", "Re-run"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="overlay-box"):
            yield Static(id="doctor-content")
        yield Footer()

    def on_screen_resume(self) -> None:
        """Re-run the checks every time this screen becomes visible.

        `on_mount` is not enough: the screen is registered in `App.SCREENS`, so
        Textual builds one instance and re-pushes it, and mount fires only the
        first time. A doctor that shows the verdict from the last time you looked
        is worse than one that shows nothing — you fix the thing it complained
        about, re-open it, and it tells you the fix did not work.

        Resume rather than an interval: this is a point-in-time check, not a live
        monitor, and it shells out to `which` for the binary checks.
        """
        self._refresh()

    def action_refresh_now(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        env = supervisor._load_env(supervisor.DEFAULT_ENV_FILE)
        all_checks = checks.check_all(env)
        tables = [_group_table(name, results) for name, results in all_checks.items()]
        self.query_one("#doctor-content", Static).update(Group(*tables))
