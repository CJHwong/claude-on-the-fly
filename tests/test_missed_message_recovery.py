"""Pending work survives crashes without blocking the next request."""

import asyncio
import json
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from claude_on_the_fly.orchestrator import Orchestrator, Turn
from claude_on_the_fly.turns import MAX_REPLAYS, PendingTurn, TurnJournal

LARGE_CHAT = 2**63 + 7


def pending(journal, **kwargs):
    item = PendingTurn(
        turn_id="original",
        chat_id=LARGE_CHAT,
        text="source question",
        route={"channel": "D1", "thread_ts": "100.0", "message_ts": "101.0"},
        recorded_at=1000,
        **kwargs,
    )
    journal.record(item)
    return item


def test_stale_turn_is_archived_and_offered_once(tmp_path):
    journal = TurnJournal(tmp_path / "slack.turns.json")
    item = pending(journal)
    replay, nudge = journal.take(now=4000)
    assert replay == [] and nudge == [item]
    reopened = TurnJournal(journal.path)
    assert reopened.parked() == [item]
    assert reopened.pending() == []
    assert reopened.take(now=5000) == ([], [])
    assert journal.recovery_path.stat().st_mode & 0o777 == 0o600


def test_crash_between_reservation_and_enqueue_keeps_bounded_original(tmp_path):
    journal = TurnJournal(tmp_path / "slack.turns.json")
    pending(journal)
    for count in range(1, MAX_REPLAYS + 1):
        journal = TurnJournal(journal.path)
        replay, nudge = journal.take(now=1001)
        assert [r.turn_id for r in replay] == ["original"]
        assert replay[0].replays == count and nudge == []
        assert journal.pending() == replay
    journal = TurnJournal(journal.path)
    replay, nudge = journal.take(now=1001)
    assert replay == [] and len(nudge) == 1
    assert nudge[0].replays == MAX_REPLAYS
    assert journal.parked() == nudge


def test_archive_io_failure_keeps_active_request(tmp_path, monkeypatch):
    journal = TurnJournal(tmp_path / "slack.turns.json")
    item = pending(journal)
    monkeypatch.setattr(journal, "park", lambda _: False)
    assert journal.take(now=4000) == ([], [])
    assert journal.pending() == [item]


@pytest.mark.parametrize(
    "contents", ["broken recovery inventory", "{}", "[{}]", "[true]"]
)
def test_corrupt_archive_is_never_overwritten(tmp_path, contents):
    journal = TurnJournal(tmp_path / "slack.turns.json")
    item = pending(journal)
    journal.recovery_path.write_text(contents)
    assert journal.take(now=4000) == ([], [])
    assert journal.pending() == [item]
    assert journal.recovery_path.read_text() == contents


class RecoveryOrchestrator(Orchestrator):
    @property
    def _journal(self):
        return self.test_journal


def bare_orchestrator(tmp_path):
    orch = object.__new__(RecoveryOrchestrator)
    orch.test_journal = TurnJournal(tmp_path / "slack.turns.json")
    orch._queues = {LARGE_CHAT: asyncio.Queue()}
    orch._running = {}
    orch._offer_back = AsyncMock()
    orch._frontend = SimpleNamespace(retire_failed_turn=AsyncMock())
    return orch


async def test_one_overflow_parks_request_and_does_not_strand_next(tmp_path):
    orch = bare_orchestrator(tmp_path)
    item = pending(orch._journal)
    orch._queues[LARGE_CHAT].put_nowait(Turn("bad", journal_id=item.turn_id))
    orch._queues[LARGE_CHAT].put_nowait(Turn("next"))
    orch._process = AsyncMock(side_effect=[OverflowError("too large for SQLite"), None])
    await orch._drain(LARGE_CHAT)
    assert orch._process.await_count == 2
    assert orch._journal.parked() == [item]
    assert orch._journal.pending() == []
    orch._offer_back.assert_awaited_once_with(item)


async def test_cancellation_does_not_become_an_automatic_retry(tmp_path):
    orch = bare_orchestrator(tmp_path)
    item = pending(orch._journal)
    orch._queues[LARGE_CHAT].put_nowait(Turn("stop", journal_id=item.turn_id))
    orch._process = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await orch._drain(LARGE_CHAT)
    assert orch._journal.pending() == [item]
    assert orch._journal.parked() == []
    orch._offer_back.assert_not_awaited()


async def test_failed_slack_source_does_not_shift_later_suppression_flags():
    from claude_on_the_fly.slack import SlackFrontend

    front = object.__new__(SlackFrontend)
    front._pending_msg = {LARGE_CHAT: deque([("D1", "101.0"), ("D1", "102.0")])}
    front._pending_reply_suppressed = {LARGE_CHAT: deque([True, False])}
    front._in_flight = {}
    front.notify_complete = AsyncMock()
    front._unreact = AsyncMock()
    await front.retire_failed_turn(LARGE_CHAT, {"channel": "D1", "message_ts": "101.0"})
    assert list(front._pending_msg[LARGE_CHAT]) == [("D1", "102.0")]
    assert list(front._pending_reply_suppressed[LARGE_CHAT]) == [False]
    front.notify_complete.assert_not_awaited()


async def test_retiring_a_waiting_source_removes_its_waiting_marks():
    from claude_on_the_fly.slack import (
        INTERRUPTED_EMOJI,
        QUEUED_EMOJI,
        SlackFrontend,
    )

    front = object.__new__(SlackFrontend)
    front._pending_msg = {LARGE_CHAT: deque([("D1", "101.0")])}
    front._pending_reply_suppressed = {}
    front._in_flight = {}
    front._unreact = AsyncMock()
    await front.retire_failed_turn(LARGE_CHAT, {"channel": "D1", "message_ts": "101.0"})
    assert [c.args for c in front._unreact.await_args_list] == [
        ("D1", "101.0", QUEUED_EMOJI),
        ("D1", "101.0", INTERRUPTED_EMOJI),
    ]


async def test_retiring_in_flight_source_cleans_only_its_own_marker():
    from claude_on_the_fly.slack import SlackFrontend

    front = object.__new__(SlackFrontend)
    front._pending_msg = {}
    front._pending_reply_suppressed = {}
    front._in_flight = {LARGE_CHAT: ("D1", "101.0")}
    front.notify_complete = AsyncMock()
    await front.retire_failed_turn(LARGE_CHAT, {"channel": "D1", "message_ts": "101.0"})
    front.notify_complete.assert_awaited_once_with(LARGE_CHAT)


def test_repeated_parking_keeps_one_original_request(tmp_path):
    journal = TurnJournal(tmp_path / "slack.turns.json")
    item = pending(journal)
    assert journal.park(item)
    assert journal.park(item)
    assert journal.parked() == [item]


def test_failed_reservation_write_does_not_start_unrecorded_replay(
    tmp_path, monkeypatch
):
    journal = TurnJournal(tmp_path / "slack.turns.json")
    item = pending(journal)
    monkeypatch.setattr(journal, "_write", lambda _: False)
    assert journal.take(now=1001) == ([], [])
    assert TurnJournal(journal.path).pending() == [item]


async def test_failed_archive_keeps_the_source_and_processes_later_messages(tmp_path):
    orch = bare_orchestrator(tmp_path)
    item = pending(orch._journal)
    orch._queues[LARGE_CHAT].put_nowait(Turn("bad", journal_id=item.turn_id))
    orch._queues[LARGE_CHAT].put_nowait(Turn("next"))
    orch._process = AsyncMock(side_effect=[RuntimeError("intake failed"), None])
    orch.test_journal.park = Mock(return_value=False)
    await orch._drain(LARGE_CHAT)
    assert orch._process.await_count == 2
    assert orch._journal.pending() == [item]
    orch._frontend.retire_failed_turn.assert_awaited_once_with(LARGE_CHAT, item.route)
    orch._offer_back.assert_not_awaited()


async def test_marker_cleanup_failure_does_not_strand_the_queue(tmp_path):
    orch = bare_orchestrator(tmp_path)
    item = pending(orch._journal)
    orch._queues[LARGE_CHAT].put_nowait(Turn("bad", journal_id=item.turn_id))
    orch._queues[LARGE_CHAT].put_nowait(Turn("next"))
    orch._process = AsyncMock(side_effect=[RuntimeError("intake failed"), None])
    orch._frontend.retire_failed_turn.side_effect = OSError("unavailable")
    await orch._drain(LARGE_CHAT)
    assert orch._process.await_count == 2
    assert orch._journal.parked() == [item]
    orch._offer_back.assert_awaited_once_with(item)


async def test_recovery_notice_does_not_register_another_queued_turn(tmp_path):
    orch = bare_orchestrator(tmp_path)
    item = pending(orch._journal)
    orch._restore_chat = Mock()
    orch._frontend.notify_nudge = AsyncMock()
    await Orchestrator._offer_back(orch, item)
    restored = orch._restore_chat.call_args.args[0]
    assert restored.chat_id == item.chat_id
    assert restored.route == {"channel": "D1", "thread_ts": "100.0"}
    assert item.route["message_ts"] == "101.0"
    orch._frontend.notify_nudge.assert_awaited_once_with(item.chat_id, item.text)


async def test_missing_slack_source_fields_do_not_consume_any_markers():
    from claude_on_the_fly.slack import SlackFrontend

    front = object.__new__(SlackFrontend)
    front._pending_msg = {LARGE_CHAT: deque([("D1", "102.0")])}
    front._pending_reply_suppressed = {LARGE_CHAT: deque([False])}
    front._in_flight = {}
    front.notify_complete = AsyncMock()
    await front.retire_failed_turn(LARGE_CHAT, {"channel": "D1"})
    assert list(front._pending_msg[LARGE_CHAT]) == [("D1", "102.0")]
    assert list(front._pending_reply_suppressed[LARGE_CHAT]) == [False]
    front.notify_complete.assert_not_awaited()


def test_intermittent_read_failure_cannot_erase_previous_recovery(
    tmp_path, monkeypatch
):
    journal = TurnJournal(tmp_path / "slack.turns.json")
    item = pending(journal)
    assert journal.park(replace(item, turn_id="previous"))
    read_text = Path.read_text
    reads = 0

    def intermittent_read(path, *args, **kwargs):
        nonlocal reads
        if path == journal.recovery_path:
            reads += 1
            if reads > 1:
                raise OSError("recovery inventory became unreadable")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", intermittent_read)
    assert journal.park(item)
    rows = json.loads(read_text(journal.recovery_path))
    assert {row["turn_id"] for row in rows} == {"previous", "original"}
