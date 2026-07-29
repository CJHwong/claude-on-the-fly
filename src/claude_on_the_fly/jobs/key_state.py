"""Per-key memory of what a producer has already tried.

A polling producer re-derives the same work on every fire, and the queue only
knows what is outstanding *right now*. Two questions therefore have no answer in
the queue:

- "this key failed, how long should I wait before trying again?" Without an
  answer, a key whose runs fail keeps being re-enqueued at the poll interval, so
  a revoked credential or a rate limit turns into a tight retry loop.
- "this key has run three times and nothing about it changed, is it stuck?"
  Without an answer, an item the agent cannot advance is worked forever, because
  the query that produced it keeps producing it.

So each key gets one small file recording its fingerprint, how many fires it has
had since that fingerprint last moved, and its failure streak.

The fingerprint is a hash of the item the producer emitted. That is a deliberate
generalization of the timestamp comparison this replaces: it needs no `updated`
field from the source and works for any producer, at the cost of putting the
burden on the emitted object. **A producer must emit a field that moves when the
work moves** — the agent commenting on a ticket has to change what the poller
emits for that ticket, or the fingerprint will not notice progress and the key
parks after `max_fires`.

Layout, one file per key, mirroring the queue's root:

    <root>/keys/<entry>__<item>.json

Writes are tmp-then-`os.replace`, so a reader never sees half a record and a
crash mid-write leaves the previous one intact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_on_the_fly.jobs.keys import safe_segment, split_key

logger = logging.getLogger(__name__)

# First retry waits this long, then doubles. Same shape and base as the daemon
# this replaces, so a failing key backs off on a familiar curve.
BASE_BACKOFF_S = 10.0
DEFAULT_MAX_BACKOFF_S = 300.0
# Fires against an unchanged fingerprint before a key is parked. Matches the
# `max_no_progress_turns` default it stands in for.
DEFAULT_MAX_FIRES = 3


@dataclass
class KeyState:
    """What one key has done so far. Absent file means a fresh key."""

    fingerprint: str = ""
    fires_since_change: int = 0
    failures: int = 0
    last_failed_at: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "fires_since_change": self.fires_since_change,
            "failures": self.failures,
            "last_failed_at": self.last_failed_at,
            "extras": self.extras,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> KeyState:
        return cls(
            fingerprint=str(raw.get("fingerprint") or ""),
            fires_since_change=int(raw.get("fires_since_change") or 0),
            failures=int(raw.get("failures") or 0),
            last_failed_at=float(raw.get("last_failed_at") or 0.0),
            extras=dict(raw.get("extras") or {}),
        )


def fingerprint(item: dict[str, Any]) -> str:
    """A stable hash of the item a producer emitted.

    `sort_keys` so a producer that reorders its JSON does not read as progress,
    and `default=str` so an unexpected non-JSON value degrades to something
    hashable instead of raising in the middle of a fire.
    """
    encoded = json.dumps(item, sort_keys=True, default=str).encode()
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def backoff_s(failures: int, max_backoff_s: float = DEFAULT_MAX_BACKOFF_S) -> float:
    """Seconds to wait after `failures` consecutive failures, capped."""
    if failures <= 0:
        return 0.0
    return min(BASE_BACKOFF_S * (2 ** (failures - 1)), max_backoff_s)


class KeyStateStore:
    """Reads and writes the per-key files under `root`."""

    def __init__(self, root: Path) -> None:
        self._dir = root / "keys"

    @property
    def dir(self) -> Path:
        return self._dir

    def _path(self, key: str) -> Path:
        entry, item = split_key(key)
        return self._dir / f"{safe_segment(entry)}__{safe_segment(item)}.json"

    def load(self, key: str) -> KeyState:
        """This key's record, or a fresh one when it is missing or unreadable.

        A corrupt record reads as fresh rather than raising: the cost is one
        unnecessary run, and the alternative is a producer that cannot fire at all
        until somebody deletes a file by hand.
        """
        path = self._path(key)
        try:
            return KeyState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return KeyState()
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "jobs: key state for %s unreadable (%s); treating as fresh", key, exc
            )
            return KeyState()

    def save(self, key: str, state: KeyState) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, path)

    def should_skip(
        self,
        key: str,
        item_fingerprint: str,
        *,
        max_fires: int = DEFAULT_MAX_FIRES,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        now: float | None = None,
    ) -> str | None:
        """Why this key should not be enqueued now, or None to go ahead.

        Returns a reason string rather than a bool so the caller can log *which*
        gate held the key back: a silent skip is indistinguishable from a producer
        that found nothing, which is the hardest kind of "why did this never run"
        to answer.
        """
        state = self.load(key)
        moved = state.fingerprint != item_fingerprint
        clock = time.time() if now is None else now

        if state.failures > 0 and not moved:
            wait = backoff_s(state.failures, max_backoff_s)
            elapsed = clock - state.last_failed_at
            if elapsed < wait:
                return (
                    f"backing off after {state.failures} failure(s): "
                    f"{wait - elapsed:.0f}s left"
                )

        if not moved and max_fires > 0 and state.fires_since_change >= max_fires:
            return (
                f"parked after {state.fires_since_change} fire(s) with no change; "
                "will resume when the item changes"
            )
        return None

    def record_fire(self, key: str, item_fingerprint: str) -> KeyState:
        """Note that this key was just enqueued.

        A changed fingerprint resets both counters: the work moved, so neither the
        no-progress count nor the failure streak describes it any more. That reset
        is what unparks a key without anyone intervening.
        """
        state = self.load(key)
        if state.fingerprint != item_fingerprint:
            state.fingerprint = item_fingerprint
            state.fires_since_change = 0
            state.failures = 0
            state.last_failed_at = 0.0
        state.fires_since_change += 1
        self.save(key, state)
        return state

    def record_outcome(
        self, key: str, *, ok: bool, now: float | None = None
    ) -> KeyState:
        """Fold a finished run's success or failure into the key's record."""
        state = self.load(key)
        if ok:
            state.failures = 0
            state.last_failed_at = 0.0
        else:
            state.failures += 1
            state.last_failed_at = time.time() if now is None else now
        self.save(key, state)
        return state


class KeyStateOutcomeRecorder:
    """`OutcomeRecorder` over a `KeyStateStore`.

    Separate from the store so the store keeps its plain string-keyed API and the
    worker-facing shape lives in one small adapter. This is what closes the loop
    the producer cannot close alone: the producer records *attempts* at enqueue
    time, and this records *outcomes* when the worker finishes, which is the half
    the backoff in `should_skip` reads.
    """

    def __init__(self, store: KeyStateStore) -> None:
        self._store = store

    def record(self, job: Any, result: Any) -> None:
        """Fold this job's outcome into its key. Unkeyed jobs have no producer.

        Never raises, per the port: this runs after the result is durable but
        before it is delivered, so a failure here must not cost the reply.
        """
        key = getattr(job, "key", None)
        if not key:
            return
        try:
            self._store.record_outcome(key, ok=bool(result.ok))
        except OSError as exc:
            logger.warning("jobs: could not record outcome for %s: %s", key, exc)
