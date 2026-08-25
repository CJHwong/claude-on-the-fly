"""Durable Slack catch-up watermarks and event deduplication.

The turn journal owns accepted work. This smaller ledger owns the gap before
that journal: Socket Mode can disconnect or the process can restart after an
event was observed, and Slack may deliver the same event again. Only event
timestamps, channel ids, and channel kinds are stored -- never message text or
credentials.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

PROCESSED_CAP = 1000
CHANNEL_CAP = 1000


@dataclass(frozen=True)
class SlackEventSnapshot:
    processed_ts: tuple[str, ...] = ()
    active_channels: dict[str, str] = field(default_factory=dict)
    channel_types: dict[str, str] = field(default_factory=dict)


class SlackEventState:
    """Atomic JSON storage for Slack's reconnect bookkeeping."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> SlackEventSnapshot:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return SlackEventSnapshot()
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("slack events: cannot read %s: %s", self.path, exc)
            return SlackEventSnapshot()
        if not isinstance(raw, dict):
            logger.warning("slack events: ignoring non-object state in %s", self.path)
            return SlackEventSnapshot()
        processed_raw = raw.get("processed_ts")
        channels_raw = raw.get("active_channels")
        types_raw = raw.get("channel_types")
        processed = (
            tuple(item for item in processed_raw if isinstance(item, str))[
                -PROCESSED_CAP:
            ]
            if isinstance(processed_raw, list)
            else ()
        )
        channels = self._string_map(channels_raw)
        channel_types = self._string_map(types_raw)
        return SlackEventSnapshot(processed, channels, channel_types)

    @staticmethod
    def _string_map(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            key: item
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)
        }

    def write(
        self,
        processed_ts: list[str],
        active_channels: dict[str, str],
        channel_types: dict[str, str],
    ) -> None:
        """Replace the ledger atomically; a failed write leaves the old one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        newest_channels = dict(
            sorted(active_channels.items(), key=lambda item: item[1])[-CHANNEL_CAP:]
        )
        payload = json.dumps(
            {
                "processed_ts": processed_ts[-PROCESSED_CAP:],
                "active_channels": newest_channels,
                "channel_types": {
                    channel: channel_types[channel]
                    for channel in newest_channels
                    if channel in channel_types
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        tmp = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
