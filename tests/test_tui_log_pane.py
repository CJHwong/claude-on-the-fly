"""Regression coverage for the dashboard's live-tail log pane.

Mounts DashboardScreen in a minimal host app and drives _refresh_daemon_log
directly across the missing<->present log transitions.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import RichLog

import claude_on_the_fly.tui.screens.dashboard as dash
from claude_on_the_fly import logs
from claude_on_the_fly.tui.screens.dashboard import DashboardScreen


class _DashboardOnlyApp(App):
    CSS = """
    #log-row { height: 1fr; min-height: 8; }
    #log-pane, #watch-pane { height: 1fr; min-height: 8; }
    """

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen())


def _isolate(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "cron.yaml"
    cfg.write_text(
        "trackers:\n"
        "  jira:\n"
        "    base_url: https://x.atlassian.net\n"
        "    project_key: PROJ\n"
        "    enabled: true\n"
    )
    monkeypatch.setattr(dash, "CRON_CONFIG", cfg)
    monkeypatch.setattr("claude_on_the_fly.tui.state.STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(dash, "LOG_DIR", tmp_path)
    monkeypatch.setattr(DashboardScreen, "_active_daemon", lambda self: "cron")


@pytest.mark.asyncio
async def test_daemon_log_missing_present_recovery(tmp_path, monkeypatch):
    """The live-tail pane must repaint when a missing log appears (and when a
    shown log is deleted in place). Regression: the offset path bailed at
    `if not new_lines` and left the stale '(missing)' header/line stuck once the
    file appeared."""
    _isolate(tmp_path, monkeypatch)
    log = tmp_path / logs.log_name("cron")

    app = _DashboardOnlyApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(
            screen, DashboardScreen
        )  # narrow for the private-method calls
        pane = screen.query_one("#log-pane", RichLog)

        def text() -> list[str]:
            return ["".join(seg.text for seg in line) for line in pane.lines]

        # Open before the daemon has written its log.
        screen._refresh_daemon_log(force_reload=True)
        assert any("no log yet" in ln for ln in text())

        # File appears: missing UI clears, backlog stays hidden.
        log.write_text("".join(f"old {i}\n" for i in range(20)))
        screen._refresh_daemon_log(force_reload=False)
        assert text() == ["live tail · full log in [l]"]

        # New lines append.
        with log.open("a") as handle:
            handle.write("live-1\nlive-2\n")
        screen._refresh_daemon_log(force_reload=False)
        assert text()[-2:] == ["live-1", "live-2"]

        # Deleted in place: missing UI repaints once.
        log.unlink()
        screen._refresh_daemon_log(force_reload=False)
        assert any("no log yet" in ln for ln in text())
