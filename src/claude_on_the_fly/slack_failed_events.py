"""Slack events whose handler raised before any side effect, kept for a retry.

Bolt catches a handler exception and moves on, and Slack does not redeliver an
event whose acknowledgement already went out. On 2026-10-27 a handler bug
failed 52 messages over ten hours and none of them ran after the fix: the
socket never dropped, so no catch-up ran, and the restart scan could not tell a
failed message from a handled one.

This file closes that gap. The frontend stores an event here only when it
raised before the message was marked processed, so a replay cannot run a turn
twice. The next catch-up replays the stored events oldest first and removes
each one that goes through.

Every dimension is bounded. The count is capped and an event older than the
restore window is dropped. Failed attempts are counted for the log, not capped:
the socket reconnects several times during a long outage, each reconnect
replays the store, and a cap would spend every event's attempts before the
restart that ships the fix. A message that itself triggers the bug replays at
most once per catch-up until it ages out.

Same durability rules as `event_dedupe.py` and `slack_watermarks.py`: the file
is replaced by rename and not synced, and an unreadable file counts as empty.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from pathlib import Path

from claude_on_the_fly.slack_watermarks import DEFAULT_MAX_AGE_SECONDS, _event_time

logger = logging.getLogger(__name__)

# The 2026-10-27 outage left 52. Every stored event can also land in the
# processed ledger when it replays, so this stays small next to that ledger.
DEFAULT_CAPACITY = 100


def _event_key(event: object) -> tuple[str, str] | None:
    if not isinstance(event, dict):
        return None
    channel = event.get("channel")
    event_ts = event.get("ts")
    if not isinstance(channel, str) or not channel or not isinstance(event_ts, str):
        return None
    if _event_time(event_ts) is None:
        return None
    return channel, event_ts


class FailedSlackEvents:
    """A bounded, atomic file of failed raw events and their attempt counts."""

    def __init__(self, path: Path, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self._path = path
        self._capacity = capacity
        self._entries: OrderedDict[tuple[str, str], tuple[dict, int]] = OrderedDict()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "slack failed events: ignoring unreadable %s: %s", self._path, exc
            )
            return
        rows = raw.get("events") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            logger.warning("slack failed events: ignoring malformed %s", self._path)
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            event = row.get("event")
            attempts = row.get("attempts")
            key = _event_key(event)
            if not isinstance(event, dict) or key is None:
                continue
            if not isinstance(attempts, int):
                continue
            self._entries[key] = (event, attempts)
        self._trim()

    def __len__(self) -> int:
        return len(self._entries)

    def record_failure(self, event: dict) -> None:
        """Count one failed attempt, storing the event on its first failure."""
        key = _event_key(event)
        if key is None:
            logger.warning(
                "slack failed events: not kept, no usable channel and ts: %r",
                {name: event.get(name) for name in ("channel", "ts")},
            )
            return
        try:
            # A copy that is known to serialize, so a later write cannot fail on it.
            stored = json.loads(json.dumps(event))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "slack failed events: not kept, %s/%s does not serialize: %s",
                *key,
                exc,
            )
            return
        _, attempts = self._entries.get(key, (stored, 0))
        attempts += 1
        if attempts > 1:
            logger.warning(
                "slack failed events: %s/%s failed again (%d attempts)", *key, attempts
            )
        self._entries[key] = (stored, attempts)
        self._trim()
        self._save()

    def discard(self, channel: str, event_ts: str) -> None:
        """Forget one event. Forgetting an absent one is not rewritten."""
        if self._entries.pop((channel, event_ts), None) is not None:
            self._save()

    def pending(
        self,
        *,
        now: float | None = None,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    ) -> list[dict]:
        """Stored events oldest first, after dropping those past the window."""
        current = time.time() if now is None else now
        cutoff = current - max_age_seconds
        stale = [key for key in self._entries if (_event_time(key[1]) or 0) < cutoff]
        for key in stale:
            del self._entries[key]
            logger.warning(
                "slack failed events: dropping %s/%s, older than %d seconds",
                *key,
                int(max_age_seconds),
            )
        if stale:
            self._save()
        ordered = sorted(self._entries, key=lambda key: _event_time(key[1]) or 0)
        return [dict(self._entries[key][0]) for key in ordered]

    def _trim(self) -> None:
        while len(self._entries) > self._capacity:
            (channel, event_ts), _ = self._entries.popitem(last=False)
            logger.warning(
                "slack failed events: store full, dropping %s/%s", channel, event_ts
            )

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.tmp")
            body = {
                "events": [
                    {"event": event, "attempts": attempts}
                    for event, attempts in self._entries.values()
                ]
            }
            tmp.write_text(json.dumps(body), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning(
                "slack failed events: could not write %s: %s", self._path, exc
            )
