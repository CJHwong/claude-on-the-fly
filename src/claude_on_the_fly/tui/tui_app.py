"""Textual application — interactive dashboard for claude-on-the-fly.

Three screens (dashboard / logs / doctor) bound to keys. Dashboard is the
default.
"""

from __future__ import annotations

from textual.app import App

from claude_on_the_fly.tui.screens.dashboard import DashboardScreen
from claude_on_the_fly.tui.screens.doctor import DoctorScreen
from claude_on_the_fly.tui.screens.logs import LogsScreen


class ClaudeTuiApp(App):
    CSS = """
    #dashboard-body {
        padding: 1 2;
    }
    #dashboard-body #frontends {
        height: auto;
    }
    #dashboard-body #jobs-content {
        height: auto;
    }
    #dashboard-body #log-header {
        padding: 1 0 0 0;
    }
    #dashboard-body #log-pane {
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
    }

    def on_mount(self) -> None:
        self.push_screen("dashboard")


def run_app() -> None:
    ClaudeTuiApp().run()
