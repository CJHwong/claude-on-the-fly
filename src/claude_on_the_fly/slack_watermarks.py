"""Recent Slack conversations to scan after a daemon restart.

Socket Mode reconnects inside one process already use an in-memory channel
watermark.  A process restart used to erase that map, so the first connection
could not ask Slack for messages sent while the daemon was down.  This module
persists the same small map; it is intentionally not a workspace-wide history
index.

Both dimensions are bounded.  Only the most recently active conversations are
kept, and callers only restore entries whose Slack timestamp is recent.  A long
outage therefore cannot turn startup into an unbounded burst of history calls or
stale agent turns.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches the processed-event ledger's 1,000-entry cap at the existing
# 20-message history limit: even a maximally full startup scan stays within the
# durable dedupe window.
DEFAULT_CAPACITY = 50
DEFAULT_MAX_AGE_SECONDS = 3600.0


@dataclass(frozen=True)
class SlackWatermark:
    channel: str
    ts: str
    channel_type: str = ""


def _event_time(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


class SlackWatermarks:
    """A bounded, atomic file of last-processed timestamps by conversation."""

    def __init__(self, path: Path, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self._path = path
        self._capacity = capacity
        self._entries: OrderedDict[str, SlackWatermark] = OrderedDict()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "slack watermarks: ignoring unreadable %s: %s", self._path, exc
            )
            return
        rows = raw.get("channels") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            logger.warning("slack watermarks: ignoring malformed %s", self._path)
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            channel = row.get("channel")
            event_ts = row.get("ts")
            channel_type = row.get("channel_type", "")
            if (
                not isinstance(channel, str)
                or not channel
                or not isinstance(event_ts, str)
                or _event_time(event_ts) is None
                or not isinstance(channel_type, str)
            ):
                continue
            self._entries[channel] = SlackWatermark(
                channel=channel, ts=event_ts, channel_type=channel_type
            )
            self._entries.move_to_end(channel)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)

    def record(self, channel: str, event_ts: str, channel_type: str = "") -> None:
        """Advance one conversation without ever moving its timestamp backward."""
        candidate_time = _event_time(event_ts)
        if not channel or candidate_time is None:
            return
        current = self._entries.get(channel)
        if current is not None:
            current_time = _event_time(current.ts)
            if current_time is not None and candidate_time < current_time:
                event_ts = current.ts
            if not channel_type:
                channel_type = current.channel_type
        entry = SlackWatermark(channel, event_ts, channel_type)
        if current == entry:
            return
        self._entries[channel] = entry
        self._entries.move_to_end(channel)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        self._save()

    def recent(
        self,
        *,
        now: float | None = None,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    ) -> tuple[SlackWatermark, ...]:
        """Return only entries recent enough to be safe startup catch-up inputs."""
        current = time.time() if now is None else now
        cutoff = current - max_age_seconds
        return tuple(
            entry
            for entry in self._entries.values()
            if (_event_time(entry.ts) or 0) >= cutoff
        )

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.tmp")
            body = {"channels": [asdict(entry) for entry in self._entries.values()]}
            tmp.write_text(json.dumps(body), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("slack watermarks: could not write %s: %s", self._path, exc)
