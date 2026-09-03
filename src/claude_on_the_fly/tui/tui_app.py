"""Textual application — interactive dashboard for claude-on-the-fly.

Three screens (dashboard / logs / doctor) bound to keys. Dashboard is the
default.
"""

from __future__ import annotations

import os
from typing import Any

from textual.app import App

from claude_on_the_fly import upgrade
from claude_on_the_fly.events import (
    EVENT_WORKER_DONE,
    EVENT_WORKER_FAILED,
    EventLog,
)
from claude_on_the_fly.tui.screens.dashboard import DashboardScreen
from claude_on_the_fly.tui.screens.doctor import DoctorScreen
from claude_on_the_fly.tui.screens.history import HistoryScreen
from claude_on_the_fly.tui.screens.logs import LogsScreen

# Event types that count as a "job finished" worth surfacing as a toast + bell.
_WORKER_EVENT_TYPES = (EVENT_WORKER_DONE, EVENT_WORKER_FAILED)


def _event_sig(record: dict[str, Any]) -> tuple:
    """Identity of an event row, stable across polls. ts has only second
    precision, so include identifier + session_uuid to disambiguate events
    that share a timestamp."""
    return (
        record.get("ts"),
        record.get("type"),
        record.get("identifier"),
        record.get("session_uuid"),
    )


def _events_since(records: list[dict[str, Any]], last_sig: tuple | None) -> list[dict]:
    """Rows appended after the one identified by `last_sig` (exclusive).

    `last_sig is None` → nothing is "new" yet (the caller is priming its
    baseline). If `last_sig` has aged out of the tail window, treat the whole
    window as new — better to over-notify than to silently drop completions.
    """
    if last_sig is None:
        return []
    for i in range(len(records) - 1, -1, -1):
        if _event_sig(records[i]) == last_sig:
            return records[i + 1 :]
    return records


class ClaudeTuiApp(App):
    # Shown in the Header bar.
    TITLE = "Claude On The Fly"

    # No ctrl+p command palette — this is a fixed-purpose supervisor, not a
    # general app; the footer already lists every action.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #dashboard-body {
        padding: 1 2;
    }
    #dashboard-body #stale-banner {
        height: auto;
        padding: 0 0 1 0;
    }
    #dashboard-body #log-row {
        height: 1fr;
        min-height: 8;
    }
    #dashboard-body #log-daemon-col {
        width: 1fr;
        height: 1fr;
    }
    #dashboard-body #live-col {
        width: 1fr;
        height: 1fr;
        padding: 0 0 0 2;
    }
    #dashboard-body #log-header,
    #dashboard-body #live-header {
        padding: 1 0 0 0;
        height: auto;
    }
    #dashboard-body #log-pane,
    #dashboard-body #live-view {
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

    # How many recent events a poll inspects. Generous so bursts of completions
    # between two 2s polls don't age the baseline out of the window.
    _EVENT_TAIL = 50

    # Set by the upgrade action. A process keeps the code it loaded, so showing
    # the new version means handing this terminal to a new process — done after
    # `run()` returns, where Textual has already restored the terminal, rather
    # than exec'ing out from under a live screen.
    relaunch_on_exit = False

    def on_mount(self) -> None:
        self.push_screen("dashboard")
        # Watch the shared event log app-wide (not per-screen), so a finished
        # job toasts + bells no matter which screen is on top.
        self._event_log = EventLog()
        self._last_event_sig = self._latest_event_sig()
        self.set_interval(2.0, self._poll_worker_events)

    def _latest_event_sig(self) -> tuple | None:
        records = self._event_log.tail(self._EVENT_TAIL)
        return _event_sig(records[-1]) if records else None

    def _poll_worker_events(self) -> None:
        records = self._event_log.tail(self._EVENT_TAIL)
        if not records:
            return
        for record in _events_since(records, self._last_event_sig):
            if record.get("type") in _WORKER_EVENT_TYPES:
                self._notify_worker_event(record)
        self._last_event_sig = _event_sig(records[-1])

    def _notify_worker_event(self, record: dict[str, Any]) -> None:
        identifier = record.get("identifier") or "?"
        if record.get("type") == EVENT_WORKER_FAILED:
            self.notify(f"{identifier} failed", title="agent job", severity="error")
        else:
            reason = record.get("reason") or record.get("state") or "done"
            self.notify(
                f"{identifier} — {reason}",
                title="agent job",
                severity="information",
            )
        self.bell()  # no-op when headless (tests); BEL to the terminal otherwise


def run_app(*, exec_relaunch=os.execv) -> None:
    app = ClaudeTuiApp()
    app.run()
    if app.relaunch_on_exit:
        argv = upgrade.relaunch_argv()
        exec_relaunch(argv[0], argv)
