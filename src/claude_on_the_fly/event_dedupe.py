"""Which chat events this install has already handled, across restarts.

A frontend keeps a bounded set of processed event ids in memory so a redelivery
inside one process cannot run the same message twice. `slack.py` says why that
matters where it handles `$compact`:

    a reconnect re-ingests the trigger and compacts a second time

That protection ends at the process boundary. The in-memory set starts empty on
every start, so a redelivery that lands after a restart is indistinguishable
from a new message, and the turn runs again. `turns.py` does not cover it: that
journal replays turns the daemon *accepted* and lost, keyed by turn, while this
answers whether an arriving event was ever accepted at all. One is about
finishing work, the other about not starting it twice.

Deliberately not a catch-up watermark. Remembering what was handled is cheap and
has no failure mode worse than forgetting; going back to fetch what was missed
is a different feature with a different risk (a long outage turning into a burst
of history calls and a replay of stale mentions), and it belongs in its own
change with its own argument.

The restart catch-up feature keeps that separate watermark, but shares this
ledger for dedupe. It also records COTF's own posted Slack timestamps: a user-token
install deliberately accepts messages authored by its own user, and only those
durable ids distinguish a reply sent by COTF from a new message typed by the same
person after the in-memory echo guard is lost at restart.

Durability is deliberately atomic-but-not-synced. The file is replaced by rename
so a reader never sees a partial set, and there is no fsync: a power loss can
lose the tail, and the cost of that is re-running a message exactly the way this
install does today. An fsync on every accepted event would put a disk wait on
the event loop, which is a real stall on a busy workspace and a steep price for
a failure mode that degrades to current behaviour.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches the in-memory deque this replaces. Enough that a redelivery window
# cannot outrun it, small enough that the whole set is a single short write.
DEFAULT_CAPACITY = 1000


class ProcessedEvents:
    """A bounded, order-preserving set of handled event ids, backed by a file."""

    def __init__(self, path: Path, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self._path = path
        self._capacity = capacity
        self._ids: deque[str] = deque(maxlen=capacity)
        self._load()

    def _load(self) -> None:
        """Read the stored ids, or start empty.

        Any unreadable or malformed file is treated as empty rather than fatal.
        The consequence of starting empty is the behaviour this install had
        before the file existed, so a corrupt file must never stop a daemon.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("event dedupe: ignoring unreadable %s: %s", self._path, exc)
            return
        ids = raw.get("ids") if isinstance(raw, dict) else None
        if not isinstance(ids, list):
            logger.warning("event dedupe: ignoring malformed %s", self._path)
            return
        self._ids.extend(item for item in ids if isinstance(item, str))

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, event_id: str) -> None:
        """Record an id and persist. A repeat is not rewritten."""
        if event_id in self._ids:
            return
        self._ids.append(event_id)
        self._save()

    def _save(self) -> None:
        """Rewrite the whole set atomically.

        A failure here is logged and swallowed. Losing the ability to remember
        an event costs a possible duplicate after a restart; raising would cost
        the message that is being handled right now.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.tmp")
            tmp.write_text(json.dumps({"ids": list(self._ids)}), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("event dedupe: could not write %s: %s", self._path, exc)
