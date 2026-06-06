"""Tests for tui.render — pure header/label builders for the dashboard."""

from __future__ import annotations

from claude_on_the_fly.tui.render import (
    scheduler_header,
    symphony_strip_header,
)


# ---------------------------------------------------------------------------
# symphony_strip_header
# ---------------------------------------------------------------------------


class TestSymphonyStripHeader:
    def test_multi_tracker_reverse_videos_selected(self):
        line = symphony_strip_header(
            [("jira", "running"), ("github", "running")], selected=0, active=2
        )
        assert "SYMPHONY" in line
        assert "jira" in line
        assert "github" in line
        assert "reverse" in line  # the selected tracker is highlighted
        assert "2 active" in line

    def test_disabled_tracker_shows_disabled_suffix(self):
        line = symphony_strip_header(
            [("jira", "running"), ("github", "disabled")], selected=0, active=0
        )
        assert "disabled" in line
        assert "idle" in line  # selected (jira) has no active jobs

    def test_single_tracker_collapses_to_one_line(self):
        line = symphony_strip_header([("jira", "running")], selected=0, active=1)
        assert "jira" in line
        assert "1 active" in line
        assert "reverse" not in line  # nothing to switch between

    def test_no_trackers_reads_as_unconfigured(self):
        line = symphony_strip_header([], selected=0, active=0)
        assert "no trackers configured" in line

    def test_config_error_is_surfaced(self):
        line = symphony_strip_header([], selected=0, active=0, error="bad yaml")
        assert "no trackers configured" in line
        assert "bad yaml" in line


# ---------------------------------------------------------------------------
# scheduler_header
# ---------------------------------------------------------------------------


class TestSchedulerHeader:
    def test_running_with_next_fire(self):
        line = scheduler_header(state="running", next_fire_str="Mon 09:00  (in 4m)")
        assert "SCHEDULER" in line
        assert "next fire" in line
        assert "in 4m" in line

    def test_running_no_jobs(self):
        line = scheduler_header(state="running", next_fire_str=None)
        assert "no jobs" in line

    def test_stopped_omits_next_fire(self):
        line = scheduler_header(state="stopped", next_fire_str="Mon 09:00  (in 4m)")
        assert "next fire" not in line
        assert "dim" in line  # stopped state style

    def test_error_takes_precedence(self):
        line = scheduler_header(
            state="running",
            next_fire_str="Mon 09:00  (in 4m)",
            schedule_error="bad cron",
        )
        assert "bad cron" in line
        assert "next fire" not in line
