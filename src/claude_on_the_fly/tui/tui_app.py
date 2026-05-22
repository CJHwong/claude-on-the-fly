"""Textual application — interactive dashboard for claude-on-the-fly.

Three screens (dashboard / logs / doctor) bound to keys. Dashboard is the
default.
"""

from __future__ import annotations

from textual.app import App

from claude_on_the_fly.tui.screens.dashboard import DashboardScreen
from claude_on_the_fly.tui.screens.doctor import DoctorScreen
from claude_on_the_fly.tui.screens.history import HistoryScreen
from claude_on_the_fly.tui.screens.logs import LogsScreen


class ClaudeTuiApp(App):
    CSS = """
    #dashboard-body {
        padding: 1 2;
    }
    #dashboard-body #stale-banner {
        height: auto;
        padding: 0 0 1 0;
    }
    #dashboard-body #frontends {
        height: auto;
    }
    #dashboard-body #jobs-row {
        height: auto;
    }
    #dashboard-body #jobs-pane {
        width: 1fr;
        height: auto;
    }
    #dashboard-body #symphony-pane {
        width: 1fr;
        height: auto;
        padding: 0 0 0 2;
    }
    #dashboard-body #jobs-header,
    #dashboard-body #symphony-header {
        height: auto;
        padding: 0 0 0 1;
    }
    #dashboard-body #jobs-content,
    #dashboard-body #symphony-tickets {
        height: auto;
    }
    #dashboard-body #symphony-tickets-hint {
        height: auto;
    }
    #dashboard-body #log-row {
        height: 1fr;
        min-height: 8;
    }
    #dashboard-body #log-daemon-col {
        width: 1fr;
        height: 1fr;
    }
    #dashboard-body #log-watch-col {
        width: 1fr;
        height: 1fr;
        padding: 0 0 0 2;
    }
    #dashboard-body #log-header,
    #dashboard-body #watch-header {
        padding: 1 0 0 0;
        height: auto;
    }
    #dashboard-body #log-pane,
    #dashboard-body #watch-pane {
        height: 1fr;
        min-height: 8;
        border: solid grey;
    }
    #logs-sidebar {
        width: 30;
        border-right: solid grey;
    }
    #logs-title {
        padding: 0 1;
        text-style: bold;
    }
    #logs-list {
        height: 1fr;
    }
    #logs-main {
        padding: 0 1;
    }
    #logs-content {
        height: 1fr;
    }
    """

    SCREENS = {
        "dashboard": DashboardScreen,
        "logs": LogsScreen,
        "doctor": DoctorScreen,
        "history": HistoryScreen,
    }

    def on_mount(self) -> None:
        self.push_screen("dashboard")


def run_app() -> None:
    ClaudeTuiApp().run()
