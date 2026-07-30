"""Tests for tui.render — pure header/label builders for the dashboard."""

from __future__ import annotations

import pytest

from claude_on_the_fly.tui import render
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


class TestReadNewLines:
    """Powers the dashboard's live-tail panes at 1Hz, so it re-reads only the
    appended bytes rather than the last N lines every tick."""

    def test_attaching_skips_the_backlog(self, tmp_path):
        """The full file stays available in the [l] screen, so replaying it into the
        pane on attach would just duplicate it."""
        path = tmp_path / "slack.log"
        path.write_text("old line\n")
        lines, offset = render.read_new_lines(path, None)
        assert lines == []
        assert offset == path.stat().st_size

    def test_appended_lines_come_back_once(self, tmp_path):
        path = tmp_path / "slack.log"
        path.write_text("first\n")
        _lines, offset = render.read_new_lines(path, None)
        path.write_text("first\nsecond\n")
        lines, offset = render.read_new_lines(path, offset)
        assert lines == ["second"]
        # Nothing new the second time round.
        assert render.read_new_lines(path, offset) == ([], offset)

    def test_a_partial_line_is_left_for_the_next_tick(self, tmp_path):
        """The writer is mid-write, so consuming it would split a log line across two
        pane rows."""
        path = tmp_path / "slack.log"
        path.write_text("complete\npartial-no-newline")
        lines, offset = render.read_new_lines(path, 0)
        assert lines == ["complete"]
        assert offset == len("complete\n")
        path.write_text("complete\npartial-no-newline-now-finished\n")
        lines, _offset = render.read_new_lines(path, offset)
        assert lines == ["partial-no-newline-now-finished"]

    def test_no_newline_at_all_yields_nothing(self, tmp_path):
        path = tmp_path / "slack.log"
        path.write_text("still writing")
        assert render.read_new_lines(path, 0) == ([], 0)

    def test_a_rotated_file_is_re_read_from_the_start(self, tmp_path):
        """The daily rollover replaces the file, and keeping the old offset would skip
        everything the new one has written so far."""
        path = tmp_path / "slack.log"
        path.write_text("a much longer previous file\n")
        _lines, offset = render.read_new_lines(path, 0)
        path.write_text("new\n")
        lines, new_offset = render.read_new_lines(path, offset)
        assert lines == ["new"]
        assert new_offset == len("new\n")

    def test_an_empty_file_at_the_current_offset_yields_nothing(self, tmp_path):
        path = tmp_path / "slack.log"
        path.write_text("")
        assert render.read_new_lines(path, 0) == ([], 0)

    def test_a_truncated_file_that_is_now_empty_yields_nothing(self, tmp_path):
        path = tmp_path / "slack.log"
        path.write_text("something\n")
        _lines, offset = render.read_new_lines(path, 0)
        path.write_text("")
        assert render.read_new_lines(path, offset) == ([], 0)

    def test_a_file_that_cannot_be_read_keeps_the_offset(self, tmp_path):
        """A missing file is what a rotation looks like mid-tick, and losing the
        offset would replay the whole next file into the pane."""
        assert render.read_new_lines(tmp_path / "gone.log", 512) == ([], 512)

    def test_a_missing_file_with_no_offset_yields_zero(self, tmp_path):
        assert render.read_new_lines(tmp_path / "gone.log", None) == ([], 0)


class TestNextFireLabel:
    """Rendered in the cron table, so the unit has to match the distance or the
    operator cannot tell 90 seconds from 90 hours at a glance."""

    def test_a_fire_in_the_past_reads_as_now(self):
        from datetime import datetime, timedelta

        now = datetime(2026, 7, 30, 12, 0, 0)
        assert "(now)" in render._fmt_next_fire(now - timedelta(seconds=5), now)

    def test_seconds_away(self):
        from datetime import datetime, timedelta

        now = datetime(2026, 7, 30, 12, 0, 0)
        assert "(in 30s)" in render._fmt_next_fire(now + timedelta(seconds=30), now)

    def test_minutes_away(self):
        from datetime import datetime, timedelta

        now = datetime(2026, 7, 30, 12, 0, 0)
        assert "(in 30m)" in render._fmt_next_fire(now + timedelta(minutes=30), now)

    def test_hours_away(self):
        from datetime import datetime, timedelta

        now = datetime(2026, 7, 30, 12, 0, 0)
        assert "(in 5.0h)" in render._fmt_next_fire(now + timedelta(hours=5), now)

    def test_days_away(self):
        from datetime import datetime, timedelta

        now = datetime(2026, 7, 30, 12, 0, 0)
        assert "(in 3.0d)" in render._fmt_next_fire(now + timedelta(days=3), now)

    def test_the_wall_clock_time_is_always_shown(self):
        """The relative part answers "soon?"; the absolute answers "at what time?"."""
        from datetime import datetime, timedelta

        now = datetime(2026, 7, 30, 12, 0, 0)
        label = render._fmt_next_fire(now + timedelta(hours=4), now)
        assert "16:00" in label


class TestSnapshotJson:
    def test_an_unserialisable_value_raises_rather_than_being_guessed(self):
        """`claude-tui status --json` is read by scripts, so a silently coerced value
        (a repr, a null) is worse than a loud failure at the point it appears."""
        from datetime import UTC, datetime

        from claude_on_the_fly.tui.state import FrontendStatus, Snapshot

        snap = Snapshot(
            timestamp=datetime.now(UTC),
            frontends=[
                FrontendStatus(name="slack", state="running", extra={"x": object()})
            ],
            jobs=[],
            schedule_error=None,
            jobs_queue=None,
        )
        with pytest.raises(TypeError):
            render.render_snapshot_json(snap)

    def test_a_clean_snapshot_serialises(self):
        import json as _json
        from datetime import UTC, datetime

        from claude_on_the_fly.tui.state import Snapshot

        snap = Snapshot(
            timestamp=datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
            frontends=[],
            jobs=[],
            schedule_error=None,
            jobs_queue=None,
        )
        payload = _json.loads(render.render_snapshot_json(snap))
        assert payload["timestamp"] == "2026-07-30T12:00:00Z"
