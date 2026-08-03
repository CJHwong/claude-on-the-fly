"""One turn's mid-turn progress: coalescing, rate limiting, and delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from claude_on_the_fly import settings

logger = logging.getLogger(__name__)

# Forward the agent's own mid-turn narration into the conversation while the turn
# runs. Off unless explicitly on: a 40-minute turn is silent today, and this is
# the fix, but it also posts more messages into somebody's thread than they asked
# for, so it is opted into. Read per turn (see `_seconds` for why nothing here
# binds a setting at import).
INTERIM_PROGRESS_VAR = "COTF_INTERIM_PROGRESS"
INTERIM_WARMUP_VAR = "COTF_INTERIM_WARMUP_SECONDS"
INTERIM_MIN_GAP_VAR = "COTF_INTERIM_MIN_GAP_SECONDS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
# Nothing is posted until a turn has run this long, and then at most one message
# per gap, with everything produced in between coalesced into it. Settings rather
# than constants because both are pacing policy — how long somebody tolerates
# silence, and how often they want interrupting — which is a per-deployment
# answer in the way `agent.auto_compact_pct` is, not a property of the mechanism
# (the caps and the tick below are). The defaults make a turn that finishes
# inside five minutes post nothing at all, which is the point: such a turn never
# needed telling that it was alive. A longer one gets one coalesced digest per
# gap, so the cost of a slow turn is bounded by its duration rather than by how
# much the agent happened to say.
DEFAULT_INTERIM_WARMUP_S = 300.0
DEFAULT_INTERIM_MIN_GAP_S = 300.0
# Narration lines held between posts before the OLDEST is dropped, and the largest
# single line that may be held. The producer is a stdout reader that must never
# block, and the limiter can hold the buffer for minutes, so BOTH dimensions need
# a bound: a line count alone bounds nothing in bytes.
INTERIM_BUFFER_MAX = 20
INTERIM_LINE_MAX_CHARS = 500
# Largest coalesced message handed to a frontend. Owned here rather than deferred
# to the adapter because trimming is a POLICY question — which end gets cut — and
# the answer has to agree with the buffer's: newest wins, so whole lines go from
# the FRONT. The adapter's own per-message limit still applies underneath and is
# untouched; it is the last resort, not the mechanism.
INTERIM_MESSAGE_MAX_CHARS = 2000
# Coalesced messages awaiting delivery before the newest is dropped. Small,
# because the limiter already caps delivery at one message per gap: a backlog of
# more than a couple here means Slack is failing, not that the agent is chatty.
INTERIM_QUEUE_MAX = 8
# How often the drain task wakes when nothing has arrived. Without it the limiter
# is driven only by new text ARRIVING, so a turn that narrates once and then sits
# for half an hour inside a single tool call re-enters `emit` never, and posts
# nothing at all — the exact silent turn this feature exists to fix. The tick only
# bounds how LATE a due post can be, never how often one goes out, so it is
# deliberately far smaller than the gap above; it costs one wakeup per 15s per
# in-flight turn, cheaper than the typing indicator already running beside it.
INTERIM_TICK_S = 15.0
# How long `aclose` waits for an in-flight post before giving up on it.
INTERIM_CLOSE_GRACE = 5.0


def interim_progress_reads_as_on(raw: str) -> bool:
    """Whether a raw setting value turns the feature on. Anything else is off.

    A pure function over the string, so the two readers of this ONE setting
    cannot drift: `interim_progress_enabled` asks `settings`, while
    `checks._check_interim_progress` asks the env mapping it was handed and must
    stay pure to do it. Spelling the truthy set in both places is how a widening
    here would leave the doctor reporting "off" for a value the runtime honours.
    """
    return raw.strip().lower() in _TRUTHY


def interim_progress_enabled() -> bool:
    """Whether this turn forwards mid-turn narration. Anything unrecognised is off."""
    return interim_progress_reads_as_on(settings.get(INTERIM_PROGRESS_VAR))


def _seconds(name: str, fallback: float) -> float:
    """A pacing policy in seconds, read per turn rather than bound at import.

    Bound at import it could not see a value `load_dotenv()` put in the
    environment afterwards, and could not see a config-file edit at all. A junk
    value falls back to the default and says so once: pacing is not something
    worth refusing to serve a message over, and a typo that silently changed the
    pacing would look exactly like a working setting.

    A negative is junk of the same kind and takes the same path. `-1` is not a
    smaller gap, it is no gap at all: the limiter's comparison would never be
    true again, and the limiter is the only thing bounding how many messages a
    turn puts in somebody's thread on a path that deliberately does not count
    against the reply budget. Written `not (value >= 0)` rather than `value < 0`
    so a `nan` — which compares False against everything, and would disable the
    limiter in exactly the same way — is rejected by the same statement.
    """
    raw = settings.get(name).strip()
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %.0fs", name, raw, fallback)
        return fallback
    if not (value >= 0):
        # Deliberately not a superstring of the message above: a test asserting
        # on the junk wording would otherwise pass on this branch too.
        logger.warning(
            "%s=%r is not a pacing value >= 0; using %.0fs", name, raw, fallback
        )
        return fallback
    return value


def interim_warmup_seconds() -> float:
    """How long a turn runs before its first progress message may go out."""
    return _seconds(INTERIM_WARMUP_VAR, DEFAULT_INTERIM_WARMUP_S)


def interim_min_gap_seconds() -> float:
    """The shortest gap between two progress messages in one turn."""
    return _seconds(INTERIM_MIN_GAP_VAR, DEFAULT_INTERIM_MIN_GAP_S)


def _omitted_marker(dropped: int) -> str:
    """The line a trimmed digest is prefixed with.

    Its own function because its length has to be budgeted for BEFORE it is
    written, and a marker built at the point it is prepended is a marker nothing
    measured.
    """
    return f"[…{dropped} earlier line(s) omitted]"


def _digest_length(lines: list[str], dropped: int) -> int:
    """How long the finished message would be, the marker included.

    `len(x) + 1` per part counts one newline too many (n parts are joined by
    n - 1 of them), which is the safe direction to be wrong about a cap.
    """
    total = sum(len(x) + 1 for x in lines)
    if dropped:
        total += len(_omitted_marker(dropped)) + 1
    return total


class InterimProgress:
    """One turn's progress: coalesced, rate-limited, and posted without ever
    stalling the stream reader.

    `emit` is called from inside the agent's stdout loop, so it is synchronous and
    only ever buffers or enqueues: a slow post must not hold up reading, and a
    failing one must not end the turn. A single drain task keeps the messages in
    the order the agent produced them, which a task-per-message would not.

    The buffer is released by EITHER of two things — new text arriving (`emit`) or
    the drain task's tick (`INTERIM_TICK_S`). Arrival alone would strand a turn
    that narrates once and then disappears into a single long tool call, which is
    the very turn this exists for; the tick is what makes the limiter a function
    of time rather than of chattiness.

    The rate limiter lives here rather than in the frontend adapter because it is
    policy, not rendering: how often a person wants to be interrupted is the same
    question on every platform, while "make this look like machine progress" is
    the platform's own. `now` is injected so the tests can move time without
    sleeping.
    """

    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send = send
        self._now = now
        self._start = now()
        # Read once per turn, beside `_start`, because they are read together and
        # a turn's pacing must not change underneath it half way through. The
        # turn is also the unit the two settings are documented in.
        self._warmup = interim_warmup_seconds()
        self._min_gap = interim_min_gap_seconds()
        self._last_post: float | None = None
        self._buffer: list[str] = []
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=INTERIM_QUEUE_MAX)
        self._closed = False
        self._task = asyncio.create_task(self._drain())

    def emit(self, text: str) -> None:
        """Hold one narration line, and release the whole buffer if it is due.

        Never blocks, never raises: it runs inside the agent's stdout read loop.

        The line is capped before it is held. The buffer bounds how MANY lines
        wait, which bounds nothing at all in bytes on its own — one narration
        block can be tens of thousands of characters, and the limiter may hold it
        for minutes.
        """
        if self._closed:
            return
        if len(text) > INTERIM_LINE_MAX_CHARS:
            text = text[:INTERIM_LINE_MAX_CHARS] + " […]"
        self._buffer.append(text)
        if len(self._buffer) > INTERIM_BUFFER_MAX:
            # Drop the OLDEST: what the agent is doing now is what a waiting
            # person wants. `_post_buffer` trims from the front for the same
            # reason, so both size policies agree that the newest survives.
            self._buffer.pop(0)
            logger.warning(
                "interim: more than %d lines held, dropped the oldest",
                INTERIM_BUFFER_MAX,
            )
        self._flush_if_due()

    def _flush_if_due(self) -> None:
        """Release the buffer if the limiter allows.

        The single home of the due-check, because it has two callers and they are
        driven by different things: `emit`, when new text ARRIVES, and `_drain`'s
        tick, when TIME passes. Arrival alone is not enough — a turn that narrates
        once and then spends half an hour inside one tool call calls `emit` exactly
        once, before the warm-up, and without the ticker would post nothing at all.

        Both callers run on the same event loop and neither awaits anywhere inside
        this method, so the two cannot interleave mid-call and no lock is needed.
        Stated because it is the first question a reader will have.

        Guarded on `_closed` so a tick that fires while `aclose` is waiting on the
        queue cannot enqueue a message that would land after the reply. The error
        path's deliberate flush calls `_post_buffer` directly and is, correctly,
        not subject to that guard.
        """
        if self._closed or not self._buffer:
            return
        if not self._warmed_up():
            return
        now = self._now()
        if self._last_post is not None and now - self._last_post < self._min_gap:
            return
        self._post_buffer()

    def _warmed_up(self) -> bool:
        """Whether this turn has run long enough to be worth reporting on.

        Its own predicate because two paths ask the question for the same reason:
        the limiter, and the error path's flush. A turn that fails in three
        seconds is no more in need of a progress message than one that succeeds
        in three seconds.
        """
        return self._now() - self._start >= self._warmup

    def _post_buffer(self) -> None:
        """Enqueue everything held as ONE message. Sync; drops rather than blocks.

        Trims from the FRONT, by whole lines, so the two size policies agree on
        newest-wins: the buffer drops its OLDEST line on overflow because what the
        agent is doing now is what a waiting person wants — and a frontend that
        truncates at the tail would then cut off exactly the lines that policy
        just protected. Doing it here, in whole lines, keeps the frontend's own
        limit untouched and leaves it as a backstop rather than the mechanism.

        A trim says so twice — a WARNING in the log and a marker on the message
        itself — because at ordinary narration lengths a full buffer routinely
        exceeds the cap, so this is a normal path and not a pathological one, and
        because both neighbouring size policies already announce themselves:
        `emit` marks a line it capped and warns about a line it dropped. A reader
        who cannot tell that something was cut has no reason to go looking for it.

        The marker is budgeted for INSIDE the loop rather than prepended after
        it: a marker added to a message already trimmed to the cap puts it back
        over the cap, which is the one thing this method exists to prevent. The
        cap therefore holds whenever the marker fits — the `len(lines) > 1` guard
        is deliberately stronger, so a single line plus its marker still ships
        rather than the trim eating the last thing the agent said.
        """
        if not self._buffer:
            return
        lines = self._buffer
        self._buffer = []
        dropped = 0
        while (
            len(lines) > 1
            and _digest_length(lines, dropped) > INTERIM_MESSAGE_MAX_CHARS
        ):
            lines.pop(0)
            dropped += 1
        if dropped:
            logger.warning(
                "interim: digest over %d chars, dropped the %d oldest line(s)",
                INTERIM_MESSAGE_MAX_CHARS,
                dropped,
            )
            lines.insert(0, _omitted_marker(dropped))
        text = "\n".join(lines)
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            logger.warning(
                "interim: %d messages behind, dropping a progress message",
                INTERIM_QUEUE_MAX,
            )
            return
        # Only after a successful enqueue: a dropped message must not also silence
        # the next gap, which is exactly when a person is most in need of hearing
        # something. That covers the ONE drop this method can see. Delivery can
        # still fail further down — `Frontend.send_progress` returns silently on
        # four of its own conditions, and the drain swallows a raising post — and
        # none of them reach back here, so a transient failure does cost a full
        # gap of silence. Left as is deliberately: reporting delivery back would
        # put an outcome contract on every frontend for a message the ABC defines
        # as best-effort.
        self._last_post = self._now()

    async def _drain(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(self._queue.get(), timeout=INTERIM_TICK_S)
            except TimeoutError:
                # Nothing arrived this tick — but time passed, and time is the
                # other thing that can make the buffer due. This is the only path
                # that releases a line for a turn which narrated early and then
                # went quiet inside one long tool call.
                self._flush_if_due()
                continue
            try:
                await self._send(text)
            except Exception:
                # One failed post must not end progress for the rest of the turn,
                # and must never surface as the turn's own failure.
                logger.exception("interim: could not post a progress message")
            finally:
                self._queue.task_done()

    async def aclose(self, *, flush: bool = False) -> None:
        """Stop accepting, settle what is held, post what is queued, stop. Idempotent.

        `flush=False` — the normal path — **discards** whatever is still held. The
        reply is about to arrive in the same thread, and a progress digest posted
        immediately above it is pure noise; the held lines are also not in the
        reply, so nothing is duplicated either way.

        `flush=True` is the error path, and what it releases is precisely what
        the rate limiter was still HOLDING: narration this class received and the
        gap withheld. There is no reply body for it to duplicate against, so it
        goes out, bypassing the GAP — bounded, because the turn is over.

        It is not "everything the agent said". A text block the stream has not
        yet proved to be narration is held one layer up, in `InterimRelay`
        (`agent.py`), which never reaches `emit` and is discarded when the turn
        raises. So a turn whose last words came after its final tool call flushes
        nothing, and flushing those too would mean flushing at each raise site.

        It does NOT bypass the warm-up. A turn that fails after three seconds
        would otherwise push a progress message immediately above its own error
        message: two notifications where one is the answer, for a turn that was
        never slow enough to need telling anyone it was alive. The warm-up is the
        one thing deciding whether a turn is worth reporting on at all, and
        failing does not make a fast turn slow.

        Graceful rather than a bare cancel because a cancelled `chat_postMessage`
        can leave a message in Slack whose ts was never recorded, and an
        unrecorded post of ours is exactly what the echo guard exists to stop.
        Bounded, because the reply is waiting behind it.
        """
        if self._closed:
            return
        self._closed = True
        if flush and self._warmed_up():
            self._post_buffer()
        elif self._buffer:
            logger.debug(
                "interim: dropped %d held progress line(s); the turn's own "
                "message is next",
                len(self._buffer),
            )
            self._buffer.clear()
        try:
            await asyncio.wait_for(self._queue.join(), timeout=INTERIM_CLOSE_GRACE)
        except TimeoutError:
            # qsize() + 1, not qsize(): the message we are actually losing is the
            # one the drain has already dequeued and is stuck sending, so it is no
            # longer counted. Logging a bare qsize() prints "0 unposted" at the
            # exact moment one is lost — a diagnostic that lies when it matters.
            logger.warning(
                "interim: giving up on ~%d unposted progress message(s)",
                self._queue.qsize() + 1,
            )
        self._task.cancel()

    def cancel(self) -> None:
        """Stop without waiting: two assignments and a `task.cancel()`, no await.

        Used on the abort path because it is CHEAP, not because awaiting there
        would be unsafe — `aclose()` on a cancelling task is fine, it just costs
        up to INTERIM_CLOSE_GRACE, and $stop is somebody waiting for an ack.
        """
        self._closed = True
        self._task.cancel()
