"""Pure-function tests for the history screen's row formatter."""

from __future__ import annotations

from claude_on_the_fly.tui.screens.history import _event_source, _format_detail


class TestEventSource:
    def test_symphony_row_returns_symphony(self) -> None:
        assert _event_source({"source": "symphony"}) == "symphony"

    def test_chat_row_returns_frontend(self) -> None:
        assert _event_source({"source": "telegram"}) == "telegram"
        assert _event_source({"source": "slack"}) == "slack"
        assert _event_source({"source": "gmail"}) == "gmail"

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
