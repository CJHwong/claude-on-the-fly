"""Append-only JSONL audit log of AI-job lifecycle events.

Every AI run, whether triggered by symphony, telegram, slack, or gmail,
emits its dispatch / complete / failure transitions here. Heartbeats tell
the TUI what is currently running; this log tells it what *has happened*
so users can find jobs that have aged out of the live pane and re-attach
to them.

Format: one JSON object per line. Required keys: ts (ISO-8601 UTC), type,
source, identifier. `source` is the frontend ("symphony" | "telegram" |
"slack" | "gmail"). For symphony rows, the tracker (jira | github) lives
in an optional `tracker` field. Optional everywhere: workspace,
session_uuid, plus type-specific extras (state, reason, attempt, error).

Concurrency: writers use `os.write` with O_APPEND, which POSIX guarantees
atomic for line-sized writes, safe across the daemon process and any
co-running ad-hoc CLI invocations. Readers don't lock; malformed lines
(partial writes after a hard crash) are skipped.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_on_the_fly import agent

logger = logging.getLogger(__name__)

DEFAULT_PATH = agent.DATA_DIR / "state" / "events.jsonl"

# Event type vocabulary. Kept as plain strings (not an enum) because they
# round-trip through JSON and the consumer is the TUI, which would just
# stringify them anyway.
EVENT_DISPATCHED = "dispatched"
EVENT_WORKER_DONE = "worker_done"  # turn loop exited normally
EVENT_CANCELLED = "cancelled"  # reason in extra: "terminal" | "inactive" | "stall"
EVENT_RETRY_SCHEDULED = "retry_scheduled"
EVENT_WORKER_FAILED = "worker_failed"  # unhandled exception in worker


class EventLog:
    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        event_type: str,
        *,
        source: str,
        identifier: str,
        **extra: Any,
    ) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "type": event_type,
            "source": source,
            "identifier": identifier,
        }
        for key, value in extra.items():
            if value is None:
                continue
            # str() Path objects so the JSON is portable + diff-friendly.
            if isinstance(value, Path):
                record[key] = str(value)
            else:
                record[key] = value
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"

        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError:
            logger.exception("events: open(%s) for append failed", self.path)
            return
        try:
            os.write(fd, line.encode("utf-8"))
        except OSError:
            logger.exception("events: append to %s failed", self.path)
        finally:
            os.close(fd)

    def tail(self, n: int) -> list[dict[str, Any]]:
        """Return the last n parseable records (oldest first).

        Reverse-seeks the file in 8KB chunks so we don't load months of
        history into memory just to render 50 rows. Malformed JSON lines
        are skipped silently — they typically come from a crashed write
        and the next clean line is what we want anyway.
        """
        if n <= 0 or not self.path.exists():
            return []

        block = 8192
        data = bytearray()
        try:
            with self.path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                # Read backward until we have enough newlines or reach start.
                # `n + 1` because the read may slice a partial leading line
                # that we'll discard.
                while pos > 0 and data.count(b"\n") <= n:
                    read = min(block, pos)
                    pos -= read
                    f.seek(pos)
                    data[:0] = f.read(read)
        except OSError:
            logger.exception("events: tail(%s) read failed", self.path)
            return []

        # Drop a partial first line when we didn't reach byte 0 — it may
        # be sliced mid-record.
        lines = data.split(b"\n")
        if pos > 0 and lines:
            lines = lines[1:]

        out: list[dict[str, Any]] = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                out.append(record)
        return out[-n:]
