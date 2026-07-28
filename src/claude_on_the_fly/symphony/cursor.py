"""Per-ticket cursor storage for Jira gating.

Replaces `gate_label`-based gating with a timestamp comparison: the daemon
re-claims a ticket only when `ticket.updated_at > last_job_done_time`. The
agent's own writes (comments, transitions) are naturally swallowed because
we sample the cursor AFTER the worker returns, so the agent's last
write-induced bump is already reflected in the new cursor.

File layout:
    ~/.claude-on-the-fly/symphony/state/<source>/<KEY>.json

Each file is one ticket. Atomic writes via tmpfile + os.replace().
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Safe charset for ticket identifiers translated into filenames. GitHub's
# `owner/repo#42` style needs sanitizing; Jira `FIS-1234` is already safe.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_filename(identifier: str) -> str:
    """`owner/repo#42` → `owner__repo__42`. Keeps the path single-segment."""
    return _FILENAME_SAFE.sub("__", identifier)


@dataclass
class TicketCursor:
    """Last-known-good state for one ticket.

    `last_job_done_time` is the ISO 8601 timestamp of the most recent
    completed run (terminal state OR voluntary yield). The daemon re-claims
    the ticket only when the tracker's `updated` field is newer than this.
    """

    identifier: str
    last_job_done_time: str | None = None  # ISO 8601 UTC
    last_run_outcome: str = ""  # "terminal" / "yield" / "stall" / "max_turns" / etc
    attempts: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "last_job_done_time": self.last_job_done_time,
            "last_run_outcome": self.last_run_outcome,
            "attempts": self.attempts,
            "extras": self.extras,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TicketCursor:
        return cls(
            identifier=str(raw.get("identifier") or ""),
            last_job_done_time=(
                str(raw["last_job_done_time"])
                if raw.get("last_job_done_time")
                else None
            ),
            last_run_outcome=str(raw.get("last_run_outcome") or ""),
            attempts=int(raw.get("attempts") or 0),
            extras=dict(raw.get("extras") or {}),
        )


class CursorStore:
    """Loads, saves, and queries per-ticket cursor files."""

    def __init__(self, root: Path, source: str) -> None:
        self._dir = root / source
        self._cache: dict[str, TicketCursor] = {}

    @property
    def dir(self) -> Path:
        return self._dir

    def _path(self, identifier: str) -> Path:
        return self._dir / f"{_safe_filename(identifier)}.json"

    def load(self, identifier: str) -> TicketCursor:
        """Read the on-disk cursor, fall back to a fresh one if missing/invalid."""
        if identifier in self._cache:
            return self._cache[identifier]
        path = self._path(identifier)
        if not path.is_file():
            cursor = TicketCursor(identifier=identifier)
            self._cache[identifier] = cursor
            return cursor
        try:
            raw = json.loads(path.read_text())
            cursor = TicketCursor.from_dict(raw)
            # Defensive: keep the identifier from the call, not the file.
            cursor.identifier = identifier
        except Exception as exc:
            logger.warning(
                "cursor load failed for %s (%s); treating as fresh", identifier, exc
            )
            cursor = TicketCursor(identifier=identifier)
        self._cache[identifier] = cursor
        return cursor

    def save(self, cursor: TicketCursor) -> None:
        """Atomic write: tmpfile + os.replace()."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(cursor.identifier)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cursor.to_dict(), indent=2, sort_keys=True))
        os.replace(tmp, path)
        self._cache[cursor.identifier] = cursor

    def record_run_end(
        self,
        identifier: str,
        *,
        outcome: str,
        ticket_updated: str | None = None,
        now_iso: str | None = None,
        attempts_increment: int = 1,
    ) -> TicketCursor:
        """Bump cursor and persist. Called by the worker on every run end.

        Stamps the LATER of wall-clock now and the ticket's own `updated`
        timestamp. The agent's writes during the run bump `updated`; if Jira's
        clock runs ahead of ours (or the write indexes a hair after run-end),
        a plain wall-clock stamp would be behind the ticket's `updated` and the
        next tick would re-claim a just-finished ticket. Taking the max closes
        that window — only a NEW (post-run) write makes it claimable again.
        """
        cursor = self.load(identifier)
        stamp = now_iso or _now_iso()
        if ticket_updated and _compare_iso(ticket_updated, stamp) > 0:
            stamp = ticket_updated
        cursor.last_job_done_time = stamp
        cursor.last_run_outcome = outcome
        cursor.attempts += attempts_increment
        self.save(cursor)
        return cursor


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def is_claimable(*, ticket_updated: str | None, cursor: TicketCursor) -> bool:
    """True if the ticket has been touched since our last run.

    - No prior run → always claimable (cursor is fresh).
    - No `updated` from the tracker → claimable (we can't compare, so we try).
    - Tracker `updated` > cursor → claimable.
    """
    if cursor.last_job_done_time is None:
        return True
    if not ticket_updated:
        return True
    return _compare_iso(ticket_updated, cursor.last_job_done_time) > 0


def _compare_iso(a: str, b: str) -> int:
    """Return -1 / 0 / +1 comparing two ISO 8601 timestamps.

    Tolerates the +0800 offset format Jira emits as well as the standard
    `Z` form. Returns 0 when either side cannot be parsed (the safer
    "treat as equal" default — caller decides what to do).
    """
    try:
        da = _parse_iso(a)
        db = _parse_iso(b)
    except (ValueError, TypeError):
        return 0
    if da < db:
        return -1
    if da > db:
        return 1
    return 0


def _parse_iso(s: str) -> datetime:
    """Best-effort ISO 8601 parser. Jira emits `2026-05-22T15:31:51.189+0800`
    which `fromisoformat` accepts only after Python 3.11. We're on 3.12+."""
    # Normalize trailing `Z` to `+00:00` for fromisoformat compatibility.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Jira format `+0800` (no colon) → `+08:00`.
    if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":" and s[-4:].isdigit():
        s = s[:-2] + ":" + s[-2:]
    dt = datetime.fromisoformat(s)
    # Assume UTC for a naive timestamp so comparisons never mix naive/aware
    # (which raises TypeError). Real Jira/GitHub stamps carry an offset; this
    # only guards a degenerate tracker payload.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
