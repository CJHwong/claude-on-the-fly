"""Retry queue with exponential backoff (SPEC §4.1.7, §8.4).

Two retry policies:
- Continuation: fixed 1s delay, attempt counter unchanged. Used after a worker
  exits cleanly but the ticket is still active (e.g. max_turns reached).
- Failure: exponential backoff `min(10000 * 2^(attempt-1), max_backoff_ms)`
  with attempt counter escalating per call. Used on ClaudeUnavailableError or
  unhandled exception.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace

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
    issue_id: str
    identifier: str
    attempt: int  # 1-based; counts failures
    due_at_ms: float  # monotonic clock ms
    error: str | None  # short description for logs


class RetryQueue:
    """Tracks scheduled retry intentions per issue_id. Single-entry per issue
    (a new schedule replaces any pending one)."""

    def __init__(self) -> None:
        self._entries: dict[str, RetryEntry] = {}

    def has(self, issue_id: str) -> bool:
        return issue_id in self._entries

    def get_attempt(self, issue_id: str) -> int:
        entry = self._entries.get(issue_id)
        return entry.attempt if entry else 0

    def schedule_continuation(self, issue_id: str, identifier: str) -> None:
        """Continuation does not count as a failure; preserve any prior failure_attempt."""
        prev = self._entries.get(issue_id)
        attempt = prev.attempt if prev else 0
        self._entries[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=_now_ms() + CONTINUATION_DELAY_MS,
            error=None,
        )
        logger.debug(
            "retry: continuation scheduled for %s in %dms",
            identifier,
            CONTINUATION_DELAY_MS,
        )

    def schedule_failure(
        self,
        issue_id: str,
        identifier: str,
        max_backoff_ms: int,
        attempt: int,
        error: str | None = None,
    ) -> None:
        """Caller passes the new attempt number explicitly (escalation lives in the caller)."""
        delay = failure_delay_ms(attempt, max_backoff_ms)
        self._entries[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=_now_ms() + delay,
            error=error,
        )
        logger.info(
            "retry: failure scheduled for %s (attempt %d, delay %dms): %s",
            identifier,
            attempt,
            delay,
            error or "-",
        )

    def cancel(self, issue_id: str) -> None:
        if self._entries.pop(issue_id, None) is not None:
            logger.debug("retry: cancelled for issue_id=%s", issue_id)

    def due_now(self, now_ms: float | None = None) -> list[RetryEntry]:
        """Return and remove entries whose due_at_ms <= now_ms."""
        if now_ms is None:
            now_ms = _now_ms()
        due: list[RetryEntry] = []
        for issue_id, entry in list(self._entries.items()):
            if entry.due_at_ms <= now_ms:
                due.append(entry)
                self._entries.pop(issue_id, None)
        return due

    def requeue(
        self, entry: RetryEntry, delay_ms: int, error: str | None = None
    ) -> None:
        """Push an entry back without changing the attempt counter (for "no slots" case)."""
        self._entries[entry.issue_id] = replace(
            entry,
            due_at_ms=_now_ms() + delay_ms,
            error=error if error is not None else entry.error,
        )

    def all_pending(self) -> list[RetryEntry]:
        return list(self._entries.values())
