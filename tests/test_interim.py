"""Tests for claude_on_the_fly.interim — coalescing, rate limiting, shutdown."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import pytest

from claude_on_the_fly import interim as interim_mod
from claude_on_the_fly.interim import (
    InterimProgress,
    interim_min_gap_seconds,
    interim_progress_enabled,
    interim_warmup_seconds,
)

_LOGGER = "claude_on_the_fly.interim"


@pytest.fixture
async def make_relay(monkeypatch):
    """Build an InterimProgress and guarantee its drain task is stopped.

    The constructor starts a task, so a test that forgot to stop one would leave
    it pending past the end of the loop.

    Every setting the feature reads is cleared first, the way
    `TestInterimProgressSetting` does for the toggle: the relay reads its pacing
    from the environment per turn, so on a machine that actually runs this
    feature the operator's own warm-up and gap would otherwise decide what these
    tests see. Clearing here rather than per test because the constructor is
    where the read happens, and that is this fixture. The other half of the
    lookup — `~/.claude-on-the-fly/config.yaml` — is already out of reach: HOME
    is redirected in `conftest.py` before the package is imported, and
    `agent.DATA_DIR` binds it there.
    """
    for var in (
        "COTF_INTERIM_PROGRESS",
        "COTF_INTERIM_WARMUP_SECONDS",
        "COTF_INTERIM_MIN_GAP_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    made: list[InterimProgress] = []

    def _make(
        send: Callable[[str], Awaitable[None]],
        now: Callable[[], float] = time.monotonic,
    ) -> InterimProgress:
        relay = InterimProgress(send, now=now)
        made.append(relay)
        return relay

    yield _make

    for relay in made:
        relay.cancel()
    await asyncio.sleep(0)


class TestInterimProgressSetting:
    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv("COTF_INTERIM_PROGRESS", raising=False)
        assert interim_progress_enabled() is False

    def test_true_is_on(self, monkeypatch):
        monkeypatch.setenv("COTF_INTERIM_PROGRESS", "true")
        assert interim_progress_enabled() is True

    def test_junk_is_off(self, monkeypatch):
        """Anything unrecognised is off — a typo must not turn a feature on."""
        monkeypatch.setenv("COTF_INTERIM_PROGRESS", "maybe")
        assert interim_progress_enabled() is False

    def test_yaml_false_is_off(self, monkeypatch):
        """`_flatten` maps YAML false to "0", which is what the reader sees."""
        monkeypatch.setenv("COTF_INTERIM_PROGRESS", "0")
        assert interim_progress_enabled() is False


class TestInterimPacingSettings:
    """The warm-up and the gap are pacing policy, so they are operator-settable
    the way `agent.auto_compact_pct` is — and the defaults leave behaviour
    exactly where the constants left it."""

    def test_unset_uses_the_defaults(self, monkeypatch):
        monkeypatch.delenv("COTF_INTERIM_WARMUP_SECONDS", raising=False)
        monkeypatch.delenv("COTF_INTERIM_MIN_GAP_SECONDS", raising=False)
        assert interim_warmup_seconds() == interim_mod.DEFAULT_INTERIM_WARMUP_S
        assert interim_min_gap_seconds() == interim_mod.DEFAULT_INTERIM_MIN_GAP_S
        assert interim_mod.DEFAULT_INTERIM_WARMUP_S == 300.0
        assert interim_mod.DEFAULT_INTERIM_MIN_GAP_S == 300.0

    def test_a_configured_value_is_used(self, monkeypatch):
        monkeypatch.setenv("COTF_INTERIM_WARMUP_SECONDS", "30")
        monkeypatch.setenv("COTF_INTERIM_MIN_GAP_SECONDS", "12.5")
        assert interim_warmup_seconds() == 30.0
        assert interim_min_gap_seconds() == 12.5

    def test_junk_falls_back_to_the_default_and_says_so(self, monkeypatch, caplog):
        """A typo must not take the daemon down, and must not look like a working
        setting either."""
        monkeypatch.setenv("COTF_INTERIM_WARMUP_SECONDS", "five minutes")
        with caplog.at_level("WARNING", logger=_LOGGER):
            assert interim_warmup_seconds() == interim_mod.DEFAULT_INTERIM_WARMUP_S
        assert "is not a number;" in caplog.text

    @pytest.mark.parametrize("raw", ["-1", "-0.5", "nan"])
    def test_a_value_below_zero_falls_back_the_same_way_junk_does(
        self, monkeypatch, caplog, raw
    ):
        """`float()` accepts all three, and none of them is a pacing policy: a
        negative gap makes the limiter's comparison false forever, and `nan`
        compares false against everything, which is the same thing. The limiter
        is what bounds message volume on a path that does not count against the
        reply budget, so this is not a cosmetic parse."""
        monkeypatch.setenv("COTF_INTERIM_WARMUP_SECONDS", raw)
        monkeypatch.setenv("COTF_INTERIM_MIN_GAP_SECONDS", raw)
        with caplog.at_level("WARNING", logger=_LOGGER):
            assert interim_warmup_seconds() == interim_mod.DEFAULT_INTERIM_WARMUP_S
            assert interim_min_gap_seconds() == interim_mod.DEFAULT_INTERIM_MIN_GAP_S
        # Not "is not a number": that is the junk branch's wording, and asserting
        # on a substring of it would pass whichever branch ran.
        assert "is not a pacing value >= 0" in caplog.text

    def test_zero_is_a_setting_not_a_fallback(self, monkeypatch):
        """`0` is documented for both keys — no warm-up, and no gap — so it must
        not be swept up with the empty string."""
        monkeypatch.setenv("COTF_INTERIM_WARMUP_SECONDS", "0")
        monkeypatch.setenv("COTF_INTERIM_MIN_GAP_SECONDS", "0")
        assert interim_warmup_seconds() == 0.0
        assert interim_min_gap_seconds() == 0.0

    async def test_a_negative_gap_still_leaves_the_limiter_bounding_volume(
        self, make_relay, monkeypatch
    ):
        """The harm a rejected negative prevents, at the layer it would happen:
        with `-1` honoured the gap comparison could never be true again and every
        line would post."""
        monkeypatch.setenv("COTF_INTERIM_WARMUP_SECONDS", "0")
        monkeypatch.setenv("COTF_INTERIM_MIN_GAP_SECONDS", "-1")
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        relay.emit("first")
        await asyncio.sleep(0)
        relay.emit("held by the default gap")
        await asyncio.sleep(0)

        assert sent == ["first"]
        assert relay._min_gap == interim_mod.DEFAULT_INTERIM_MIN_GAP_S

    async def test_the_relay_reads_them_per_turn(self, make_relay, monkeypatch):
        """Not bound at import — one turn is one InterimProgress, so a config edit
        lands on the next turn without a restart."""
        monkeypatch.setenv("COTF_INTERIM_WARMUP_SECONDS", "0")
        monkeypatch.setenv("COTF_INTERIM_MIN_GAP_SECONDS", "0")
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        relay.emit("straight away")
        await asyncio.sleep(0)

        assert sent == ["straight away"]


class TestInterimProgress:
    async def test_nothing_is_posted_before_the_warm_up(self, make_relay):
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        relay.emit("a")
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S - 1
        relay.emit("b")
        await asyncio.sleep(0)

        assert sent == []
        assert relay._buffer == ["a", "b"]

    async def test_the_first_post_coalesces_everything_held(self, make_relay):
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        relay.emit("one")
        relay.emit("two")
        relay.emit("three")
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("four")
        await asyncio.sleep(0)

        assert sent == ["one\ntwo\nthree\nfour"]

    async def test_a_line_inside_the_gap_is_held(self, make_relay):
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("first")
        await asyncio.sleep(0)
        clock[0] += interim_mod.DEFAULT_INTERIM_MIN_GAP_S - 1
        relay.emit("inside the gap")
        await asyncio.sleep(0)

        assert sent == ["first"]
        assert relay._buffer == ["inside the gap"]

    async def test_the_held_line_goes_out_once_the_gap_elapses(self, make_relay):
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("first")
        await asyncio.sleep(0)
        clock[0] += interim_mod.DEFAULT_INTERIM_MIN_GAP_S - 1
        relay.emit("held")
        clock[0] += 2
        relay.emit("new")
        await asyncio.sleep(0)

        assert sent == ["first", "held\nnew"]

    async def test_the_ticker_posts_a_line_that_arrived_before_the_warm_up(
        self, make_relay, monkeypatch
    ):
        """The headline regression: a turn that narrates once and then vanishes
        into a single long tool call never re-enters `emit`, so only the passage
        of time can release its line."""
        monkeypatch.setattr(interim_mod, "INTERIM_TICK_S", 0.001)
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        relay.emit("halfway")
        await asyncio.sleep(0.01)
        assert sent == []

        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        await asyncio.sleep(0.01)

        assert sent == ["halfway"]

    async def test_a_tick_before_the_warm_up_posts_nothing(
        self, make_relay, monkeypatch
    ):
        monkeypatch.setattr(interim_mod, "INTERIM_TICK_S", 0.001)
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        relay.emit("too early")
        await asyncio.sleep(0.01)

        assert sent == []

    async def test_an_idle_tick_with_an_empty_buffer_does_nothing(
        self, make_relay, monkeypatch
    ):
        """The commonest runtime path: a turn that never narrates at all."""
        monkeypatch.setattr(interim_mod, "INTERIM_TICK_S", 0.001)
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        await asyncio.sleep(0.01)

        assert sent == []

    async def test_a_tick_after_close_posts_nothing(self, make_relay, monkeypatch):
        """End-to-end: nothing lands after the reply, whichever guard stops it."""
        monkeypatch.setattr(interim_mod, "INTERIM_TICK_S", 0.001)
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        relay.emit("held")
        await relay.aclose()
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        await asyncio.sleep(0.01)

        assert sent == []

    async def test_the_buffer_drops_the_oldest_when_full(
        self, make_relay, monkeypatch, caplog
    ):
        """The cap runs after every append, so the buffer never exceeds the max:
        a,b,c -> [b,c], then d -> [c,d]."""
        monkeypatch.setattr(interim_mod, "INTERIM_BUFFER_MAX", 2)
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        with caplog.at_level("WARNING", logger=_LOGGER):
            relay.emit("a")
            relay.emit("b")
            relay.emit("c")
            clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
            relay.emit("d")
            await asyncio.sleep(0)

        assert sent == ["c\nd"]
        assert "dropped the oldest" in caplog.text

    async def test_an_overlong_line_is_capped_before_it_is_held(
        self, make_relay, monkeypatch
    ):
        monkeypatch.setattr(interim_mod, "INTERIM_LINE_MAX_CHARS", 10)
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        relay.emit("x" * 40)
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("y")
        await asyncio.sleep(0)

        assert sent == ["xxxxxxxxxx […]\ny"]

    async def test_an_overlong_digest_is_trimmed_from_the_front(
        self, make_relay, monkeypatch
    ):
        """Newest wins, so the trim agrees with the buffer's drop-oldest rule.

        The cap is patched well clear of the marker's own length, because the
        marker is part of what has to fit: budget it and four lines go, ignore it
        and the loop stops after one and ships 83 characters against a cap of 60.
        """
        monkeypatch.setattr(interim_mod, "INTERIM_MESSAGE_MAX_CHARS", 60)
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        for line in ("aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc", "dddddddddd"):
            relay.emit(line)
        relay.emit("eeeeeeeeee")
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("ffffffffff")
        await asyncio.sleep(0)

        assert sent == ["[…4 earlier line(s) omitted]\neeeeeeeeee\nffffffffff"]
        assert len(sent[0]) <= interim_mod.INTERIM_MESSAGE_MAX_CHARS

    async def test_a_trimmed_digest_says_so_in_the_message_and_in_the_log(
        self, make_relay, monkeypatch, caplog
    ):
        """Both neighbouring size policies announce themselves, and this one is
        reached at ordinary narration lengths rather than pathological ones — so a
        reader has to be able to tell that the count they can see is not all of
        them.

        And the message that carries the marker is still inside the cap: a marker
        prepended after the trim loop is a marker nothing budgeted for, which
        breaks the very limit the trim was enforcing.
        """
        monkeypatch.setattr(interim_mod, "INTERIM_MESSAGE_MAX_CHARS", 60)
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        with caplog.at_level("WARNING", logger=_LOGGER):
            for line in ("aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc", "dddddddddd"):
                relay.emit(line)
            relay.emit("eeeeeeeeee")
            clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
            relay.emit("ffffffffff")
            await asyncio.sleep(0)

        assert len(sent) == 1
        assert sent[0].startswith("[…4 earlier line(s) omitted]\n")
        assert len(sent[0]) <= interim_mod.INTERIM_MESSAGE_MAX_CHARS
        assert "dropped the 4 oldest line(s)" in caplog.text

    async def test_an_untrimmed_digest_carries_no_marker(self, make_relay):
        """The marker is guarded on a trim having happened: the common case is a
        digest well inside the cap, and a "0 omitted" line would be noise on it."""
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        relay.emit("aaa")
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("bbb")
        await asyncio.sleep(0)

        assert sent == ["aaa\nbbb"]

    async def test_a_single_line_over_the_message_cap_is_still_sent(
        self, make_relay, monkeypatch
    ):
        """The trim loop stops at one line rather than eating the last one.

        Both caps are patched: with the real values a held line is already capped
        well under the message cap, so no single line can exceed it.
        """
        monkeypatch.setattr(interim_mod, "INTERIM_MESSAGE_MAX_CHARS", 12)
        monkeypatch.setattr(interim_mod, "INTERIM_LINE_MAX_CHARS", 40)
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("z" * 30)
        await asyncio.sleep(0)

        assert sent == ["z" * 30]

    async def test_a_dropped_message_does_not_start_the_gap(
        self, make_relay, monkeypatch
    ):
        """A dropped message must not also silence the next gap."""
        monkeypatch.setattr(interim_mod, "INTERIM_QUEUE_MAX", 1)
        monkeypatch.setattr(interim_mod, "DEFAULT_INTERIM_MIN_GAP_S", 0.0)
        sent: list[str] = []
        clock = [0.0]
        gate = asyncio.Event()

        async def send(text: str) -> None:
            sent.append(text)
            await gate.wait()

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("first")
        await asyncio.sleep(0)
        relay.emit("second")
        posted_at = relay._last_post
        assert posted_at is not None
        clock[0] += 1
        relay.emit("third")

        assert relay._last_post == posted_at
        gate.set()

    async def test_a_full_queue_drops_rather_than_blocks(
        self, make_relay, monkeypatch, caplog
    ):
        monkeypatch.setattr(interim_mod, "INTERIM_QUEUE_MAX", 1)
        monkeypatch.setattr(interim_mod, "DEFAULT_INTERIM_MIN_GAP_S", 0.0)
        sent: list[str] = []
        clock = [0.0]
        gate = asyncio.Event()

        async def send(text: str) -> None:
            sent.append(text)
            await gate.wait()

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        with caplog.at_level("WARNING", logger=_LOGGER):
            relay.emit("first")
            await asyncio.sleep(0)
            relay.emit("second")
            relay.emit("third")

        assert sent == ["first"]
        assert "dropping a progress message" in caplog.text
        gate.set()

    async def test_a_failing_post_does_not_stop_the_next_one(
        self, make_relay, monkeypatch, caplog
    ):
        monkeypatch.setattr(interim_mod, "DEFAULT_INTERIM_MIN_GAP_S", 0.0)
        calls: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("slack said no")

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        with caplog.at_level("ERROR", logger=_LOGGER):
            relay.emit("one")
            await asyncio.sleep(0)
            relay.emit("two")
            await asyncio.sleep(0)

        assert calls == ["one", "two"]
        assert "could not post" in caplog.text

    async def test_emit_after_close_is_ignored(self, make_relay):
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        await relay.aclose()
        relay.emit("late")
        await asyncio.sleep(0)

        assert sent == []
        assert relay._buffer == []

    async def test_close_is_idempotent(self, make_relay):
        sent: list[str] = []

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send)
        await relay.aclose()
        await relay.aclose()

        assert sent == []

    async def test_close_discards_what_is_still_held(self, make_relay, caplog):
        """The reply lands next in the same thread; a digest above it is noise."""
        sent: list[str] = []

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send)
        with caplog.at_level("DEBUG", logger=_LOGGER):
            relay.emit("a")
            relay.emit("b")
            await relay.aclose()

        assert sent == []
        assert "dropped 2 held progress line(s)" in caplog.text

    async def test_close_with_flush_posts_what_is_held_inside_the_gap(self, make_relay):
        """The error path has no reply body to duplicate against, so the last
        thing the agent said goes out — bypassing the GAP. The clock is past the
        warm-up but no gap has elapsed, which is what the limiter would refuse."""
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("a")
        await asyncio.sleep(0)
        assert sent == ["a"]

        relay.emit("b")
        await relay.aclose(flush=True)

        assert sent == ["a", "b"]

    async def test_close_with_flush_before_the_warm_up_posts_nothing(
        self, make_relay, caplog
    ):
        """A turn that fails in three seconds would otherwise push a progress
        message immediately above its own error message. Failing does not make a
        fast turn slow."""
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        with caplog.at_level("DEBUG", logger=_LOGGER):
            relay.emit("a")
            clock[0] = 3.0
            await relay.aclose(flush=True)

        assert sent == []
        assert relay._buffer == []
        assert "dropped 1 held progress line(s)" in caplog.text

    async def test_close_with_flush_and_an_empty_buffer_posts_nothing(self, make_relay):
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        await relay.aclose(flush=True)

        assert sent == []

    async def test_close_gives_up_on_a_hanging_post(
        self, make_relay, monkeypatch, caplog
    ):
        monkeypatch.setattr(interim_mod, "INTERIM_CLOSE_GRACE", 0.01)
        sent: list[str] = []
        clock = [0.0]
        gate = asyncio.Event()

        async def send(text: str) -> None:
            sent.append(text)
            await gate.wait()

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.emit("stuck")
        await asyncio.sleep(0)
        assert sent == ["stuck"]

        with caplog.at_level("WARNING", logger=_LOGGER):
            await relay.aclose()

        assert "giving up" in caplog.text
        # cancel() only *requests* cancellation; without yielding the task is
        # still PENDING and the assertion below would be vacuous.
        await asyncio.sleep(0)
        assert relay._task.done()
        gate.set()

    async def test_cancel_stops_the_drain_without_awaiting(self, make_relay):
        sent: list[str] = []
        clock = [0.0]

        async def send(text: str) -> None:
            sent.append(text)

        relay = make_relay(send, now=lambda: clock[0])
        clock[0] = interim_mod.DEFAULT_INTERIM_WARMUP_S + 1
        relay.cancel()
        await asyncio.sleep(0)

        assert relay._task.done()
        relay.emit("after")
        await asyncio.sleep(0)
        assert sent == []
