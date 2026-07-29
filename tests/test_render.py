"""Tests for tui.render — pure header/label builders for the dashboard."""

from __future__ import annotations

from claude_on_the_fly.tui.render import (
    cron_header,
)

# ---------------------------------------------------------------------------
# cron_header
# ---------------------------------------------------------------------------


class TestSchedulerHeader:
    def test_running_with_next_fire(self):
        line = cron_header(state="running", next_fire_str="Mon 09:00  (in 4m)")
        assert "CRON" in line
        assert "next fire" in line
        assert "in 4m" in line

    def test_running_no_jobs(self):
        line = cron_header(state="running", next_fire_str=None)
        assert "no jobs" in line

    def test_stopped_omits_next_fire(self):
        line = cron_header(state="stopped", next_fire_str="Mon 09:00  (in 4m)")
        assert "next fire" not in line
        assert "dim" in line  # stopped state style

    def test_error_takes_precedence(self):
        line = cron_header(
            state="running",
            next_fire_str="Mon 09:00  (in 4m)",
            schedule_error="bad cron",
        )
        assert "bad cron" in line
        assert "next fire" not in line
