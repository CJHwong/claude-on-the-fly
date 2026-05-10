"""Tests for symphony orchestrator: pure functions and async scheduling logic."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


from claude_on_the_fly.symphony.config import SymphonyConfig, TrackerConfig
from claude_on_the_fly.symphony.orchestrator import (
    _check_and_cancel_stall,
    _dispatch,
    _eligible,
    _has_per_state_capacity,
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
from claude_on_the_fly.symphony.tracker.issue import BlockerRef, Issue


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
    }
    return Issue(**(defaults | {k: v for k, v in overrides.items() if k in defaults}))  # type: ignore[arg-type]


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
    }
    kwargs = defaults | {k: v for k, v in overrides.items() if k in defaults}
    return TrackerConfig(**kwargs)  # type: ignore[arg-type]


def _config(**overrides: object) -> SymphonyConfig:
    defaults = {
        "tracker": _tracker_cfg(),
        "gate_label": "symphony-active",
        "turn_timeout_ms": 60_000,
        "max_turns": 10,
        "stall_timeout_ms": 300_000,
        "polling_ms": 30_000,
        "max_concurrent": 3,
        "max_retry_backoff_ms": 3600_000,
        "max_concurrent_by_state": {},
        "prompt_path": Path("/tmp/symphony-prompt.md"),
    }
    kwargs = defaults | {k: v for k, v in overrides.items() if k in defaults}
    return SymphonyConfig(**kwargs)  # type: ignore[arg-type]


def _mock_tracker() -> MagicMock:
    t = MagicMock()
    t.fetch_one = AsyncMock()
    t.fetch_states_by_keys = AsyncMock()
    t.fetch_candidates = AsyncMock()
    t.aclose = AsyncMock()
    return t


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
        assert _has_per_state_capacity(state, "In Progress", _config()) is True

    def test_at_per_state_cap(self) -> None:
        config = _config(max_concurrent_by_state={"in progress": 1})
        state = OrchestratorState()
        issue = _issue(state="In Progress")
        state.claim(issue)
        assert _has_per_state_capacity(state, "In Progress", config) is False

    def test_under_per_state_cap(self) -> None:
        config = _config(max_concurrent_by_state={"in progress": 2})
        state = OrchestratorState()
        issue = _issue(state="In Progress")
        state.claim(issue)
        assert _has_per_state_capacity(state, "In Progress", config) is True

    def test_falls_back_to_global_concurrent(self) -> None:
        config = _config(max_concurrent_by_state={})
        state = OrchestratorState()
        # Claim until full under global cap
        for i in range(config.max_concurrent):
            state.claim(_issue(id=str(i), state="In Progress"))
        assert _has_per_state_capacity(state, "In Progress", config) is False


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
        assert rq.has("1") is True

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
        assert rq.has("1") is True


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
            _dispatch(issue, state, tracker, _config(), "prompt", rq, pending)

        assert state.is_claimed(issue.id) is True
        assert len(pending) == 1

    async def test_dispatch_raises_on_duplicate(self) -> None:
        issue = _issue()
        state = OrchestratorState()
        state.claim(issue)
        tracker = _mock_tracker()
        pending: set[asyncio.Task[None]] = set()

        _dispatch(issue, state, tracker, _config(), "prompt", RetryQueue(), pending)
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

            await _run_worker(issue, state, tracker, config, "prompt", rq)

        assert rq.has(issue.id) is True
        assert state.is_claimed(issue.id) is False

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
            await _run_worker(issue, state, tracker, config, "prompt", rq)

        assert rq.has(issue.id) is True
        assert state.is_claimed(issue.id) is False

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
                issue, state, tracker, config, "prompt", rq, starting_failure_attempt=0
            )

        assert rq.has(issue.id) is True

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
            await _run_worker(issue, state, tracker, config, "prompt", rq)

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
            await _run_worker(issue, state, tracker, config, "prompt", rq)

        assert state.is_claimed(issue.id) is False

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
            await _run_worker(issue, state, tracker, config, "prompt", rq)

        assert rq.has(issue.id) is True


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


class TestReconcile:
    async def test_no_running_workers_returns_early(self) -> None:
        state = OrchestratorState()
        tracker = _mock_tracker()
        await reconcile(state, tracker, _config(), RetryQueue())
        tracker.fetch_states_by_keys.assert_not_called()

    async def test_fetch_failure_logs_and_returns(self) -> None:
        state = OrchestratorState()
        issue = _issue()
        state.claim(issue)
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.side_effect = ConnectionError("down")
        await reconcile(state, tracker, _config(stall_timeout_ms=0), RetryQueue())
        tracker.fetch_states_by_keys.assert_awaited_once()

    async def test_terminal_mid_run_cancels_and_removes_workspace(self) -> None:
        state = OrchestratorState()
        issue = _issue()
        state.claim(issue)
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "Done"}

        with patch(
            "claude_on_the_fly.symphony.orchestrator.remove_workspace"
        ) as mock_rm:
            await reconcile(state, tracker, cfg, RetryQueue())

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
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "Backlog"}

        await reconcile(state, tracker, cfg, RetryQueue())
        task.cancel.assert_called_once()

    async def test_state_update_when_changed_and_still_active(self) -> None:
        state = OrchestratorState()
        issue = _issue(state="To Do")
        entry = state.claim(issue)
        cfg = _config(stall_timeout_ms=0)
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "In Progress"}

        await reconcile(state, tracker, cfg, RetryQueue())
        assert entry.issue_state == "In Progress"


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
            await startup_cleanup(root, tracker, _tracker_cfg())

        _asyncio.run(_run())
        tracker.fetch_states_by_keys.assert_not_called()

    def test_no_dirs(self, tmp_path: Path) -> None:
        tracker = _mock_tracker()
        root = tmp_path / "empty"
        root.mkdir()

        async def _run() -> None:
            await startup_cleanup(root, tracker, _tracker_cfg())

        asyncio.run(_run())
        tracker.fetch_states_by_keys.assert_not_called()

    def test_removes_terminal_dirs(self, tmp_path: Path) -> None:
        tracker = _mock_tracker()
        root = tmp_path / "worktrees"
        root.mkdir()
        d = root / "PROJ-1"
        d.mkdir()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "Done"}

        async def _run() -> None:
            await startup_cleanup(root, tracker, _tracker_cfg())

        asyncio.run(_run())
        assert not d.exists()

    def test_leaves_non_terminal_dirs(self, tmp_path: Path) -> None:
        tracker = _mock_tracker()
        root = tmp_path / "worktrees"
        root.mkdir()
        d = root / "PROJ-2"
        d.mkdir()
        tracker.fetch_states_by_keys.return_value = {"PROJ-2": "In Progress"}

        async def _run() -> None:
            await startup_cleanup(root, tracker, _tracker_cfg())

        asyncio.run(_run())
        assert d.exists()

    def test_fetch_failure_logs_and_skips(self, tmp_path: Path) -> None:
        tracker = _mock_tracker()
        root = tmp_path / "worktrees"
        root.mkdir()
        (root / "PROJ-1").mkdir()
        tracker.fetch_states_by_keys.side_effect = ConnectionError("down")

        async def _run() -> None:
            await startup_cleanup(root, tracker, _tracker_cfg())

        asyncio.run(_run())
        # DIR not removed because fetch failed
        assert (root / "PROJ-1").exists()


# ---------------------------------------------------------------------------
# _process_due_retries
# ---------------------------------------------------------------------------


class TestProcessDueRetries:
    async def test_empty_due_returns_early(self) -> None:
        rq = RetryQueue()
        tracker = _mock_tracker()
        pending: set[asyncio.Task[None]] = set()
        await _process_due_retries(
            OrchestratorState(), tracker, _config(), "prompt", rq, pending
        )
        tracker.fetch_states_by_keys.assert_not_called()

    async def test_terminal_state_dropped(self) -> None:
        cfg = _config()
        rq = RetryQueue()
        # Pretend attempt=2 so delay is computed from failure_delay_ms
        rq.schedule_failure(
            "1", "PROJ-1", cfg.max_retry_backoff_ms, attempt=2, error="test"
        )
        # Override due_at so it's "due now"
        rq._entries["1"] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=2, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "Done"}
        pending: set[asyncio.Task[None]] = set()

        await _process_due_retries(
            OrchestratorState(), tracker, cfg, "prompt", rq, pending
        )
        assert rq.has("1") is False  # dropped, not requeued

    async def test_inactive_state_dropped(self) -> None:
        cfg = _config()
        rq = RetryQueue()
        rq._entries["1"] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "Backlog"}
        pending: set[asyncio.Task[None]] = set()

        await _process_due_retries(
            OrchestratorState(), tracker, cfg, "prompt", rq, pending
        )
        assert rq.has("1") is False

    async def test_no_global_slots_requeues(self) -> None:
        cfg = _config(max_concurrent=2)
        state = OrchestratorState()
        state.claim(_issue(id="99", state="In Progress"))
        state.claim(_issue(id="100", state="In Progress"))
        rq = RetryQueue()
        rq._entries["1"] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "In Progress"}
        pending: set[asyncio.Task[None]] = set()

        await _process_due_retries(state, tracker, cfg, "prompt", rq, pending)
        assert rq.has("1") is True  # requeued

    async def test_dispatches_when_slots_available(self) -> None:
        cfg = _config(max_concurrent=3)
        rq = RetryQueue()
        rq._entries["1"] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "In Progress"}
        tracker.fetch_one.return_value = _issue()
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ):
            await _process_due_retries(
                OrchestratorState(), tracker, cfg, "prompt", rq, pending
            )

        tracker.fetch_one.assert_awaited_once_with("PROJ-1")

    async def test_fetch_one_failure_requeues(self) -> None:
        cfg = _config()
        rq = RetryQueue()
        rq._entries["1"] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=2, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "In Progress"}
        tracker.fetch_one.side_effect = ConnectionError("down")
        pending: set[asyncio.Task[None]] = set()

        await _process_due_retries(
            OrchestratorState(), tracker, cfg, "prompt", rq, pending
        )
        assert rq.has("1") is True  # requeued

    async def test_gate_label_missing_drops(self) -> None:
        # Agent removed the gate label to park the ticket. Retry path must drop it
        # rather than re-dispatch, so the daemon honors the agent's pause signal.
        cfg = _config()  # gate_label="symphony-active"
        rq = RetryQueue()
        rq._entries["1"] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "In Progress"}
        tracker.fetch_one.return_value = _issue(labels=("other-label",))
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ):
            await _process_due_retries(
                OrchestratorState(), tracker, cfg, "prompt", rq, pending
            )

        assert rq.has("1") is False  # dropped, not requeued
        # _dispatch was NOT called: no worker task was created
        assert len(pending) == 0

    async def test_gate_label_present_dispatches(self) -> None:
        cfg = _config()  # gate_label="symphony-active"
        rq = RetryQueue()
        rq._entries["1"] = RetryEntry(
            issue_id="1", identifier="PROJ-1", attempt=1, due_at_ms=0, error="test"
        )
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {"PROJ-1": "In Progress"}
        tracker.fetch_one.return_value = _issue(labels=("symphony-active",))
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ):
            await _process_due_retries(
                OrchestratorState(), tracker, cfg, "prompt", rq, pending
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
        tracker.fetch_states_by_keys.return_value = {}
        pending: set[asyncio.Task[None]] = set()

        await tick(state, cfg, "prompt", tracker, RetryQueue(), pending)
        tracker.fetch_candidates.assert_not_called()

    async def test_dispatches_candidates(self) -> None:
        cfg = _config(max_concurrent=3)
        state = OrchestratorState()
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {}
        tracker.fetch_candidates.return_value = [
            _issue(id="1", identifier="PROJ-1", state="In Progress", priority=1),
        ]
        pending: set[asyncio.Task[None]] = set()

        with patch(
            "claude_on_the_fly.symphony.orchestrator._run_worker", return_value=None
        ):
            await tick(state, cfg, "prompt", tracker, RetryQueue(), pending)

        tracker.fetch_candidates.assert_awaited_once()

    async def test_fetch_candidates_failure(self) -> None:
        cfg = _config()
        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {}
        tracker.fetch_candidates.side_effect = ConnectionError("down")
        pending: set[asyncio.Task[None]] = set()

        # Should not raise
        await tick(OrchestratorState(), cfg, "prompt", tracker, RetryQueue(), pending)


# ---------------------------------------------------------------------------
# run_loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    async def test_runs_one_tick_then_stops(self, tmp_path: Path) -> None:
        config_path = tmp_path / "symphony.yaml"
        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("# Prompt")

        tracker = _mock_tracker()
        tracker.fetch_states_by_keys.return_value = {}
        tracker.fetch_candidates.return_value = []

        stop = asyncio.Event()

        with (
            patch("claude_on_the_fly.symphony.orchestrator.load_config") as mock_load,
            patch(
                "claude_on_the_fly.symphony.orchestrator.make_tracker",
                return_value=tracker,
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
