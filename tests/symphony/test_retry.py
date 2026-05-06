"""RetryQueue: backoff math, schedule_continuation vs schedule_failure, due_now."""

from __future__ import annotations

import time

from claude_on_the_fly.symphony.retry import (
    CONTINUATION_DELAY_MS,
    RetryQueue,
    failure_delay_ms,
)


def test_failure_delay_first_attempt():
    assert failure_delay_ms(1, 300_000) == 10_000


def test_failure_delay_escalates():
    assert failure_delay_ms(1, 300_000) == 10_000
    assert failure_delay_ms(2, 300_000) == 20_000
    assert failure_delay_ms(3, 300_000) == 40_000
    assert failure_delay_ms(4, 300_000) == 80_000
    assert failure_delay_ms(5, 300_000) == 160_000


def test_failure_delay_caps_at_max():
    assert failure_delay_ms(99, 300_000) == 300_000
    assert failure_delay_ms(99, 60_000) == 60_000


def test_failure_delay_clamps_low_attempt():
    assert failure_delay_ms(0, 300_000) == 10_000
    assert failure_delay_ms(-1, 300_000) == 10_000


def test_schedule_continuation_uses_fixed_delay():
    q = RetryQueue()
    before = time.monotonic() * 1000
    q.schedule_continuation("id1", "PROJ-1")
    after = time.monotonic() * 1000
    pending = q.all_pending()
    assert len(pending) == 1
    entry = pending[0]
    assert entry.issue_id == "id1"
    assert entry.identifier == "PROJ-1"
    assert (
        before + CONTINUATION_DELAY_MS - 50
        <= entry.due_at_ms
        <= after + CONTINUATION_DELAY_MS + 50
    )


def test_schedule_failure_increments_attempt_explicitly():
    q = RetryQueue()
    q.schedule_failure("id1", "PROJ-1", max_backoff_ms=300_000, attempt=1)
    assert q.get_attempt("id1") == 1
    q.schedule_failure("id1", "PROJ-1", max_backoff_ms=300_000, attempt=2)
    assert q.get_attempt("id1") == 2


def test_due_now_returns_and_removes_due_entries():
    q = RetryQueue()
    q.schedule_continuation("id1", "PROJ-1")
    now_ms = time.monotonic() * 1000
    # Not due yet (continuation is +1s in the future)
    assert q.due_now(now_ms) == []
    assert q.has("id1")

    # Force-due by querying with future timestamp
    far_future = now_ms + 10_000
    due = q.due_now(far_future)
    assert len(due) == 1
    assert due[0].issue_id == "id1"
    assert not q.has("id1")  # removed


def test_cancel_removes_entry():
    q = RetryQueue()
    q.schedule_continuation("id1", "PROJ-1")
    assert q.has("id1")
    q.cancel("id1")
    assert not q.has("id1")


def test_requeue_preserves_attempt():
    q = RetryQueue()
    q.schedule_failure("id1", "PROJ-1", max_backoff_ms=300_000, attempt=3, error="x")
    entry = q.all_pending()[0]
    q.requeue(entry, delay_ms=1000, error="no slots")
    new_entry = q.all_pending()[0]
    assert new_entry.attempt == 3
    assert new_entry.error == "no slots"


def test_continuation_preserves_prior_failure_count():
    q = RetryQueue()
    q.schedule_failure("id1", "PROJ-1", max_backoff_ms=300_000, attempt=2)
    q.schedule_continuation("id1", "PROJ-1")
    # Continuation preserves the prior failure_attempt (no escalation, no reset)
    assert q.get_attempt("id1") == 2


def test_continuation_with_no_prior_starts_at_zero():
    q = RetryQueue()
    q.schedule_continuation("id1", "PROJ-1")
    # Fresh continuation: zero prior failures
    assert q.get_attempt("id1") == 0
