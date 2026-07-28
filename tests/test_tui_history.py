"""Pure-function tests for the history screen's row formatter."""

from __future__ import annotations

from datetime import UTC

from claude_on_the_fly.tui.screens.history import (
    _aggregate_by_job,
    _compute_runtimes,
    _event_source,
    _format_detail,
    _format_local_time,
    _format_runtime,
    _parse_ts,
)


class TestEventSource:
    def test_symphony_row_returns_symphony(self) -> None:
        assert _event_source({"source": "symphony"}) == "symphony"

    def test_chat_row_returns_frontend(self) -> None:
        assert _event_source({"source": "telegram"}) == "telegram"
        assert _event_source({"source": "slack"}) == "slack"
        assert _event_source({"source": "telegram"}) == "telegram"

    def test_legacy_tracker_in_source_field_maps_to_symphony(self) -> None:
        # Rows written before the source/tracker split stored the tracker
        # name in `source`. They should still surface under the symphony
        # filter.
        assert _event_source({"source": "jira"}) == "symphony"
        assert _event_source({"source": "github"}) == "symphony"

    def test_missing_source_returns_empty(self) -> None:
        assert _event_source({}) == ""


class TestFormatDetail:
    def test_symphony_dispatched_with_retry_attempt(self) -> None:
        e = {
            "type": "dispatched",
            "source": "symphony",
            "state": "In Progress",
            "failure_attempt": 2,
        }
        assert _format_detail(e) == "In Progress (retry 2)"

    def test_symphony_dispatched_without_retry(self) -> None:
        e = {"type": "dispatched", "source": "symphony", "state": "Backlog"}
        assert _format_detail(e) == "Backlog"

    def test_chat_dispatched_returns_blank(self) -> None:
        e = {"type": "dispatched", "source": "telegram"}
        assert _format_detail(e) == ""

    def test_chat_worker_done_shows_cost(self) -> None:
        e = {"type": "worker_done", "source": "telegram", "cost": 0.0234}
        assert _format_detail(e) == "cost=$0.0234"

    def test_symphony_worker_done_shows_reason_and_state(self) -> None:
        e = {
            "type": "worker_done",
            "source": "symphony",
            "reason": "terminal",
            "state": "Done",
        }
        assert _format_detail(e) == "terminal (Done)"

    def test_worker_failed_truncates_error(self) -> None:
        e = {"type": "worker_failed", "source": "telegram", "error": "x" * 200}
        assert len(_format_detail(e)) == 100

    def test_cancelled_with_state(self) -> None:
        e = {
            "type": "cancelled",
            "source": "symphony",
            "reason": "stall",
            "state": "In Progress",
        }
        assert _format_detail(e) == "stall (In Progress)"

    def test_retry_scheduled(self) -> None:
        e = {
            "type": "retry_scheduled",
            "source": "symphony",
            "kind": "failure",
            "attempt": 3,
        }
        assert _format_detail(e) == "failure attempt=3"

    def test_legacy_symphony_dispatched_with_jira_in_source(self) -> None:
        # Pre-split rows: source="jira", no tracker field, no source="symphony".
        # The formatter must still render the symphony state line.
        e = {"type": "dispatched", "source": "jira", "state": "In Progress"}
        assert _format_detail(e) == "In Progress"


class TestFormatRuntime:
    def test_none_renders_em_dash(self) -> None:
        assert _format_runtime(None) == "—"

    def test_sub_second_renders_zero(self) -> None:
        assert _format_runtime(0.4) == "0s"

    def test_seconds_only(self) -> None:
        assert _format_runtime(12.7) == "12s"

    def test_minutes_and_seconds(self) -> None:
        assert _format_runtime(83.0) == "1m23s"

    def test_hours_and_minutes(self) -> None:
        assert _format_runtime(3725.0) == "1h02m"


class TestFormatLocalTime:
    def test_utc_converts_to_system_local_time(self) -> None:
        from datetime import datetime

        ts = "2026-05-25T10:00:00Z"
        # Independent reference: same instant rendered in the local zone.
        expected = (
            datetime(2026, 5, 25, 10, 0, 0, tzinfo=UTC)
            .astimezone()
            .strftime("%H:%M:%S")
        )
        assert _format_local_time(ts) == expected

    def test_unparseable_falls_back_to_raw(self) -> None:
        assert _format_local_time("not-a-date") == "not-a-date"

    def test_empty_returns_empty(self) -> None:
        assert _format_local_time(None) == ""
        assert _format_local_time("") == ""


class TestParseTs:
    def test_iso_with_z_parses(self) -> None:
        # EventLog writes ISO-8601 with trailing 'Z'; both 3.11+ and the
        # older interpreters we still see must agree on a numeric value.
        result = _parse_ts("2026-05-25T10:00:00Z")
        assert result is not None
        assert result > 0

    def test_missing_returns_none(self) -> None:
        assert _parse_ts(None) is None
        assert _parse_ts("") is None
        assert _parse_ts(12345) is None

    def test_malformed_returns_none(self) -> None:
        assert _parse_ts("not-a-date") is None


class TestComputeRuntimes:
    def test_dispatch_row_is_zero(self) -> None:
        events = [
            {
                "ts": "2026-05-25T10:00:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
            },
        ]
        assert _compute_runtimes(events)[0] == 0.0

    def test_worker_done_pairs_with_latest_dispatch(self) -> None:
        events = [
            {
                "ts": "2026-05-25T10:00:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
            },
            {
                "ts": "2026-05-25T10:01:30Z",
                "type": "worker_done",
                "identifier": "X",
                "source": "symphony",
            },
        ]
        runtimes = _compute_runtimes(events)
        assert runtimes[0] == 0.0
        assert runtimes[1] == 90.0

    def test_retry_resets_anchor(self) -> None:
        """Second dispatch starts a fresh runtime — the done following it
        must measure from the retry, not from the original first attempt."""
        events = [
            {
                "ts": "2026-05-25T10:00:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
            },
            {
                "ts": "2026-05-25T10:00:30Z",
                "type": "worker_failed",
                "identifier": "X",
                "source": "symphony",
            },
            {
                "ts": "2026-05-25T10:05:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
            },
            {
                "ts": "2026-05-25T10:06:15Z",
                "type": "worker_done",
                "identifier": "X",
                "source": "symphony",
            },
        ]
        runtimes = _compute_runtimes(events)
        assert runtimes[3] == 75.0

    def test_orphan_row_without_prior_dispatch_returns_none(self) -> None:
        # Log was truncated above the dispatch — we can't compute runtime so
        # the row shows the em-dash placeholder.
        events = [
            {
                "ts": "2026-05-25T10:01:00Z",
                "type": "worker_done",
                "identifier": "X",
                "source": "symphony",
            },
        ]
        assert _compute_runtimes(events)[0] is None

    def test_two_jobs_dont_cross_pollinate(self) -> None:
        """Dispatch for job X must not pair with worker_done for job Y."""
        events = [
            {
                "ts": "2026-05-25T10:00:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
            },
            {
                "ts": "2026-05-25T10:00:30Z",
                "type": "worker_done",
                "identifier": "Y",
                "source": "symphony",
            },
            {
                "ts": "2026-05-25T10:01:00Z",
                "type": "worker_done",
                "identifier": "X",
                "source": "symphony",
            },
        ]
        runtimes = _compute_runtimes(events)
        assert runtimes[1] is None  # Y has no dispatch
        assert runtimes[2] == 60.0  # X paired with its own dispatch


class TestAggregateByJob:
    def test_collapses_repeated_dispatches_into_one_row(self) -> None:
        """Symphony retries explode the event log; the aggregated view must
        show one row per (identifier, source) with a runs counter."""
        # Newest-first input, like the screen feeds the aggregator.
        events = [
            {
                "ts": "2026-05-25T10:05:00Z",
                "type": "worker_failed",
                "identifier": "X",
                "source": "symphony",
                "backend": "claude:ollama:qwen",
            },
            {
                "ts": "2026-05-25T10:04:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
                "backend": "claude:ollama:qwen",
            },
            {
                "ts": "2026-05-25T10:02:00Z",
                "type": "worker_failed",
                "identifier": "X",
                "source": "symphony",
                "backend": "claude:ollama:qwen",
            },
            {
                "ts": "2026-05-25T10:00:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
                "backend": "claude:ollama:qwen",
            },
        ]
        rows = _aggregate_by_job(events)
        assert len(rows) == 1
        row = rows[0]
        assert row["identifier"] == "X"
        assert row["source"] == "symphony"
        assert row["runs"] == 2
        # last_event is newest — worker_failed at 10:05.
        assert row["last_event"]["type"] == "worker_failed"

    def test_runtime_measured_from_latest_dispatch(self) -> None:
        """Runtime is wall-clock since the most recent retry, not the
        original first dispatch."""
        events = [
            {
                "ts": "2026-05-25T10:05:30Z",
                "type": "worker_failed",
                "identifier": "X",
                "source": "symphony",
            },
            {
                "ts": "2026-05-25T10:04:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
            },  # latest retry
            {
                "ts": "2026-05-25T10:00:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
            },
        ]
        rows = _aggregate_by_job(events)
        assert rows[0]["runtime"] == 90.0  # 10:05:30 - 10:04:00

    def test_keeps_jobs_separated_by_source(self) -> None:
        """Same identifier under jira vs github stays distinct."""
        events = [
            {
                "ts": "2026-05-25T10:00:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
                "tracker": "jira",
            },
            {
                "ts": "2026-05-25T10:00:01Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "telegram",
            },
        ]
        rows = _aggregate_by_job(events)
        assert len(rows) == 2
        sources = {r["source"] for r in rows}
        assert sources == {"symphony", "telegram"}

    def test_no_dispatch_in_window_yields_none_runtime(self) -> None:
        events = [
            {
                "ts": "2026-05-25T10:05:00Z",
                "type": "worker_failed",
                "identifier": "X",
                "source": "symphony",
            },
        ]
        rows = _aggregate_by_job(events)
        assert rows[0]["runs"] == 0
        assert rows[0]["runtime"] is None

    def test_preserves_newest_first_order_across_jobs(self) -> None:
        """The job whose latest event is newest appears first — matches the
        natural reading order of the events list passed in."""
        events = [
            {
                "ts": "2026-05-25T10:05:00Z",
                "type": "worker_done",
                "identifier": "B",
                "source": "symphony",
            },
            {
                "ts": "2026-05-25T10:04:00Z",
                "type": "dispatched",
                "identifier": "B",
                "source": "symphony",
            },
            {
                "ts": "2026-05-25T10:00:00Z",
                "type": "worker_done",
                "identifier": "A",
                "source": "symphony",
            },
        ]
        rows = _aggregate_by_job(events)
        assert [r["identifier"] for r in rows] == ["B", "A"]

    def test_carries_backend_from_latest_event(self) -> None:
        """Backend column reflects whichever model produced the latest event
        for this job — so resume / takeover targets the right session."""
        events = [
            {
                "ts": "2026-05-25T10:05:00Z",
                "type": "worker_done",
                "identifier": "X",
                "source": "symphony",
                "backend": "claude:native:sonnet",
            },
            {
                "ts": "2026-05-25T10:04:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
                "backend": "claude:native:sonnet",
            },
            {
                "ts": "2026-05-25T10:00:00Z",
                "type": "dispatched",
                "identifier": "X",
                "source": "symphony",
                "backend": "claude:ollama:qwen",
            },
        ]
        rows = _aggregate_by_job(events)
        assert rows[0]["backend"] == "claude:native:sonnet"
        assert rows[0]["runs"] == 2


class TestRowUrl:
    def _screen(self):
        from claude_on_the_fly.tui.screens.history import HistoryScreen

        s = HistoryScreen()
        s._jira_base_urls_cache = {"jira": "https://hardcoretech.atlassian.net"}
        return s

    def test_github_pr_url(self) -> None:
        s = self._screen()
        assert (
            s._row_url("hardcoretech/fms#42", "github")
            == "https://github.com/hardcoretech/fms/pull/42"
        )

    def test_github_detected_by_shape_regardless_of_tracker_name(self) -> None:
        s = self._screen()
        assert (
            s._row_url("owner/repo#7", "github-fms")
            == "https://github.com/owner/repo/pull/7"
        )

    def test_jira_browse_url(self) -> None:
        s = self._screen()
        assert (
            s._row_url("ACES-123", "jira")
            == "https://hardcoretech.atlassian.net/browse/ACES-123"
        )

    def test_no_url_for_unknown_shape(self) -> None:
        s = self._screen()
        assert s._row_url("owner/repo", "github") is None  # no #N
        assert s._row_url("bare", "jira") is None  # no -KEY
        assert (
            s._row_url("ACES-1", "github") is None
        )  # github tracker, no base_url path
