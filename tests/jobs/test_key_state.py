"""KeyStateStore: the two questions the queue cannot answer — should this key
back off after failing, and has it stopped making progress."""

from __future__ import annotations

from pathlib import Path

from claude_on_the_fly.jobs.key_state import (
    BASE_BACKOFF_S,
    KeyStateStore,
    backoff_s,
    fingerprint,
)


def _store(tmp_path: Path) -> KeyStateStore:
    return KeyStateStore(tmp_path)


# --- fingerprint -----------------------------------------------------------


def test_fingerprint_ignores_key_order() -> None:
    """A producer whose JSON serializer reorders fields has not made progress."""
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_fingerprint_changes_when_any_value_changes() -> None:
    before = fingerprint({"key": "ACE-1", "status": "In Progress"})
    after = fingerprint({"key": "ACE-1", "status": "In Review"})
    assert before != after


def test_fingerprint_survives_a_non_json_value() -> None:
    """It runs mid-fire, so an odd value must degrade rather than raise."""
    assert fingerprint({"when": object()})


# --- backoff ---------------------------------------------------------------


def test_backoff_doubles_and_caps() -> None:
    assert backoff_s(0) == 0.0
    assert backoff_s(1) == BASE_BACKOFF_S
    assert backoff_s(2) == BASE_BACKOFF_S * 2
    assert backoff_s(3) == BASE_BACKOFF_S * 4
    assert backoff_s(50, max_backoff_s=300.0) == 300.0


# --- gating ----------------------------------------------------------------


def test_a_fresh_key_is_never_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.should_skip("jira/ACE-1", fingerprint({"key": "ACE-1"})) is None


def test_a_failed_key_backs_off_then_becomes_claimable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    print_ = fingerprint({"key": "ACE-1"})
    store.record_fire("jira/ACE-1", print_)
    store.record_outcome("jira/ACE-1", ok=False, now=1000.0)

    within = store.should_skip("jira/ACE-1", print_, now=1000.0 + BASE_BACKOFF_S / 2)
    after = store.should_skip("jira/ACE-1", print_, now=1000.0 + BASE_BACKOFF_S + 1)

    assert within is not None and "backing off" in within
    assert after is None


def test_a_success_clears_the_failure_streak(tmp_path: Path) -> None:
    store = _store(tmp_path)
    print_ = fingerprint({"key": "ACE-1"})
    store.record_fire("jira/ACE-1", print_)
    store.record_outcome("jira/ACE-1", ok=False, now=1000.0)
    store.record_outcome("jira/ACE-1", ok=True)

    assert store.should_skip("jira/ACE-1", print_, now=1000.1) is None


def test_a_changed_item_beats_the_backoff(tmp_path: Path) -> None:
    """Somebody edited the ticket while it was backing off. That is new
    information, and making them wait out an exponential delay for it would look
    like the daemon ignoring them."""
    store = _store(tmp_path)
    old = fingerprint({"key": "ACE-1", "status": "open"})
    store.record_fire("jira/ACE-1", old)
    store.record_outcome("jira/ACE-1", ok=False, now=1000.0)

    new = fingerprint({"key": "ACE-1", "status": "in review"})

    assert store.should_skip("jira/ACE-1", new, now=1000.1) is None


def test_a_key_parks_after_max_fires_with_no_change(tmp_path: Path) -> None:
    """The no-progress guard. The query keeps producing this item, so without
    parking it would be worked forever."""
    store = _store(tmp_path)
    print_ = fingerprint({"key": "ACE-1"})
    for _ in range(3):
        assert store.should_skip("jira/ACE-1", print_, max_fires=3) is None
        store.record_fire("jira/ACE-1", print_)
        store.record_outcome("jira/ACE-1", ok=True)

    reason = store.should_skip("jira/ACE-1", print_, max_fires=3)

    assert reason is not None and "parked" in reason


def test_a_parked_key_unparks_when_the_item_changes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = fingerprint({"key": "ACE-1", "status": "open"})
    for _ in range(3):
        store.record_fire("jira/ACE-1", old)
    assert store.should_skip("jira/ACE-1", old, max_fires=3) is not None

    moved = fingerprint({"key": "ACE-1", "status": "done"})

    assert store.should_skip("jira/ACE-1", moved, max_fires=3) is None


def test_max_fires_zero_disables_parking(tmp_path: Path) -> None:
    store = _store(tmp_path)
    print_ = fingerprint({"key": "ACE-1"})
    for _ in range(10):
        store.record_fire("jira/ACE-1", print_)

    assert store.should_skip("jira/ACE-1", print_, max_fires=0) is None


def test_record_fire_resets_counters_when_the_item_moves(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = fingerprint({"n": 1})
    store.record_fire("jira/ACE-1", old)
    store.record_fire("jira/ACE-1", old)
    store.record_outcome("jira/ACE-1", ok=False, now=1000.0)

    state = store.record_fire("jira/ACE-1", fingerprint({"n": 2}))

    assert state.fires_since_change == 1
    assert state.failures == 0


# --- storage ---------------------------------------------------------------


def test_state_persists_across_stores(tmp_path: Path) -> None:
    """The producer restarts; a key mid-backoff must still be mid-backoff."""
    print_ = fingerprint({"key": "ACE-1"})
    first = _store(tmp_path)
    first.record_fire("jira/ACE-1", print_)
    first.record_outcome("jira/ACE-1", ok=False, now=1000.0)

    reloaded = _store(tmp_path).load("jira/ACE-1")

    assert reloaded.failures == 1
    assert reloaded.last_failed_at == 1000.0
    assert reloaded.fires_since_change == 1


def test_entry_and_item_share_no_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_fire("jira/ACE-1", fingerprint({"n": 1}))
    store.record_fire("prs/owner/repo#7", fingerprint({"n": 2}))

    assert len(list(store.dir.glob("*.json"))) == 2


def test_a_corrupt_record_reads_as_fresh(tmp_path: Path) -> None:
    """One unnecessary run beats a producer that cannot fire until somebody
    deletes a file by hand."""
    store = _store(tmp_path)
    store.record_fire("jira/ACE-1", fingerprint({"n": 1}))
    corrupt = next(store.dir.glob("*.json"))
    corrupt.write_text("{not json", encoding="utf-8")

    state = store.load("jira/ACE-1")

    assert state.failures == 0
    assert state.fires_since_change == 0
    assert store.should_skip("jira/ACE-1", fingerprint({"n": 1})) is None


def test_no_temp_file_is_left_behind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_fire("jira/ACE-1", fingerprint({"n": 1}))

    assert not list(store.dir.glob("*.tmp"))


# --- the OutcomeRecorder adapter -------------------------------------------


class _Job:
    def __init__(self, key: str | None) -> None:
        self.key = key


class _Result:
    def __init__(self, ok: bool) -> None:
        self.ok = ok


def test_the_recorder_feeds_the_backoff_the_producer_reads(tmp_path: Path) -> None:
    """End of the loop: worker records a failure, producer's next fire backs off."""
    from claude_on_the_fly.jobs.key_state import KeyStateOutcomeRecorder

    store = _store(tmp_path)
    print_ = fingerprint({"key": "ACE-1"})
    store.record_fire("jira/ACE-1", print_)

    KeyStateOutcomeRecorder(store).record(_Job("jira/ACE-1"), _Result(ok=False))

    reason = store.should_skip("jira/ACE-1", print_)
    assert reason is not None and "backing off" in reason


def test_the_recorder_ignores_unkeyed_jobs(tmp_path: Path) -> None:
    """A Slack job belongs to no producer, so it has no state to fold into."""
    from claude_on_the_fly.jobs.key_state import KeyStateOutcomeRecorder

    store = _store(tmp_path)

    KeyStateOutcomeRecorder(store).record(_Job(None), _Result(ok=False))

    assert not store.dir.exists()
