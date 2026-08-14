"""The pending-turn journal: what survives a stop, and what must not be replayed.

The dangerous direction is replaying too much. An entry becomes somebody's
message again, so anything unclear about a record has to resolve toward not
running it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_on_the_fly import turns
from claude_on_the_fly.turns import (
    DISPATCHED,
    QUEUED,
    PendingTurn,
    TurnJournal,
    new_turn_id,
)


@pytest.fixture
def journal(tmp_path: Path) -> TurnJournal:
    return TurnJournal(tmp_path / "state" / "slack.turns.json")


def _turn(**kwargs) -> PendingTurn:
    defaults = {
        "chat_id": 7,
        "text": "do the thing",
        "turn_id": new_turn_id(),
        "recorded_at": 1000.0,
    }
    return PendingTurn(**{**defaults, "route": {"channel": "C1"}, **kwargs})


class TestRecording:
    def test_a_recorded_turn_survives_a_new_reader(self, journal, tmp_path):
        """The whole point: the record outlives the process that made it."""
        entry = _turn()
        journal.record(entry)

        reopened = TurnJournal(journal.path)
        replay, nudge = reopened.take(now=1000.0)

        assert nudge == []
        assert [t.text for t in replay] == ["do the thing"]
        assert replay[0].route == {"channel": "C1"}

    def test_the_parent_directory_is_created(self, journal):
        journal.record(_turn())

        assert journal.path.is_file()

    def test_recording_twice_replaces_rather_than_duplicates(self, journal):
        entry = _turn()
        journal.record(entry)
        journal.record(entry)

        replay, _ = journal.take(now=1000.0)

        assert len(replay) == 1

    def test_an_unwritable_journal_does_not_raise(self, tmp_path, caplog):
        """A journal that cannot be written must not take down the turn it was
        describing. The turn is worth more than the record of it."""
        blocked = tmp_path / "file"
        blocked.write_text("not a directory")
        journal = TurnJournal(blocked / "slack.turns.json")

        with caplog.at_level("ERROR", logger="claude_on_the_fly.turns"):
            journal.record(_turn())

        assert "could not write" in caplog.text


class TestPhases:
    def test_marking_records_that_an_agent_had_started(self, journal):
        entry = _turn()
        journal.record(entry)
        journal.mark_dispatched(entry.turn_id)

        replay, _nudge = journal.take(now=1000.0)

        assert [t.phase for t in replay] == [DISPATCHED]

    def test_marking_an_unknown_turn_changes_nothing(self, journal):
        entry = _turn()
        journal.record(entry)

        journal.mark_dispatched("no-such-turn")

        replay, nudge = journal.take(now=1000.0)
        assert [t.phase for t in replay] == [QUEUED] and nudge == []

    def test_marking_twice_is_idempotent(self, journal):
        entry = _turn()
        journal.record(entry)
        journal.mark_dispatched(entry.turn_id)
        journal.mark_dispatched(entry.turn_id)

        replay, _nudge = journal.take(now=1000.0)
        assert [t.phase for t in replay] == [DISPATCHED]

    def test_an_answered_turn_is_forgotten(self, journal):
        entry = _turn()
        journal.record(entry)

        journal.forget(entry.turn_id)

        assert journal.take(now=1000.0) == ([], [])

    def test_forgetting_an_unknown_turn_is_silent(self, journal):
        journal.record(_turn())

        journal.forget("no-such-turn")

        replay, _ = journal.take(now=1000.0)
        assert len(replay) == 1


class TestTake:
    def test_taking_empties_the_journal(self, journal):
        """Emptied before anything runs, so a turn that kills the daemon cannot
        be replayed at every start. Same move as cron renaming its trigger."""
        journal.record(_turn())

        first, _ = journal.take(now=1000.0)
        second = journal.take(now=1000.0)

        assert len(first) == 1
        assert second == ([], [])

    def test_an_absent_journal_is_empty_not_an_error(self, journal):
        assert journal.take() == ([], [])

    def test_entries_come_back_oldest_first(self, journal):
        """Ids are time-sortable, and replay has to preserve the order the person
        sent them in."""
        for text in ("first", "second", "third"):
            journal.record(_turn(text=text, turn_id=f"{text}-id"))

        replay, _ = journal.take(now=1000.0)

        assert [t.turn_id for t in replay] == sorted(t.turn_id for t in replay)

    def test_an_expired_turn_is_neither_replayed_nor_mentioned(self, journal, caplog):
        """Nothing is owed to a question from another day."""
        journal.record(_turn())

        with caplog.at_level("INFO", logger="claude_on_the_fly.turns"):
            result = journal.take(ttl_s=60, now=1000.0 + 3600)

        assert result == ([], [])
        assert "older than" in caplog.text

    def test_a_turn_with_no_timestamp_is_not_treated_as_expired(self, journal):
        """A hand-written or pre-upgrade entry has no age. Dropping it would be a
        silent loss; the replay cap still bounds it."""
        journal.record(_turn(recorded_at=0.0))

        replay, _ = journal.take(ttl_s=1, now=10_000.0)

        assert len(replay) == 1

    def test_replaying_bumps_the_counter_so_a_poison_turn_parks(self, journal, caplog):
        journal.record(_turn(replays=turns.MAX_REPLAYS - 1))

        replay, _ = journal.take(now=1000.0)
        assert replay[0].replays == turns.MAX_REPLAYS

        journal.record(replay[0])
        with caplog.at_level("WARNING", logger="claude_on_the_fly.turns"):
            replay_again, nudge = journal.take(now=1000.0)

        assert replay_again == []
        assert len(nudge) == 1, "a parked turn is still offered back"
        assert "parking" in caplog.text

    def test_the_default_ttl_is_used_when_none_is_given(self, journal):
        journal.record(_turn(recorded_at=1.0))

        assert journal.take() == ([], [])


class TestCorruptRecords:
    def test_unparseable_json_reads_as_empty(self, journal, caplog):
        journal.path.parent.mkdir(parents=True)
        journal.path.write_text("{not json")

        with caplog.at_level("WARNING", logger="claude_on_the_fly.turns"):
            assert journal.take() == ([], [])

        assert "cannot read" in caplog.text

    def test_a_json_object_instead_of_a_list_reads_as_empty(self, journal, caplog):
        journal.path.parent.mkdir(parents=True)
        journal.path.write_text('{"turn_id": "x"}')

        with caplog.at_level("WARNING", logger="claude_on_the_fly.turns"):
            assert journal.take() == ([], [])

        assert "not a list" in caplog.text

    @pytest.mark.parametrize(
        "entry",
        [
            {},
            {"chat_id": 1},
            {"chat_id": 1, "text": "x"},
            {"chat_id": "not-an-int", "text": "x", "turn_id": "t"},
            {"chat_id": 1, "text": None, "turn_id": "t"},
            {"chat_id": 1, "text": "x", "turn_id": ""},
            {"chat_id": 1, "text": "x", "turn_id": 42},
            "not even a mapping",
        ],
    )
    def test_an_incomplete_entry_is_dropped(self, journal, entry):
        """The text becomes somebody's message, so a partial record is not
        something to guess at."""
        journal.path.parent.mkdir(parents=True)
        journal.path.write_text(json.dumps([entry]))

        assert journal.take(now=1000.0) == ([], [])

    def test_an_unknown_phase_resumes_as_if_work_had_started(self, journal):
        """A record we cannot classify gets the careful treatment: replayed, but
        with the note that some of it may already have happened."""
        journal.path.parent.mkdir(parents=True)
        journal.path.write_text(
            json.dumps(
                [{"chat_id": 1, "text": "x", "turn_id": "t", "phase": "something-else"}]
            )
        )

        replay, nudge = journal.take(now=1000.0)

        assert nudge == []
        assert [t.phase for t in replay] == [DISPATCHED]

    def test_junk_scalars_degrade_to_defaults(self, journal):
        journal.path.parent.mkdir(parents=True)
        journal.path.write_text(
            json.dumps(
                [
                    {
                        "chat_id": 1,
                        "text": "x",
                        "turn_id": "t",
                        "phase": QUEUED,
                        "route": "not-a-dict",
                        "session": 42,
                        "recorded_at": "yesterday",
                        "replays": "many",
                    }
                ]
            )
        )

        replay, _ = journal.take(now=1000.0)

        assert replay[0].route == {}
        assert replay[0].session is None
        assert replay[0].recorded_at == 0.0
        assert replay[0].replays == 1  # bumped from a defaulted 0


def test_a_round_trip_keeps_every_field(journal):
    entry = _turn(
        session="tok-1",
        compact=False,
        phase=DISPATCHED,
        route={"channel": "C9", "thread_ts": "1.5", "message_ts": "1.4"},
        replays=turns.MAX_REPLAYS,
    )
    journal.record(entry)

    # At the replay limit, so it comes back untouched rather than counter-bumped.
    _replay, nudge = journal.take(now=1000.0)

    assert nudge[0] == entry


def test_turn_ids_are_unique_and_sortable():
    ids = [new_turn_id() for _ in range(5)]

    assert len(set(ids)) == 5
    assert ids == sorted(ids)


def test_a_route_that_cannot_be_serialized_does_not_break_the_turn(journal, caplog):
    """`route_for` is frontend-owned, so its bugs arrive here. Losing the safety
    net is bad; refusing to serve the message is worse."""

    class Unserializable:
        pass

    with caplog.at_level("ERROR", logger="claude_on_the_fly.turns"):
        journal.record(_turn(route={"channel": Unserializable()}))

    assert "cannot serialize" in caplog.text
    assert journal.take(now=1000.0) == ([], [])


class TestBothPhasesResume:
    """The phase stopped deciding whether a turn comes back. It decides what the
    resumed turn is told, which is the caller's business."""

    def test_a_dispatched_turn_is_replayed_like_any_other(self, journal):
        entry = _turn()
        journal.record(entry)
        journal.mark_dispatched(entry.turn_id)

        replay, nudge = journal.take(now=1000.0)

        assert nudge == []
        assert [t.turn_id for t in replay] == [entry.turn_id]

    def test_the_phase_survives_so_the_caller_can_tell_them_apart(self, journal):
        fresh = _turn(turn_id="a-fresh")
        started = _turn(turn_id="b-started")
        journal.record(fresh)
        journal.record(started)
        journal.mark_dispatched(started.turn_id)

        replay, _ = journal.take(now=1000.0)

        assert {t.turn_id: t.phase for t in replay} == {
            "a-fresh": QUEUED,
            "b-started": DISPATCHED,
        }

    def test_only_a_turn_at_the_replay_limit_is_held_back(self, journal, caplog):
        """Running it again is the likeliest reason the daemon keeps going down."""
        journal.record(_turn(turn_id="parked", replays=turns.MAX_REPLAYS))
        journal.record(_turn(turn_id="ordinary"))

        with caplog.at_level("WARNING", logger="claude_on_the_fly.turns"):
            replay, nudge = journal.take(now=1000.0)

        assert [t.turn_id for t in replay] == ["ordinary"]
        assert [t.turn_id for t in nudge] == ["parked"]
        assert "parking" in caplog.text
