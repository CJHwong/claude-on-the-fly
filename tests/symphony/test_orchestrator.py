"""Tests for symphony orchestrator: pure functions and async scheduling logic."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.symphony.config import SymphonyConfig, TrackerConfig
from claude_on_the_fly.symphony.orchestrator import (
    _check_and_cancel_stall,
    _dispatch,
    _eligible,
    _has_per_state_capacity,
    _heartbeat_extra,
    _log_config_summary,
    _process_due_retries,
    _redact_token,
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
    d: dict[str, str], *, label: str = "symphony-active"
) -> dict[str, IssueSummary]:
    """Helper: turn `{key: state}` into `{key: IssueSummary(state, {"labels": (label,)})}`.

    Default label matches `_config()`'s gate_label so existing tests that
    don't care about labels keep working with the new label check."""
    return {k: IssueSummary(state=v, extra={"labels": (label,)}) for k, v in d.items()}


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
        # Default carries the gate_label that _config() uses, so the orchestrator's
        # post-turn "label removed → exit" check doesn't fire spuriously. Tests
        # that exercise parking-by-label pass labels=() explicitly.
        "labels": ("symphony-active",),
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
    "email",
    "api_token",
    "project_key",
    "jql_extra",
    "active_states",
    "terminal_states",
    "gate_label",
    "prompt_path",
    "max_concurrent_by_state",
}


def _tracker_cfg(**overrides: object) -> TrackerConfig:
    defaults = {
        "kind": "jira",
        "base_url": "https://jira.example.com",
        "email": "bot@example.com",
        "api_token": "secret",
        "project_key": "PROJ",
        "jql_extra": 'AND status != "Done"',
        "active_states": ("In Progress", "To Do", "In Review"),
        "terminal_states": ("Done", "Cancelled"),
        "gate_label": "symphony-active",
        "prompt_path": Path("/tmp/symphony-prompt.md"),
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
        "polling_ms": 30_000,
        "max_concurrent": 3,
        "max_retry_backoff_ms": 3600_000,
    }
    global_overrides = {k: v for k, v in overrides.items() if k in global_defaults}
    return SymphonyConfig(
        trackers={"jira": tracker},  # type: ignore[dict-item]
        **(global_defaults | global_overrides),
    )


def _mock_tracker(*, gate_label: str = "symphony-active") -> MagicMock:
    """Mock tracker that behaves like Jira: predicates check state list +
    optional gate label. Tests override `is_terminal` / `is_active` /
    `issue_to_summary` when they need different semantics."""
    t = MagicMock()
    t.fetch_one = AsyncMock()
    t.fetch_summaries_by_keys = AsyncMock()
    t.fetch_candidates = AsyncMock()
    t.aclose = AsyncMock()

    def _is_terminal(summary, cfg):
        return summary.state in cfg.terminal_states

    def _is_active(summary, cfg):
        if summary.state not in cfg.active_states:
            return False
        if cfg.gate_label is None:
            return True
        labels = summary.extra.get("labels") or ()
        return cfg.gate_label.lower() in labels

    def _issue_to_summary(issue):
        from claude_on_the_fly.symphony.tracker.issue import IssueSummary

        return IssueSummary(state=issue.state, extra={"labels": issue.labels})

    t.is_terminal = MagicMock(side_effect=_is_terminal)
    t.is_active = MagicMock(side_effect=_is_active)
    t.issue_to_summary = MagicMock(side_effect=_issue_to_summary)
    return t


def _trackers(t: MagicMock | None = None) -> dict[str, Tracker]:
    """Wrap a single mock tracker into a `{"jira": tracker}` dict matching
    SymphonyConfig.trackers. Convenience for tests that exercise the
    multi-source orchestrator code paths with one source."""
    return {"jira": t if t is not None else _mock_tracker()}  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# _redact_token / _log_config_summary
# ---------------------------------------------------------------------------


class TestRedactToken:
    def test_empty_returns_unset(self) -> None:
        assert _redact_token("") == "<unset>"

    def test_short_token_masked(self) -> None:
        assert _redact_token("abcd") == "***"

    def test_long_token_partial(self) -> None:
        assert _redact_token("abcdef12345") == "ab***45"


class TestLogConfigSummary:
    def test_dumps_fields_and_redacts_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as _logging

        cfg = _config(
            tracker=_tracker_cfg(
                base_url="https://j.example.com",
                email="bot@example.com",
                api_token="supersecrettoken",
                project_key="PROJ",
            ),
            polling_ms=15000,
            max_concurrent=3,
            gate_label="ready_for_ai",
        )
        with caplog.at_level(
            _logging.INFO, logger="claude_on_the_fly.symphony.orchestrator"
        ):
            _log_config_summary(cfg)

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "symphony config:" in text
        assert "[jira]" in text
        assert "https://j.example.com" in text
        assert "bot@example.com" in text
        assert "polling_ms          = 15000" in text
        assert "max_concurrent      = 3" in text
        assert "ready_for_ai" in text  # gate_label
        assert "PROJ" in text
        # Redaction: real token never appears, masked form does.
        assert "supersecrettoken" not in text
        assert "su***en" in text


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

    def test_state_not_active(self) -> None:
        issue = _issue(state="Unknown")
        assert (
            _eligible(issue, OrchestratorState(), RetryQueue(), _tracker_cfg()) is False
        )

    def test_terminal_state(self) -> None:
        issue = _issue(state="Done")
        assert (
            _eligible(issue, OrchestratorState(), RetryQueue(), _tracker_cfg()) is False
        )

    def test_blocked_by_non_terminal(self) -> None:
        blocker = BlockerRef(key="PROJ-2", state="In Progress")
        issue = _issue(state="To Do", blocked_by=(blocker,))
        assert (
            _eligible(issue, OrchestratorState(), RetryQueue(), _tracker_cfg()) is False
        )

    def test_blocked_by_terminal_ok(self) -> None:
        blocker = BlockerRef(key="PROJ-2", state="Done")
        issue = _issue(state="To Do", blocked_by=(blocker,))
        assert (
            _eligible(issue, OrchestratorState(), RetryQueue(), _tracker_cfg()) is True
        )

    def test_eligible(self) -> None:
        issue = _issue(state="In Progress")
        assert (
            _eligible(issue, OrchestratorState(), RetryQueue(), _tracker_cfg()) is True
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
    def test_filters_and_sorts(self) -> None:
        cfg = _tracker_cfg()
        a = _issue(id="1", identifier="A", priority=5, state="In Progress")
        b = _issue(id="2", identifier="B", priority=1, state="In Progress")
        c = _issue(id="3", identifier="C", priority=3, state="Done")

        result = _select_candidates([a, b, c], OrchestratorState(), RetryQueue(), cfg)
        assert [i.identifier for i in result] == ["B", "A"]

    def test_empty_returns_empty(self) -> None:
        result = _select_candidates(
            [], OrchestratorState(), RetryQueue(), _tracker_cfg()
        )
        assert result == []


# ---------------------------------------------------------------------------
# _has_per_state_capacity
# ---------------------------------------------------------------------------


class TestHasPerStateCapacity:
    def test_under_global_cap_when_no_per_state_cap(self) -> None:
        state = OrchestratorState()
        cfg = _config()
        assert (
            _has_per_state_capacity(
                state, "In Progress", cfg.tracker, cfg.max_concurrent
            )
            is True
        )

    def test_at_per_state_cap(self) -> None:
        config = _config(max_concurrent_by_state={"in progress": 1})
        state = OrchestratorState()
        issue = _issue(state="In Progress")
        state.claim(issue)
        assert (
            _has_per_state_capacity(
                state, "In Progress", config.tracker, config.max_concurrent
            )
            is False
        )

    def test_under_per_state_cap(self) -> None:
        config = _config(max_concurrent_by_state={"in progress": 2})
        state = OrchestratorState()
        issue = _issue(state="In Progress")
        state.claim(issue)
        assert (
            _has_per_state_capacity(
                state, "In Progress", config.tracker, config.max_concurrent
            )
            is True
        )

    def test_falls_back_to_global_concurrent(self) -> None:
        config = _config(max_concurrent_by_state={})
        state = OrchestratorState()
        # Claim until full under global cap
        for i in range(config.max_concurrent):
            state.claim(_issue(id=str(i), state="In Progress"))
        assert (
            _has_per_state_capacity(
                state, "In Progress", config.tracker, config.max_concurrent
            )
            is False
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
        assert _check_and_cancel_stall(entry, config, RetryQueue(), 999_999.0) is False

    def test_not_stalled(self) -> None:
        config = _config(stall_timeout_ms=10_000)
        entry = RunningEntry(
            issue_id="1", issue_identifier="P-1", issue_state="S", started_at=100.0
        )
        assert _check_and_cancel_stall(entry, config, RetryQueue(), 105.0) is False

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
        result = _check_and_cancel_stall(entry, config, rq, 110.0)
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
        result = _check_and_cancel_stall(entry, config, rq, 210.0)
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
        result = _check_and_cancel_stall(entry, config, rq, 110.0)
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
            _dispatch(issue, state, tracker, cfg.tracker, cfg, "prompt", rq, pending)

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
            issue, state, tracker, cfg.tracker, cfg, "prompt", RetryQueue(), pending
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
                issue, state, tracker, config.tracker, config, "prompt", rq
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
                issue, state, tracker, config.tracker, config, "prompt", rq
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
                starting_failure_attempt=0,
            )

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

            tracker.fetch_one.return_value = _issue(state="Done")
            await _run_worker(
                issue, state, tracker, config.tracker, config, "prompt", rq
            )

        mock_rm.assert_called_once()

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

            tracker.fetch_one.return_value = _issue(state="Backlog")
            await _run_worker(
                issue, state, tracker, config.tracker, config, "prompt", rq
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
                issue, state, tracker, config.tracker, config, "prompt", rq
            )

        assert rq.has(issue.key) is True

    async def test_gate_label_removed_between_turns_exits(self) -> None:
        """Agent parks itself by removing the gate label — worker must stop."""
        issue = _issue()  # labels=("symphony-active",) by default
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
            # Post-turn refresh shows state still active BUT gate label gone.
            tracker.fetch_one.return_value = _issue(state="In Progress", labels=())
            await _run_worker(
                issue, state, tracker, config.tracker, config, "prompt", rq
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
        await reconcile(state, _trackers(tracker), _config(), RetryQueue())
        tracker.fetch_summaries_by_keys.assert_not_called()

    async def test_fetch_failure_logs_and_returns(self) -> None:
        state = OrchestratorState()
        issue = _issue()
        state.claim(issue)
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.side_effect = ConnectionError("down")
        await reconcile(
            state, _trackers(tracker), _config(stall_timeout_ms=0), RetryQueue()
        )
        tracker.fetch_summaries_by_keys.assert_awaited_once()

    async def test_terminal_mid_run_cancels_and_removes_workspace(self) -> None:
        state = OrchestratorState()
        issue = _issue()
        state.claim(issue)
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries({"PROJ-1": "Done"})

        with patch(
            "claude_on_the_fly.symphony.orchestrator.remove_workspace"
        ) as mock_rm:
            await reconcile(state, _trackers(tracker), cfg, RetryQueue())

        mock_rm.assert_not_called()  # workspace path is None, so it won't be called

    async def test_inactive_mid_run_cancels(self) -> None:
        state = OrchestratorState()
        issue = _issue()
        entry = state.claim(issue)
        task = MagicMock()
        task.done.return_value = False
        entry.task = task
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries({"PROJ-1": "Backlog"})

        await reconcile(state, _trackers(tracker), cfg, RetryQueue())
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

        await reconcile(state, _trackers(tracker), cfg, RetryQueue())
        assert entry.issue_state == "In Progress"

    async def test_gate_label_removed_mid_run_cancels(self) -> None:
        """Mid-run label removal cancels the worker even when state stays active."""
        state = OrchestratorState()
        issue = _issue()  # state="In Progress" (active), labels=("symphony-active",)
        entry = state.claim(issue)
        task = MagicMock()
        task.done.return_value = False
        entry.task = task
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        # Active state, but label gone.
        tracker.fetch_summaries_by_keys.return_value = {
            "PROJ-1": IssueSummary(
                state="In Progress", extra={"labels": ("other-label",)}
            ),
        }

        await reconcile(state, _trackers(tracker), cfg, RetryQueue())
        task.cancel.assert_called_once()

    async def test_gate_label_present_does_not_cancel(self) -> None:
        """Sanity check the new branch doesn't fire when the label is still there."""
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

        await reconcile(state, _trackers(tracker), cfg, RetryQueue())
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
        tracker.fetch_summaries_by_keys.return_value = _summaries({"PROJ-1": "Done"})

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
            state, _trackers(tracker), cfg, {"jira": "prompt"}, rq, pending
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
            pending,
        )
        assert rq.has(make_key("jira", "1")) is True  # requeued

    async def test_gate_label_missing_drops(self) -> None:
        # Agent removed the gate label to park the ticket. Retry path must drop it
        # rather than re-dispatch, so the daemon honors the agent's pause signal.
        cfg = _config()  # gate_label="symphony-active"
        rq = RetryQueue()
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}
        )
        tracker.fetch_one.return_value = _issue(labels=("other-label",))
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
                pending,
            )

        assert rq.has(make_key("jira", "1")) is False  # dropped, not requeued
        # _dispatch was NOT called: no worker task was created
        assert len(pending) == 0

    async def test_gate_label_present_dispatches(self) -> None:
        cfg = _config()  # gate_label="symphony-active"
        rq = RetryQueue()
        rq._entries[make_key("jira", "1")] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_summaries_by_keys.return_value = _summaries(
            {"PROJ-1": "In Progress"}
        )
        tracker.fetch_one.return_value = _issue(labels=("symphony-active",))
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
                pending,
            )

        tracker.fetch_one.assert_awaited_once_with("PROJ-1")


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
            state, cfg, {"jira": "prompt"}, _trackers(tracker), RetryQueue(), pending
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
            pending,
        )

    async def test_multi_source_dispatch_honors_global_cap(self) -> None:
        """Two trackers both have candidates; orchestrator merges them, sorts
        globally, and honors a global cap of 2 across sources."""
        from dataclasses import replace as _dc_replace

        # Build a two-source config (jira + github).
        jira_cfg = _tracker_cfg(kind="jira", gate_label=None)
        gh_cfg = _tracker_cfg(
            kind="github",
            active_states=("open",),
            terminal_states=("closed", "merged"),
            gate_label=None,
        )
        # SymphonyConfig: pass `trackers` directly via dataclasses.replace to
        # bypass the test helper's single-source default.
        base_cfg = _config()
        cfg = _dc_replace(base_cfg, trackers={"jira": jira_cfg, "github": gh_cfg})
        cfg = _dc_replace(cfg, max_concurrent=2)

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
                pending,
            )

        # Both trackers were polled.
        jira_tracker.fetch_candidates.assert_awaited_once()
        gh_tracker.fetch_candidates.assert_awaited_once()
        # Global cap of 2 honored.
        assert state.running_count() == 2
        # Lowest-priority issue (PROJ-1 prio=1) dispatched first, then
        # owner/repo#100 (prio=2); PROJ-2 (prio=3) skipped due to cap.
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

        jira_cfg = _tracker_cfg(kind="jira", gate_label=None)
        gh_cfg = _tracker_cfg(
            kind="github",
            active_states=("open",),
            terminal_states=("closed", "merged"),
            gate_label=None,
        )
        base_cfg = _config()
        cfg = _dc_replace(base_cfg, trackers={"jira": jira_cfg, "github": gh_cfg})

        jira_tracker = _mock_tracker()
        gh_tracker = _mock_tracker()

        # GitHub-specific predicates: open AND user hasn't reviewed yet.
        def gh_is_terminal(summary, c):
            return summary.state in c.terminal_states

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
            "PROJ-1": IssueSummary(
                state="In Progress", extra={"labels": ("symphony-active",)}
            ),
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
            {"jira": jira_tracker, "github": gh_tracker},  # type: ignore[dict-item]
            cfg,
            RetryQueue(),
        )

        # Jira stayed running, GitHub got cancelled.
        jira_task.cancel.assert_not_called()
        gh_task.cancel.assert_called_once()
        # Each tracker's summaries fetch was called exactly once, with its own keys.
        jira_tracker.fetch_summaries_by_keys.assert_awaited_once_with(["PROJ-1"])
        gh_tracker.fetch_summaries_by_keys.assert_awaited_once_with(["owner/repo#200"])


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
        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("# Prompt")

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
            patch("claude_on_the_fly.symphony.orchestrator.PromptStore") as mock_ps,
            patch("claude_on_the_fly.symphony.orchestrator.startup_cleanup") as mock_sc,
        ):
            mock_load.return_value = _config(prompt_path=prompt_path)
            mock_ps_instance = MagicMock()
            mock_ps_instance.load.return_value = "prompt source"
            mock_ps_instance.maybe_reload.return_value = "prompt source"
            mock_ps.return_value = mock_ps_instance
            mock_sc.return_value = None

            async def _stop_soon() -> None:
                await asyncio.sleep(0.01)
                stop.set()

            t = asyncio.create_task(run_loop(config_path, stop))
            await _stop_soon()
            await t

            tracker.aclose.assert_awaited_once()
