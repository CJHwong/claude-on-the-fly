"""Retry queue with exponential backoff (SPEC §4.1.7, §8.4).

Two retry policies:
- Continuation: fixed 1s delay, attempt counter unchanged. Used after a worker
  exits cleanly but the ticket is still active (e.g. max_turns reached).
- Failure: exponential backoff `min(10000 * 2^(attempt-1), max_backoff_ms)`
  with attempt counter escalating per call. Used on ClaudeUnavailableError or
  unhandled exception.

Entries are keyed by the composite `<source>:<id>` (see `Issue.key`) so the
same raw id from two trackers can't collide.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .tracker.issue import make_key

if TYPE_CHECKING:
    from claude_on_the_fly.events import EventLog

logger = logging.getLogger(__name__)

CONTINUATION_DELAY_MS = 1000


def _now_ms() -> float:
    return time.monotonic() * 1000


def failure_delay_ms(attempt: int, max_ms: int) -> int:
    """attempt is 1-based; cap at max_ms."""
    if attempt < 1:
        attempt = 1
    return min(10_000 * (2 ** (attempt - 1)), max_ms)


@dataclass(frozen=True)
class RetryEntry:
    issue_id: str  # raw tracker-internal id
    identifier: str  # human key (Jira: "PROJ-1133"; GitHub: "owner/repo#123")
    attempt: int  # 1-based; counts failures
    due_at_ms: float  # monotonic clock ms
    error: str | None  # short description for logs
    source: str = "jira"  # tracker kind that minted issue_id

    @property
    def key(self) -> str:
        return make_key(self.source, self.issue_id)


class RetryQueue:
    """Tracks scheduled retry intentions per composite key. Single-entry per
    key (a new schedule replaces any pending one).

    If an `event_log` is supplied, each `schedule_failure` / `schedule_continuation`
    call also writes a `retry_scheduled` event for the TUI History pane.
    """

    def __init__(self, event_log: "EventLog | None" = None) -> None:
        self._entries: dict[str, RetryEntry] = {}
        self._event_log = event_log

    def has(self, key: str) -> bool:
        """Caller passes the composite `<source>:<id>` key."""
        return key in self._entries

    def get_attempt(self, key: str) -> int:
        entry = self._entries.get(key)
        return entry.attempt if entry else 0

    def schedule_continuation(
        self, issue_id: str, identifier: str, *, source: str = "jira"
    ) -> None:
        """Continuation does not count as a failure; preserve any prior failure_attempt."""
        key = make_key(source, issue_id)
        prev = self._entries.get(key)
        attempt = prev.attempt if prev else 0
        self._entries[key] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=_now_ms() + CONTINUATION_DELAY_MS,
            error=None,
            source=source,
        )
        logger.debug(
            "retry: continuation scheduled for %s in %dms",
            identifier,
            CONTINUATION_DELAY_MS,
        )
        self._emit_scheduled(
            source=source,
            identifier=identifier,
            attempt=attempt,
            delay_ms=CONTINUATION_DELAY_MS,
            kind="continuation",
            error=None,
        )

    def schedule_failure(
        self,
        issue_id: str,
        identifier: str,
        max_backoff_ms: int,
        attempt: int,
        error: str | None = None,
        *,
        source: str = "jira",
    ) -> None:
        """Caller passes the new attempt number explicitly (escalation lives in the caller)."""
        key = make_key(source, issue_id)
        delay = failure_delay_ms(attempt, max_backoff_ms)
        self._entries[key] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=_now_ms() + delay,
            error=error,
            source=source,
        )
        logger.info(
            "retry: failure scheduled for %s (attempt %d, delay %dms): %s",
            identifier,
            attempt,
            delay,
            error or "-",
        )
        self._emit_scheduled(
            source=source,
            identifier=identifier,
            attempt=attempt,
            delay_ms=delay,
            kind="failure",
            error=error,
        )

    def cancel(self, key: str) -> None:
        if self._entries.pop(key, None) is not None:
            logger.debug("retry: cancelled for key=%s", key)

    def due_now(self, now_ms: float | None = None) -> list[RetryEntry]:
        """Return and remove entries whose due_at_ms <= now_ms."""
        if now_ms is None:
            now_ms = _now_ms()
        due: list[RetryEntry] = []
        for key, entry in list(self._entries.items()):
            if entry.due_at_ms <= now_ms:
                due.append(entry)
                self._entries.pop(key, None)
        return due

    def requeue(
        self, entry: RetryEntry, delay_ms: int, error: str | None = None
    ) -> None:
        """Push an entry back without changing the attempt counter (for "no slots" case)."""
        self._entries[entry.key] = replace(
            entry,
            due_at_ms=_now_ms() + delay_ms,
            error=error if error is not None else entry.error,
        )

    def all_pending(self) -> list[RetryEntry]:
        return list(self._entries.values())

    def _emit_scheduled(
        self,
        *,
        source: str,
        identifier: str,
        attempt: int,
        delay_ms: int,
        kind: str,
        error: str | None,
    ) -> None:
        if self._event_log is None:
            return
        from claude_on_the_fly.events import EVENT_RETRY_SCHEDULED

        self._event_log.append(
            EVENT_RETRY_SCHEDULED,
            source="symphony",
            tracker=source,
            identifier=identifier,
            attempt=attempt,
            delay_ms=delay_ms,
            kind=kind,
            error=error,
        )
