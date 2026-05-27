"""Tests for symphony orchestrator: pure functions and async scheduling logic."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.symphony.config import SymphonyConfig, TrackerConfig
from claude_on_the_fly.events import EventLog
from claude_on_the_fly.symphony.orchestrator import (
    _check_and_cancel_stall,
    _dispatch,
    _eligible,
    _has_per_state_capacity,
    _heartbeat_extra,
    _log_config_summary,
    _process_due_retries,
    _run_worker,
    _select_candidates,
    _sort_key,
    reconcile,
    run_loop,
    startup_cleanup,
    tick,
)
from claude_on_the_fly.symphony.retry import RetryEntry, RetryQueue
from claude_on_the_fly.symphony.state import OrchestratorState, RunningEntry
from claude_on_the_fly.symphony.tracker import Tracker
from claude_on_the_fly.symphony.tracker.issue import (
    BlockerRef,
    Issue,
    IssueSummary,
    make_key,
)


def _summaries(
    d: dict[str, str], *, matches_jql: bool = True, terminal: bool = False
) -> dict[str, IssueSummary]:
    """Helper: turn `{key: state}` into reconcile summaries under the new
    JQL-membership model. `matches_jql=True` (default) → active (keep
    running); `matches_jql=False` → left the queue (park). `terminal=True`
    simulates GitHub's closed/merged (worker cancelled + workspace removed)."""
    return {
        k: IssueSummary(
            state=v, extra={"matches_jql": matches_jql, "terminal": terminal}
        )
        for k, v in d.items()
    }


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _issue(**overrides: object) -> Issue:
    defaults = {
        "id": "10042",
        "identifier": "PROJ-1",
        "title": "Fix login bug",
        "state": "In Progress",
        "description_raw": None,
        "priority": 3,
        "labels": (),
        "blocked_by": (),
        "parent_key": None,
        "url": "https://jira.example.com/browse/PROJ-1",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-02T00:00:00",
        "source": "jira",
        "body_text": None,
    }
    return Issue(**(defaults | {k: v for k, v in overrides.items() if k in defaults}))  # type: ignore[arg-type]


_TRACKER_FIELDS = {
    "kind",
    "base_url",
    "project_key",
    "jql",
    "max_concurrent",
    "instruction",
    "max_concurrent_by_state",
}


def _tracker_cfg(**overrides: object) -> TrackerConfig:
    defaults = {
        "kind": "jira",
        "base_url": "https://jira.example.com",
        "project_key": "PROJ",
        "jql": 'status != "Done" AND assignee = currentUser()',
        "instruction": "_default",
        "max_concurrent_by_state": {},
    }
    kwargs = defaults | {k: v for k, v in overrides.items() if k in _TRACKER_FIELDS}
    return TrackerConfig(**kwargs)  # type: ignore[arg-type]


def _config(**overrides: object) -> SymphonyConfig:
    # Route tracker-shaped overrides into the JiraTrackerConfig; remaining
    # globals stay on SymphonyConfig. If `tracker=` is also passed, layer
    # any tracker-shaped overrides on top via dataclasses.replace().
    from dataclasses import replace as _dc_replace

    tracker_overrides = {k: v for k, v in overrides.items() if k in _TRACKER_FIELDS}
    # `max_concurrent` moved to the tracker config; tests still expect to
    # pass it as a global-shape kwarg, so route it to the tracker.
    if "max_concurrent" in overrides:
        tracker_overrides["max_concurrent"] = overrides["max_concurrent"]
    if "tracker" in overrides:
        tracker = overrides["tracker"]
        if tracker_overrides:
            tracker = _dc_replace(tracker, **tracker_overrides)  # type: ignore[arg-type]
    else:
        tracker = _tracker_cfg(**tracker_overrides)

    global_defaults = {
        "turn_timeout_ms": 60_000,
        "max_turns": 10,
        "stall_timeout_ms": 300_000,
        "max_no_progress_turns": 3,
        "polling_ms": 30_000,
        "max_retry_backoff_ms": 3600_000,
    }
    global_overrides = {k: v for k, v in overrides.items() if k in global_defaults}
    return SymphonyConfig(
        trackers={"jira": tracker},  # type: ignore[dict-item]
        **(global_defaults | global_overrides),
    )


def _mock_tracker() -> MagicMock:
    """Mock tracker behaving like Jira: is_active reads the `matches_jql`
    membership flag, is_terminal reads an explicit `terminal` flag. Tests
    override `is_terminal` / `is_active` / `issue_to_summary` as needed."""
    t = MagicMock()
    t.fetch_one = AsyncMock()
    t.fetch_summaries_by_keys = AsyncMock()
    t.fetch_candidates = AsyncMock()
    t.aclose = AsyncMock()

    def _is_terminal(summary, cfg):
        # New model: Jira is never terminal; GitHub-style terminal is flagged
        # explicitly via summary.extra["terminal"].
        return bool(summary.extra.get("terminal", False))

    def _is_active(summary, cfg):
        # Active = still matches the candidate JQL (membership flag set by
        # fetch_summaries_by_keys). Defaults active when unspecified.
        return bool(summary.extra.get("matches_jql", True))

    def _issue_to_summary(issue):
        from claude_on_the_fly.symphony.tracker.issue import IssueSummary

        return IssueSummary(state=issue.state, extra={"matches_jql": True})

    t.is_terminal = MagicMock(side_effect=_is_terminal)
    t.is_active = MagicMock(side_effect=_is_active)
    t.issue_to_summary = MagicMock(side_effect=_issue_to_summary)
    return t


def _trackers(t: MagicMock | None = None) -> dict[str, Tracker]:
    """Wrap a single mock tracker into a `{"jira": tracker}` dict matching
    SymphonyConfig.trackers. Convenience for tests that exercise the
    multi-source orchestrator code paths with one source."""
    return {"jira": t if t is not None else _mock_tracker()}


def _stub_event_log() -> MagicMock:
    """No-op event log for tests that don't assert on event emission.

    `spec=EventLog` so accidental misuse fails at test time, not at runtime
    inside the orchestrator."""
    return MagicMock(spec=EventLog)


def _real_event_log(tmp_path: Path) -> EventLog:
    """Real on-disk EventLog at tmp_path/events.jsonl, for tests that assert
    on emitted events via `event_log.tail()`."""
    return EventLog(tmp_path / "events.jsonl")


# ---------------------------------------------------------------------------
# _log_config_summary (no secrets — auth lives in acli)
# ---------------------------------------------------------------------------


class TestLogConfigSummary:
    def test_dumps_fields_and_notes_acli_auth(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as _logging

        cfg = _config(
            tracker=_tracker_cfg(
                base_url="https://j.example.com",
                project_key="PROJ",
                instruction="rnd",
            ),
            polling_ms=15000,
            max_concurrent=3,
        )
        with caplog.at_level(
            _logging.INFO, logger="claude_on_the_fly.symphony.orchestrator"
        ):
            _log_config_summary(cfg)

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "symphony config:" in text
        assert "[jira]" in text
        assert "https://j.example.com" in text
        assert "polling_ms          = 15000" in text
        # max_concurrent is now per-tracker, surfaced under the [jira] block.
        assert "[jira] max_concurrent  = 3" in text
        assert "instruction     = rnd" in text
        assert "PROJ" in text
        assert "auth            = acli" in text


# ---------------------------------------------------------------------------
# _heartbeat_extra
# ---------------------------------------------------------------------------


class TestHeartbeatExtra:
    def test_empty_state_yields_empty_list(self) -> None:
        state = OrchestratorState()
        extra = _heartbeat_extra(state, pending_tasks=set(), retry_queue=RetryQueue())
        assert extra["running"] == 0
        assert extra["pending_workers"] == 0
        assert extra["retry_queue"] == 0
        assert extra["running_tickets"] == []

    def test_running_ticket_summary(self) -> None:
        state = OrchestratorState()
        state.claim(_issue(id="9", identifier="PROJ-9", state="In Progress"))
        entry = state.get_running(make_key("jira", "9"))
        assert entry is not None
        # Fix started_at so uptime is deterministic.
        entry.started_at = 100.0
        entry.failure_attempt = 2

        with patch(
            "claude_on_the_fly.symphony.orchestrator.time.monotonic",
            return_value=160.0,
        ):
            extra = _heartbeat_extra(
                state, pending_tasks=set(), retry_queue=RetryQueue()
            )

        assert extra["running"] == 1
        tickets = extra["running_tickets"]
        assert len(tickets) == 1
        t = tickets[0]
        assert t["identifier"] == "PROJ-9"
        assert t["state"] == "In Progress"
        assert t["uptime_s"] == 60
        assert t["last_turn_end_age_s"] is None
        assert t["failure_attempt"] == 2

    def test_last_turn_end_age(self) -> None:
        state = OrchestratorState()
        state.claim(_issue(id="9", identifier="PROJ-9", state="In Progress"))
        entry = state.get_running(make_key("jira", "9"))
        assert entry is not None
        entry.started_at = 100.0
        entry.last_turn_end_at = 150.0

        with patch(
            "claude_on_the_fly.symphony.orchestrator.time.monotonic",
            return_value=160.0,
        ):
            extra = _heartbeat_extra(
                state, pending_tasks=set(), retry_queue=RetryQueue()
            )

        assert extra["running_tickets"][0]["last_turn_end_age_s"] == 10


# ---------------------------------------------------------------------------
# _eligible
# ---------------------------------------------------------------------------


class TestEligible:
    def test_already_claimed(self) -> None:
        issue = _issue(id="1")
        state = OrchestratorState()
        state.claim(issue)
        assert _eligible(issue, state, RetryQueue(), _tracker_cfg()) is False

    def test_in_retry_queue(self) -> None:
        issue = _issue(id="1")
        rq = RetryQueue()
        rq.schedule_continuation("1", "PROJ-1")
        assert _eligible(issue, OrchestratorState(), rq, _tracker_cfg()) is False

    def test_missing_fields(self) -> None:
        issue = _issue(id="", state="")
        assert (
            _eligible(issue, OrchestratorState(), RetryQueue(), _tracker_cfg()) is False
        )

    def test_state_no_longer_filtered(self) -> None:
        """_eligible no longer filters by state — the candidate fetch (Jira's
        JQL, GitHub's search) is authoritative. Any state that came back as a
        candidate qualifies; blocker-state gating was dropped too."""
        for state in ("Unknown", "Done", "To Do", "In Progress"):
            issue = _issue(state=state)
            assert (
                _eligible(issue, OrchestratorState(), RetryQueue(), _tracker_cfg())
                is True
            ), state

    def test_blocked_by_no_longer_gates(self) -> None:
        """The To-Do-with-unresolved-blockers heuristic was removed with the
        state lists — a blocked ticket is now eligible if the JQL returned it."""
        blocker = BlockerRef(key="PROJ-2", state="In Progress")
        issue = _issue(state="To Do", blocked_by=(blocker,))
        assert (
            _eligible(issue, OrchestratorState(), RetryQueue(), _tracker_cfg()) is True
        )

    def test_eligible(self) -> None:
        issue = _issue(state="In Progress")
        assert (
            _eligible(issue, OrchestratorState(), RetryQueue(), _tracker_cfg()) is True
        )

    def test_cursor_blocks_unchanged_ticket(self, tmp_path) -> None:
        """Phase 4: cursor-based gating. When cursor is newer than the
        ticket's updated_at, _eligible returns False so the daemon does
        not re-claim a ticket that hasn't been touched."""
        from claude_on_the_fly.symphony.cursor import CursorStore, TicketCursor

        store = CursorStore(tmp_path, "jira")
        store.save(
            TicketCursor(
                identifier="PROJ-1",
                last_job_done_time="2026-05-27T10:00:00+00:00",
            )
        )
        issue = _issue(
            identifier="PROJ-1",
            state="In Progress",
            updated_at="2026-05-27T09:00:00+00:00",  # older than cursor
        )
        assert (
            _eligible(
                issue,
                OrchestratorState(),
                RetryQueue(),
                _tracker_cfg(),
                cursor_store=store,
            )
            is False
        )

    def test_cursor_allows_newer_ticket(self, tmp_path) -> None:
        from claude_on_the_fly.symphony.cursor import CursorStore, TicketCursor

        store = CursorStore(tmp_path, "jira")
        store.save(
            TicketCursor(
                identifier="PROJ-1",
                last_job_done_time="2026-05-27T10:00:00+00:00",
            )
        )
        issue = _issue(
            identifier="PROJ-1",
            state="In Progress",
            updated_at="2026-05-27T11:00:00+00:00",  # newer than cursor
        )
        assert (
            _eligible(
                issue,
                OrchestratorState(),
                RetryQueue(),
                _tracker_cfg(),
                cursor_store=store,
            )
            is True
        )

    def test_cursor_first_run_allows_when_no_history(self, tmp_path) -> None:
        from claude_on_the_fly.symphony.cursor import CursorStore

        store = CursorStore(tmp_path, "jira")  # empty store
        issue = _issue(
            identifier="PROJ-1",
            state="In Progress",
            updated_at="2026-05-27T11:00:00+00:00",
        )
        assert (
            _eligible(
                issue,
                OrchestratorState(),
                RetryQueue(),
                _tracker_cfg(),
                cursor_store=store,
            )
            is True
        )


# ---------------------------------------------------------------------------
# _sort_key
# ---------------------------------------------------------------------------


class TestSortKey:
    def test_priority_primary(self) -> None:
        a = _issue(priority=1, identifier="A")
        b = _issue(priority=5, identifier="B")
        assert _sort_key(a) < _sort_key(b)

    def test_none_priority_becomes_9999(self) -> None:
        a = _issue(priority=9999, identifier="A")
        b = _issue(priority=None, identifier="B")
        # None priority becomes 9999, so they tie on priority
        assert _sort_key(a)[0] == 9999
        assert _sort_key(b)[0] == 9999

    def test_created_at_secondary(self) -> None:
        a = _issue(priority=3, created_at="2026-01-01T00:00:00", identifier="A")
        b = _issue(priority=3, created_at="2026-01-02T00:00:00", identifier="B")
        assert _sort_key(a) < _sort_key(b)

    def test_missing_created_at_defaults(self) -> None:
        a = _issue(priority=3, created_at=None, identifier="Z")
        assert _sort_key(a)[1] == "9999-99-99T99:99:99"

    def test_identifier_tiebreak(self) -> None:
        a = _issue(priority=3, created_at="2026-01-01T00:00:00", identifier="PROJ-A")
        b = _issue(priority=3, created_at="2026-01-01T00:00:00", identifier="PROJ-B")
        assert _sort_key(a) < _sort_key(b)


# ---------------------------------------------------------------------------
# _select_candidates
# ---------------------------------------------------------------------------


class TestSelectCandidates:
    def test_sorts_by_priority_no_state_filter(self) -> None:
        """No state filtering anymore (the candidate fetch is authoritative),
        so all three sort purely by priority asc."""
        cfg = _tracker_cfg()
        a = _issue(id="1", identifier="A", priority=5, state="In Progress")
        b = _issue(id="2", identifier="B", priority=1, state="In Progress")
        c = _issue(id="3", identifier="C", priority=3, state="Done")

        result = _select_candidates([a, b, c], OrchestratorState(), RetryQueue(), cfg)
        assert [i.identifier for i in result] == ["B", "C", "A"]

    def test_empty_returns_empty(self) -> None:
        result = _select_candidates(
            [], OrchestratorState(), RetryQueue(), _tracker_cfg()
        )
        assert result == []


# ---------------------------------------------------------------------------
# _has_per_state_capacity
# ---------------------------------------------------------------------------


class TestHasPerStateCapacity:
    def test_under_tracker_cap_when_no_per_state_cap(self) -> None:
        state = OrchestratorState()
        cfg = _config(max_concurrent=3)
        assert (
            _has_per_state_capacity(state, "jira", "In Progress", cfg.tracker) is True
        )

    def test_at_per_state_cap(self) -> None:
        config = _config(max_concurrent_by_state={"in progress": 1})
        state = OrchestratorState()
        issue = _issue(state="In Progress")
        state.claim(issue)
        assert (
            _has_per_state_capacity(state, "jira", "In Progress", config.tracker)
            is False
        )

    def test_under_per_state_cap(self) -> None:
        config = _config(max_concurrent_by_state={"in progress": 2})
        state = OrchestratorState()
        issue = _issue(state="In Progress")
        state.claim(issue)
        assert (
            _has_per_state_capacity(state, "jira", "In Progress", config.tracker)
            is True
        )

    def test_falls_back_to_tracker_max_concurrent(self) -> None:
        config = _config(max_concurrent=3, max_concurrent_by_state={})
        state = OrchestratorState()
        # Claim until tracker budget is full.
        for i in range(config.tracker.max_concurrent):
            state.claim(_issue(id=str(i), state="In Progress"))
        assert (
            _has_per_state_capacity(state, "jira", "In Progress", config.tracker)
            is False
        )

    def test_per_state_count_is_source_scoped(self) -> None:
        """A claim under tracker B's source does not exhaust tracker A's
        per-state cap."""
        config = _config(max_concurrent_by_state={"in progress": 1})
        state = OrchestratorState()
        # Claim once under a *different* source; jira's cap should still be open.
        gh_issue = _issue(id="g-1", state="In Progress")
        # _issue() defaults to source="jira"; build a github-source issue via replace.
        from dataclasses import replace as _replace

        state.claim(_replace(gh_issue, source="github"))
        assert (
            _has_per_state_capacity(state, "jira", "In Progress", config.tracker)
            is True
        )


# ---------------------------------------------------------------------------
# _check_and_cancel_stall
# ---------------------------------------------------------------------------


class TestCheckAndCancelStall:
    def test_stall_disabled(self) -> None:
        config = _config(stall_timeout_ms=0)
        entry = RunningEntry(
            issue_id="1", issue_identifier="P-1", issue_state="S", started_at=0.0
        )
        assert (
            _check_and_cancel_stall(
                entry, config, RetryQueue(), _stub_event_log(), 999_999.0
            )
            is False
        )

    def test_not_stalled(self) -> None:
        config = _config(stall_timeout_ms=10_000)
        entry = RunningEntry(
            issue_id="1", issue_identifier="P-1", issue_state="S", started_at=100.0
        )
        assert (
            _check_and_cancel_stall(
                entry, config, RetryQueue(), _stub_event_log(), 105.0
            )
            is False
        )

    def test_stalled_cancels_task_and_schedules_failure(self) -> None:
        config = _config(stall_timeout_ms=5_000)
        rq = RetryQueue()
        task = MagicMock()
        task.done.return_value = False
        entry = RunningEntry(
            issue_id="1",
            issue_identifier="P-1",
            issue_state="S",
            started_at=100.0,
            task=task,
        )
        result = _check_and_cancel_stall(entry, config, rq, _stub_event_log(), 110.0)
        assert result is True
        task.cancel.assert_called_once()
        assert rq.has(make_key("jira", "1")) is True

    def test_stalled_uses_last_turn_end(self) -> None:
        config = _config(stall_timeout_ms=5_000)
        rq = RetryQueue()
        task = MagicMock()
        task.done.return_value = False
        entry = RunningEntry(
            issue_id="1",
            issue_identifier="P-1",
            issue_state="S",
            started_at=100.0,
            last_turn_end_at=200.0,
            task=task,
        )
        # now = 210.0, 10s since last turn end -> stalled
        result = _check_and_cancel_stall(entry, config, rq, _stub_event_log(), 210.0)
        assert result is True

    def test_stalled_does_not_cancel_already_done(self) -> None:
        config = _config(stall_timeout_ms=5_000)
        rq = RetryQueue()
        task = MagicMock()
        task.done.return_value = True
        entry = RunningEntry(
            issue_id="1",
            issue_identifier="P-1",
            issue_state="S",
            started_at=100.0,
            task=task,
        )
        result = _check_and_cancel_stall(entry, config, rq, _stub_event_log(), 110.0)
        assert result is True
        task.cancel.assert_not_called()
        assert rq.has(make_key("jira", "1")) is True


# ---------------------------------------------------------------------------
# _dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    async def test_dispatch_claims_and_creates_task(self) -> None:
        issue = _issue()
        state = OrchestratorState()
        tracker = _mock_tracker()
        rq = RetryQueue()
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker"
        ) as mock_run_worker:
            mock_run_worker.return_value = None
            cfg = _config()
            _dispatch(
                issue,
                state,
                tracker,
                cfg.tracker,
                cfg,
                "prompt",
                rq,
                _stub_event_log(),
                pending,
            )

        assert state.is_claimed(issue.key) is True
        assert len(pending) == 1

    async def test_dispatch_raises_on_duplicate(self) -> None:
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        pending: set[asyncio.Task[None]] = set()

        cfg = _config()
        _dispatch(
            issue,
            state,
            tracker,
            cfg.tracker,
            cfg,
            "prompt",
            RetryQueue(),
            _stub_event_log(),
            pending,
        )
        # Should not raise; just silently returns
        assert len(pending) == 0


# ---------------------------------------------------------------------------
# _run_worker
# ---------------------------------------------------------------------------


class TestRunWorker:
    async def test_claude_unavailable_schedules_retry(self) -> None:
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=1)
        rq = RetryQueue()

        # Mock ClaudeUnavailableError from TicketRunner.run_turn
        from claude_on_the_fly.agent import ClaudeUnavailableError

        with (
            patch(
                "claude_on_the_fly.symphony.orchestrator.ensure_workspace"
            ) as mock_ew,
            patch(
                "claude_on_the_fly.symphony.orchestrator.TicketRunner"
            ) as mock_runner_cls,
        ):
            mock_ew.return_value = Path("/tmp/ws")
            mock_runner = MagicMock()
            mock_runner.run_turn = AsyncMock(
                side_effect=ClaudeUnavailableError("usage limit exceeded")
            )
            mock_runner_cls.return_value = mock_runner

            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                rq,
                _stub_event_log(),
            )

        assert rq.has(issue.key) is True
        assert state.is_claimed(issue.key) is False

    async def test_workspace_prep_failure(self) -> None:
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=1)
        rq = RetryQueue()

        with patch(
            "claude_on_the_fly.symphony.orchestrator.ensure_workspace",
            side_effect=OSError("disk full"),
        ):
            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                rq,
                _stub_event_log(),
            )

        assert rq.has(issue.key) is True
        assert state.is_claimed(issue.key) is False

    async def test_max_turns_reached_schedules_continuation(self) -> None:
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=2)
        rq = RetryQueue()

        with (
            patch(
                "claude_on_the_fly.symphony.orchestrator.ensure_workspace"
            ) as mock_ew,
            patch(
                "claude_on_the_fly.symphony.orchestrator.TicketRunner"
            ) as mock_runner_cls,
        ):
            mock_ew.return_value = Path("/tmp/ws")
            mock_runner = MagicMock()
            mock_runner.run_turn = AsyncMock(return_value=MagicMock(body="ok"))
            mock_runner_cls.return_value = mock_runner

            # Each turn — tracker.fetch_one returns active state so we loop
            tracker.fetch_one.return_value = _issue(state="In Progress")
            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                rq,
                _stub_event_log(),
                starting_failure_attempt=0,
            )

        assert rq.has(issue.key) is True

    async def test_no_progress_turns_stop_worker(self) -> None:
        """Turns that complete with zero tool use trip the no-progress guard
        after max_no_progress_turns in a row — the worker bails instead of
        churning to max_turns, and schedules a backoff retry."""
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=20, max_no_progress_turns=3)
        rq = RetryQueue()

        # Real Response (not a MagicMock) so the guard exercises the actual
        # `has_tools` property — a MagicMock makes it callable and would mask a
        # `has_tools()` vs `has_tools` mistake. Empty tool_counts → has_tools False.
        from claude_on_the_fly.agent import Response

        no_tool = Response(body="", model="<synthetic>")

        with (
            patch(
                "claude_on_the_fly.symphony.orchestrator.ensure_workspace"
            ) as mock_ew,
            patch(
                "claude_on_the_fly.symphony.orchestrator.TicketRunner"
            ) as mock_runner_cls,
        ):
            mock_ew.return_value = Path("/tmp/ws")
            mock_runner = MagicMock()
            mock_runner.run_turn = AsyncMock(return_value=no_tool)
            mock_runner_cls.return_value = mock_runner
            # Stay active so absent the guard the worker would run to max_turns.
            tracker.fetch_summaries_by_keys.return_value = _summaries(
                {issue.identifier: "In Progress"}
            )
            tracker.fetch_one.return_value = _issue(state="In Progress")
            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                rq,
                _stub_event_log(),
            )

        assert mock_runner.run_turn.await_count == 3
        assert rq.has(issue.key) is True

    async def test_terminal_state_after_turn(self) -> None:
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=10)
        rq = RetryQueue()

        with (
            patch(
                "claude_on_the_fly.symphony.orchestrator.ensure_workspace"
            ) as mock_ew,
            patch(
                "claude_on_the_fly.symphony.orchestrator.TicketRunner"
            ) as mock_runner_cls,
            patch(
                "claude_on_the_fly.symphony.orchestrator.remove_workspace"
            ) as mock_rm,
        ):
            mock_ew.return_value = Path("/tmp/ws")
            mock_runner = MagicMock()
            mock_runner.run_turn = AsyncMock(return_value=MagicMock(body="ok"))
            mock_runner_cls.return_value = mock_runner

            # Post-turn decision now comes from fetch_summaries_by_keys.
            # terminal=True simulates a GitHub-style closed/merged PR.
            tracker.fetch_summaries_by_keys.return_value = _summaries(
                {"PROJ-1": "Done"}, terminal=True
            )
            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                rq,
                _stub_event_log(),
            )

        mock_rm.assert_called_once()

    async def test_missing_summary_after_turn_reschedules_keeps_workspace(self) -> None:
        """A post-turn state gap (key omitted, e.g. a transient `gh pr view`
        blip) is NOT terminal per the tracker Protocol: keep the workspace and
        schedule a retry rather than deleting work over a transient failure."""
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=10)
        rq = RetryQueue()

        with (
            patch(
                "claude_on_the_fly.symphony.orchestrator.ensure_workspace"
            ) as mock_ew,
            patch(
                "claude_on_the_fly.symphony.orchestrator.TicketRunner"
            ) as mock_runner_cls,
            patch(
                "claude_on_the_fly.symphony.orchestrator.remove_workspace"
            ) as mock_rm,
        ):
            mock_ew.return_value = Path("/tmp/ws")
            mock_runner = MagicMock()
            mock_runner.run_turn = AsyncMock(return_value=MagicMock(body="ok"))
            mock_runner_cls.return_value = mock_runner
            # Key omitted from the post-turn snapshot → summary is None.
            tracker.fetch_summaries_by_keys.return_value = {}
            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                rq,
                _stub_event_log(),
            )

        mock_rm.assert_not_called()  # workspace kept (transient, not terminal)
        assert rq.has(issue.key) is True  # rescheduled

    async def test_inactive_state_after_turn(self) -> None:
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=10)
        rq = RetryQueue()

        with (
            patch(
                "claude_on_the_fly.symphony.orchestrator.ensure_workspace"
            ) as mock_ew,
            patch(
                "claude_on_the_fly.symphony.orchestrator.TicketRunner"
            ) as mock_runner_cls,
        ):
            mock_ew.return_value = Path("/tmp/ws")
            mock_runner = MagicMock()
            mock_runner.run_turn = AsyncMock(return_value=MagicMock(body="ok"))
            mock_runner_cls.return_value = mock_runner

            # Ticket left the JQL after the turn → parked (inactive).
            tracker.fetch_summaries_by_keys.return_value = _summaries(
                {"PROJ-1": "Backlog"}, matches_jql=False
            )
            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                rq,
                _stub_event_log(),
            )

        assert state.is_claimed(issue.key) is False

    async def test_worker_crash_schedules_failure(self) -> None:
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=10)
        rq = RetryQueue()

        with patch(
            "claude_on_the_fly.symphony.orchestrator.ensure_workspace",
            side_effect=RuntimeError("boom"),
        ):
            # This actually falls into workspace prep failure path
            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                rq,
                _stub_event_log(),
            )

        assert rq.has(issue.key) is True

    async def test_leaves_jql_between_turns_exits(self) -> None:
        """Agent parks itself by moving the ticket out of the candidate JQL
        (e.g. transitioning to a human-review status) — worker must stop."""
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=10)  # plenty of headroom
        rq = RetryQueue()

        with (
            patch(
                "claude_on_the_fly.symphony.orchestrator.ensure_workspace"
            ) as mock_ew,
            patch(
                "claude_on_the_fly.symphony.orchestrator.TicketRunner"
            ) as mock_runner_cls,
        ):
            mock_ew.return_value = Path("/tmp/ws")
            mock_runner = MagicMock()
            mock_runner.run_turn = AsyncMock(return_value=MagicMock(body="ok"))
            mock_runner_cls.return_value = mock_runner
            # Post-turn membership check shows the ticket left the JQL.
            tracker.fetch_summaries_by_keys.return_value = _summaries(
                {"PROJ-1": "In Review"}, matches_jql=False
            )
            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                rq,
                _stub_event_log(),
            )

        # Exactly one turn ran, no retry scheduled (clean park).
        assert mock_runner.run_turn.await_count == 1
        assert rq.has(issue.key) is False


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


class TestReconcile:
    async def test_no_running_workers_returns_early(self) -> None:
        state = OrchestratorState()
        tracker = _mock_tracker()
        await reconcile(
            state, _trackers(tracker), _config(), RetryQueue(), _stub_event_log()
        )
        tracker.fetch_summaries_by_keys.assert_not_called()

    async def test_fetch_failure_logs_and_returns(self) -> None:
        state = OrchestratorState()
        issue = _issue()
        state.claim(issue)
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.side_effect = ConnectionError("down")
        await reconcile(
            state,
            _trackers(tracker),
            _config(stall_timeout_ms=0),
            RetryQueue(),
            _stub_event_log(),
        )
        tracker.fetch_summaries_by_keys.assert_awaited_once()

    async def test_terminal_mid_run_cancels_and_removes_workspace(self) -> None:
        state = OrchestratorState()
        issue = _issue()
        entry = state.claim(issue)
        entry.workspace = Path("/tmp/ws-PROJ-1")
        task = MagicMock()
        task.done.return_value = False
        entry.task = task
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        # terminal=True simulates a GitHub closed/merged PR.
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "Done"}, terminal=True
        )

        with patch(
            "claude_on_the_fly.symphony.orchestrator.remove_workspace"
        ) as mock_rm:
            await reconcile(
                state, _trackers(tracker), cfg, RetryQueue(), _stub_event_log()
            )

        mock_rm.assert_called_once()
        task.cancel.assert_called_once()

    async def test_inactive_mid_run_cancels(self) -> None:
        state = OrchestratorState()
        issue = _issue()
        entry = state.claim(issue)
        task = MagicMock()
        task.done.return_value = False
        entry.task = task
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        # Ticket left the JQL mid-run → park (cancel, keep workspace).
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "Backlog"}, matches_jql=False
        )

        await reconcile(state, _trackers(tracker), cfg, RetryQueue(), _stub_event_log())
        task.cancel.assert_called_once()

    async def test_state_update_when_changed_and_still_active(self) -> None:
        state = OrchestratorState()
        issue = _issue(state="To Do")
        entry = state.claim(issue)
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}
        )

        await reconcile(state, _trackers(tracker), cfg, RetryQueue(), _stub_event_log())
        assert entry.issue_state == "In Progress"

    async def test_left_jql_mid_run_cancels(self) -> None:
        """Ticket still has a live status but no longer matches the candidate
        JQL (e.g. reassigned, or moved to a status the JQL excludes) — worker
        is cancelled, workspace kept."""
        state = OrchestratorState()
        issue = _issue()
        entry = state.claim(issue)
        task = MagicMock()
        task.done.return_value = False
        entry.task = task
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}, matches_jql=False
        )

        await reconcile(state, _trackers(tracker), cfg, RetryQueue(), _stub_event_log())
        task.cancel.assert_called_once()

    async def test_still_matching_jql_does_not_cancel(self) -> None:
        """Sanity check: a ticket still matching the JQL keeps running."""
        state = OrchestratorState()
        issue = _issue()
        entry = state.claim(issue)
        task = MagicMock()
        task.done.return_value = False
        entry.task = task
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}
        )

        await reconcile(state, _trackers(tracker), cfg, RetryQueue(), _stub_event_log())
        task.cancel.assert_not_called()


# ---------------------------------------------------------------------------
# startup_cleanup
# ---------------------------------------------------------------------------


class TestStartupCleanup:
    def test_root_not_exists(self, tmp_path: Path) -> None:
        tracker = _mock_tracker()
        root = tmp_path / "nonexistent"
        # Should not raise
        import asyncio as _asyncio

        async def _run() -> None:
            await startup_cleanup(root, _trackers(tracker), _config())

        _asyncio.run(_run())
        tracker.fetch_summaries_by_keys.assert_not_called()

    def test_no_dirs(self, tmp_path: Path) -> None:
        tracker = _mock_tracker()
        root = tmp_path / "empty"
        root.mkdir()

        async def _run() -> None:
            await startup_cleanup(root, _trackers(tracker), _config())

        asyncio.run(_run())
        tracker.fetch_summaries_by_keys.assert_not_called()

    def test_removes_terminal_dirs(self, tmp_path: Path) -> None:
        tracker = _mock_tracker()
        root = tmp_path / "worktrees"
        # New per-source layout: cleanup walks `root / <source>`.
        (root / "jira").mkdir(parents=True)
        d = root / "jira" / "PROJ-1"
        d.mkdir()
        # Ticket no longer matches the JQL (done / reassigned) → GC the dir.
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "Done"}, matches_jql=False
        )

        async def _run() -> None:
            await startup_cleanup(root, _trackers(tracker), _config())

        asyncio.run(_run())
        assert not d.exists()

    def test_leaves_non_terminal_dirs(self, tmp_path: Path) -> None:
        tracker = _mock_tracker()
        root = tmp_path / "worktrees"
        (root / "jira").mkdir(parents=True)
        d = root / "jira" / "PROJ-2"
        d.mkdir()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-2": "In Progress"}
        )

        async def _run() -> None:
            await startup_cleanup(root, _trackers(tracker), _config())

        asyncio.run(_run())
        assert d.exists()

    def test_fetch_failure_logs_and_skips(self, tmp_path: Path) -> None:
        tracker = _mock_tracker()
        root = tmp_path / "worktrees"
        (root / "jira").mkdir(parents=True)
        (root / "jira" / "PROJ-1").mkdir()
        tracker.fetch_summaries_by_keys.side_effect = ConnectionError("down")

        async def _run() -> None:
            await startup_cleanup(root, _trackers(tracker), _config())

        asyncio.run(_run())
        # DIR not removed because fetch failed
        assert (root / "jira" / "PROJ-1").exists()


# ---------------------------------------------------------------------------
# _process_due_retries
# ---------------------------------------------------------------------------


class TestProcessDueRetries:
    async def test_empty_due_returns_early(self) -> None:
        rq = RetryQueue()
        tracker = _mock_tracker()
        pending: set[asyncio.Task[None]] = set()
        await _process_due_retries(
            OrchestratorState(),
            _trackers(tracker),
            _config(),
            {"jira": "prompt"},
            rq,
            _stub_event_log(),
            pending,
        )
        tracker.fetch_summaries_by_keys.assert_not_called()

    async def test_terminal_state_dropped(self) -> None:
        cfg = _config()
        rq = RetryQueue()
        # Pretend attempt=2 so delay is computed from failure_delay_ms
        rq.schedule_failure(
            "1", "PROJ-1", cfg.max_retry_backoff_ms, attempt=2, error="test"
        )
        # Override due_at so it's "due now"
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=2, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries({"PROJ-1": "Done"})
        pending: set[asyncio.Task[None]] = set()

        await _process_due_retries(
            OrchestratorState(),
            _trackers(tracker),
            cfg,
            {"jira": "prompt"},
            rq,
            _stub_event_log(),
            pending,
        )
        assert rq.has(make_key("jira", "1")) is False  # dropped, not requeued

    async def test_inactive_state_dropped(self) -> None:
        cfg = _config()
        rq = RetryQueue()
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries({"PROJ-1": "Backlog"})
        pending: set[asyncio.Task[None]] = set()

        await _process_due_retries(
            OrchestratorState(),
            _trackers(tracker),
            cfg,
            {"jira": "prompt"},
            rq,
            _stub_event_log(),
            pending,
        )
        assert rq.has(make_key("jira", "1")) is False

    async def test_no_global_slots_requeues(self) -> None:
        cfg = _config(max_concurrent=2)
        state = OrchestratorState()
        state.claim(_issue(id="99", state="In Progress"))
        state.claim(_issue(id="100", state="In Progress"))
        rq = RetryQueue()
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}
        )
        pending: set[asyncio.Task[None]] = set()

        await _process_due_retries(
            state,
            _trackers(tracker),
            cfg,
            {"jira": "prompt"},
            rq,
            _stub_event_log(),
            pending,
        )
        assert rq.has(make_key("jira", "1")) is True  # requeued

    async def test_dispatches_when_slots_available(self) -> None:
        cfg = _config(max_concurrent=3)
        rq = RetryQueue()
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}
        )
        tracker.fetch_one.return_value = _issue()
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ):
            await _process_due_retries(
                OrchestratorState(),
                _trackers(tracker),
                cfg,
                {"jira": "prompt"},
                rq,
                _stub_event_log(),
                pending,
            )

        tracker.fetch_one.assert_awaited_once_with("PROJ-1")

    async def test_fetch_one_failure_requeues(self) -> None:
        cfg = _config()
        rq = RetryQueue()
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=2, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}
        )
        tracker.fetch_one.side_effect = ConnectionError("down")
        pending: set[asyncio.Task[None]] = set()

        await _process_due_retries(
            OrchestratorState(),
            _trackers(tracker),
            cfg,
            {"jira": "prompt"},
            rq,
            _stub_event_log(),
            pending,
        )
        assert rq.has(make_key("jira", "1")) is True  # requeued

    async def test_left_jql_drops(self) -> None:
        # Agent parked the ticket by moving it out of the candidate JQL. The
        # retry path must drop it rather than re-dispatch, honoring the pause.
        cfg = _config()
        rq = RetryQueue()
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}, matches_jql=False
        )
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ):
            await _process_due_retries(
                OrchestratorState(),
                _trackers(tracker),
                cfg,
                {"jira": "prompt"},
                rq,
                _stub_event_log(),
                pending,
            )

        assert rq.has(make_key("jira", "1")) is False  # dropped, not requeued
        # _dispatch was NOT called: no worker task was created
        assert len(pending) == 0

    async def test_matching_retry_dispatches(self) -> None:
        cfg = _config()
        rq = RetryQueue()
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        # Still matches the jql → eligible for re-dispatch.
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}
        )
        tracker.fetch_one.return_value = _issue()
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ):
            await _process_due_retries(
                OrchestratorState(),
                _trackers(tracker),
                cfg,
                {"jira": "prompt"},
                rq,
                _stub_event_log(),
                pending,
            )

        tracker.fetch_one.assert_awaited_once_with("PROJ-1")

    async def test_retry_dispatches_even_when_issue_to_summary_inactive(self) -> None:
        """Regression: the retry path must NOT re-derive activeness from
        issue_to_summary. Real Jira's issue_to_summary omits matches_jql, so
        is_active(issue_to_summary(issue)) is always False — which used to drop
        every Jira retry. The authoritative fetch_summaries_by_keys gate above
        already validated it, so the dispatch must still happen."""
        cfg = _config()
        rq = RetryQueue()
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}
        )
        tracker.fetch_one.return_value = _issue()
        # Mimic real Jira: projection carries no matches_jql → is_active False.
        tracker.issue_to_summary.side_effect = lambda issue: IssueSummary(
            state=issue.state, extra={}
        )
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ) as mock_rw:
            await _process_due_retries(
                OrchestratorState(),
                _trackers(tracker),
                cfg,
                {"jira": "prompt"},
                rq,
                _stub_event_log(),
                pending,
            )

        mock_rw.assert_called_once()  # dispatched, not dropped by a broken re-check


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


class TestTick:
    async def test_full_capacity_skips_dispatch(self) -> None:
        cfg = _config(max_concurrent=2)
        state = OrchestratorState()
        state.claim(_issue(id="1", state="In Progress"))
        state.claim(_issue(id="2", state="In Progress"))
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = {}
        pending: set[asyncio.Task[None]] = set()

        await tick(
            state,
            cfg,
            {"jira": "prompt"},
            _trackers(tracker),
            RetryQueue(),
            _stub_event_log(),
            pending,
        )
        tracker.fetch_candidates.assert_not_called()

    async def test_dispatches_candidates(self) -> None:
        cfg = _config(max_concurrent=3)
        state = OrchestratorState()
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = {}
        tracker.fetch_candidates.return_value = [
            _issue(id="1", identifier="PROJ-1", state="In Progress", priority=1),
        ]
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ):
            await tick(
                state,
                cfg,
                {"jira": "prompt"},
                _trackers(tracker),
                RetryQueue(),
                _stub_event_log(),
                pending,
            )

        tracker.fetch_candidates.assert_awaited_once()

    async def test_fetch_candidates_failure(self) -> None:
        cfg = _config()
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = {}
        tracker.fetch_candidates.side_effect = ConnectionError("down")
        pending: set[asyncio.Task[None]] = set()

        # Should not raise
        await tick(
            OrchestratorState(),
            cfg,
            {"jira": "prompt"},
            _trackers(tracker),
            RetryQueue(),
            _stub_event_log(),
            pending,
        )

    async def test_multi_source_dispatch_honors_per_tracker_caps(self) -> None:
        """Two trackers each with their own concurrency budget. Orchestrator
        merges candidates, sorts globally, and respects each tracker's cap
        independently. With jira.max_concurrent=1 + github.max_concurrent=1
        we can dispatch 2 total, one per source."""
        from dataclasses import replace as _dc_replace

        # Build a two-source config (jira + github), each capped at 1.
        jira_cfg = _tracker_cfg(kind="jira", max_concurrent=1)
        gh_cfg = _tracker_cfg(
            kind="github",
            active_states=("open",),
            terminal_states=("closed", "merged"),
            max_concurrent=1,
        )
        # SymphonyConfig: pass `trackers` directly via dataclasses.replace to
        # bypass the test helper's single-source default.
        base_cfg = _config()
        cfg = _dc_replace(base_cfg, trackers={"jira": jira_cfg, "github": gh_cfg})

        jira_tracker = _mock_tracker()
        gh_tracker = _mock_tracker()
        jira_tracker.fetch_summaries_by_keys.return_value = {}
        gh_tracker.fetch_summaries_by_keys.return_value = {}
        jira_tracker.fetch_candidates.return_value = [
            _issue(
                id="1",
                identifier="PROJ-1",
                state="In Progress",
                priority=1,
                labels=(),
                source="jira",
            ),
            _issue(
                id="2",
                identifier="PROJ-2",
                state="In Progress",
                priority=3,
                labels=(),
                source="jira",
            ),
        ]
        gh_tracker.fetch_candidates.return_value = [
            _issue(
                id="100",
                identifier="owner/repo#100",
                state="open",
                priority=2,
                labels=(),
                source="github",
            ),
        ]
        trackers = {"jira": jira_tracker, "github": gh_tracker}
        state = OrchestratorState()
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ):
            await tick(
                state,
                cfg,
                {"jira": "jira-prompt", "github": "gh-prompt"},
                trackers,
                RetryQueue(),
                _stub_event_log(),
                pending,
            )

        # Both trackers were polled.
        jira_tracker.fetch_candidates.assert_awaited_once()
        gh_tracker.fetch_candidates.assert_awaited_once()
        # Per-tracker caps of 1 each — total 2 running.
        assert state.running_count() == 2
        assert state.running_by_source("jira") == 1
        assert state.running_by_source("github") == 1
        # PROJ-1 (jira, prio=1) wins jira's slot over PROJ-2 (prio=3).
        # owner/repo#100 takes github's slot.
        running_ids = {e.issue_identifier for e in state.all_running()}
        assert "PROJ-1" in running_ids
        assert "owner/repo#100" in running_ids
        assert "PROJ-2" not in running_ids


# ---------------------------------------------------------------------------
# Heartbeat extra includes source
# ---------------------------------------------------------------------------


class TestMultiSourceReconcile:
    async def test_each_source_uses_its_own_tracker_for_predicates(self) -> None:
        """Two workers running, one Jira and one GitHub. Reconcile fetches
        summaries from each tracker independently and applies that tracker's
        is_terminal/is_active predicates — the orchestrator stays agnostic."""
        from dataclasses import replace as _dc_replace

        jira_cfg = _tracker_cfg(kind="jira")
        gh_cfg = _tracker_cfg(kind="github")
        base_cfg = _config()
        cfg = _dc_replace(base_cfg, trackers={"jira": jira_cfg, "github": gh_cfg})

        jira_tracker = _mock_tracker()
        gh_tracker = _mock_tracker()

        # GitHub-specific predicates use hardcoded lifecycle constants — no
        # config state lists. terminal = closed/merged; active = open AND not
        # already reviewed at head.
        def gh_is_terminal(summary, c):
            return summary.state in ("closed", "merged")

        def gh_is_active(summary, c):
            return summary.state == "open" and not bool(
                summary.extra.get("user_has_reviewed")
            )

        gh_tracker.is_terminal = MagicMock(side_effect=gh_is_terminal)
        gh_tracker.is_active = MagicMock(side_effect=gh_is_active)

        # Two running workers.
        state = OrchestratorState()
        jira_issue = _issue(
            id="1", identifier="PROJ-1", state="In Progress", source="jira"
        )
        gh_issue = _issue(
            id="200",
            identifier="owner/repo#200",
            state="open",
            source="github",
        )
        state.claim(jira_issue)
        state.claim(gh_issue)

        # Each tracker returns its own summary. Jira is still active
        # (predicate returns True); GitHub PR's reviewRequests no longer
        # contains @me, so its predicate returns False — worker should be
        # cancelled but workspace left.
        jira_tracker.fetch_summaries_by_keys.return_value = {
            "PROJ-1": IssueSummary(state="In Progress", extra={"matches_jql": True}),
        }
        gh_tracker.fetch_summaries_by_keys.return_value = {
            "owner/repo#200": IssueSummary(
                state="open", extra={"user_has_reviewed": True}
            ),
        }

        # Attach tasks so reconcile has something to cancel.
        jira_task = MagicMock()
        jira_task.done.return_value = False
        gh_task = MagicMock()
        gh_task.done.return_value = False
        jira_entry = state.get_running(jira_issue.key)
        gh_entry = state.get_running(gh_issue.key)
        assert jira_entry is not None and gh_entry is not None
        jira_entry.task = jira_task
        gh_entry.task = gh_task

        await reconcile(
            state,
            {"jira": jira_tracker, "github": gh_tracker},
            cfg,
            RetryQueue(),
            _stub_event_log(),
        )

        # Jira stayed running, GitHub got cancelled.
        jira_task.cancel.assert_not_called()
        gh_task.cancel.assert_called_once()
        # Each tracker's summaries fetch was called exactly once, with its own
        # keys and its own config (cfg is now passed for jql membership).
        jira_tracker.fetch_summaries_by_keys.assert_awaited_once_with(
            ["PROJ-1"], jira_cfg
        )
        gh_tracker.fetch_summaries_by_keys.assert_awaited_once_with(
            ["owner/repo#200"], gh_cfg
        )


class TestHeartbeatSource:
    def test_running_ticket_carries_source(self) -> None:
        state = OrchestratorState()
        from claude_on_the_fly.symphony.tracker.issue import Issue as _Issue

        gh_issue = _Issue(
            id="99",
            identifier="owner/repo#99",
            title="t",
            state="open",
            description_raw=None,
            priority=None,
            labels=(),
            blocked_by=(),
            parent_key=None,
            url="",
            created_at=None,
            updated_at=None,
            source="github",
        )
        state.claim(gh_issue)
        extra = _heartbeat_extra(state, set(), RetryQueue())
        tickets = extra["running_tickets"]
        assert tickets[0]["source"] == "github"
        assert tickets[0]["identifier"] == "owner/repo#99"


# ---------------------------------------------------------------------------
# run_loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    async def test_runs_one_tick_then_stops(self, tmp_path: Path) -> None:
        config_path = tmp_path / "symphony.yaml"

        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = {}
        tracker.fetch_candidates.return_value = []

        stop = asyncio.Event()

        with (
            patch("claude_on_the_fly.symphony.orchestrator.load_config") as mock_load,
            patch(
                "claude_on_the_fly.symphony.orchestrator.make_trackers",
                return_value={"jira": tracker},
            ),
            patch(
                "claude_on_the_fly.symphony.orchestrator._refresh_prompt_stores"
            ) as mock_rps,
            patch("claude_on_the_fly.symphony.orchestrator.startup_cleanup") as mock_sc,
        ):
            mock_load.return_value = _config()
            mock_rps.return_value = None  # leave prompt_stores empty
            mock_sc.return_value = None

            async def _stop_soon() -> None:
                await asyncio.sleep(0.01)
                stop.set()

            t = asyncio.create_task(run_loop(config_path, stop))
            await _stop_soon()
            await t

            tracker.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Event emission — black-box checks that each transition writes to EventLog
# ---------------------------------------------------------------------------


class TestEventEmission:
    async def test_dispatch_emits_dispatched_event(self, tmp_path: Path) -> None:
        issue = _issue(identifier="PROJ-EV1")
        state = OrchestratorState()
        tracker = _mock_tracker()
        event_log = _real_event_log(tmp_path)
        pending: set[asyncio.Task[None]] = set()

        with patch("claude_on_the_fly.symphony.orchestrator._run_worker") as mock_rw:
            mock_rw.return_value = None
            cfg = _config()
            _dispatch(
                issue,
                state,
                tracker,
                cfg.tracker,
                cfg,
                "prompt",
                RetryQueue(),
                event_log,
                pending,
            )

        events = event_log.tail(10)
        assert len(events) == 1
        assert events[0]["type"] == "dispatched"
        assert events[0]["identifier"] == "PROJ-EV1"
        assert events[0]["source"] == "symphony"
        assert events[0]["tracker"] == "jira"

    async def test_stall_emits_cancelled_with_reason(self, tmp_path: Path) -> None:
        event_log = _real_event_log(tmp_path)
        config = _config(stall_timeout_ms=5_000)
        rq = RetryQueue(event_log=event_log)
        task = MagicMock()
        task.done.return_value = False
        entry = RunningEntry(
            issue_id="1",
            issue_identifier="PROJ-EV2",
            issue_state="In Progress",
            started_at=100.0,
            task=task,
        )
        _check_and_cancel_stall(entry, config, rq, event_log, 110.0)

        events = event_log.tail(10)
        types = [e["type"] for e in events]
        # cancelled comes first (the cancel itself), then retry_scheduled
        assert "cancelled" in types
        cancelled = next(e for e in events if e["type"] == "cancelled")
        assert cancelled["reason"] == "stall"
        assert cancelled["identifier"] == "PROJ-EV2"
        assert "retry_scheduled" in types

    async def test_reconcile_terminal_emits_cancelled(self, tmp_path: Path) -> None:
        event_log = _real_event_log(tmp_path)
        state = OrchestratorState()
        issue = _issue(identifier="PROJ-EV3")
        state.claim(issue)
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-EV3": "Done"}, terminal=True
        )

        await reconcile(state, _trackers(tracker), cfg, RetryQueue(), event_log)

        events = event_log.tail(10)
        cancelled = [e for e in events if e["type"] == "cancelled"]
        assert len(cancelled) == 1
        assert cancelled[0]["reason"] == "terminal"

    async def test_reconcile_inactive_emits_cancelled(self, tmp_path: Path) -> None:
        event_log = _real_event_log(tmp_path)
        state = OrchestratorState()
        issue = _issue(identifier="PROJ-EV4")
        state.claim(issue)
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-EV4": "Backlog"}, matches_jql=False
        )

        await reconcile(state, _trackers(tracker), cfg, RetryQueue(), event_log)

        events = event_log.tail(10)
        cancelled = [e for e in events if e["type"] == "cancelled"]
        assert len(cancelled) == 1
        assert cancelled[0]["reason"] == "inactive"

    async def test_worker_done_terminal_emits_event(self, tmp_path: Path) -> None:
        event_log = _real_event_log(tmp_path)
        issue = _issue(identifier="PROJ-EV5")
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        config = _config(max_turns=10)

        with (
            patch(
                "claude_on_the_fly.symphony.orchestrator.ensure_workspace"
            ) as mock_ew,
            patch(
                "claude_on_the_fly.symphony.orchestrator.TicketRunner"
            ) as mock_runner_cls,
            patch("claude_on_the_fly.symphony.orchestrator.remove_workspace"),
        ):
            mock_ew.return_value = Path("/tmp/ws")
            mock_runner = MagicMock()
            mock_runner.run_turn = AsyncMock(return_value=MagicMock(body="ok"))
            mock_runner_cls.return_value = mock_runner
            tracker.fetch_summaries_by_keys.return_value = _summaries(
                {"PROJ-EV5": "Done"}, terminal=True
            )
            await _run_worker(
                issue,
                state,
                tracker,
                config.tracker,
                config,
                "prompt",
                RetryQueue(),
                event_log,
            )

        events = event_log.tail(10)
        done = [e for e in events if e["type"] == "worker_done"]
        assert len(done) == 1
        assert done[0]["reason"] == "terminal"

    async def test_retry_schedule_emits_event(self, tmp_path: Path) -> None:
        event_log = _real_event_log(tmp_path)
        rq = RetryQueue(event_log=event_log)
        rq.schedule_failure(
            "1", "PROJ-EV6", max_backoff_ms=60_000, attempt=2, error="boom"
        )

        events = event_log.tail(10)
        assert len(events) == 1
        assert events[0]["type"] == "retry_scheduled"
        assert events[0]["kind"] == "failure"
        assert events[0]["attempt"] == 2

    async def test_retry_continuation_emits_event(self, tmp_path: Path) -> None:
        event_log = _real_event_log(tmp_path)
        rq = RetryQueue(event_log=event_log)
        rq.schedule_continuation("1", "PROJ-EV7")

        events = event_log.tail(10)
        assert len(events) == 1
        assert events[0]["type"] == "retry_scheduled"
        assert events[0]["kind"] == "continuation"
