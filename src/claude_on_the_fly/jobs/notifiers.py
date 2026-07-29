"""Delivery adapters that are not Slack, and the router that picks between them.

A job's `origin` says where its reply belongs. Slack-triggered jobs carry a
channel and thread; cron-triggered ones carry the entry that produced them and
want their reply in that entry's log file, where the rest of that entry's history
already is.

`worker.py` holds exactly one `Notifier`, which is why routing lives in an adapter
rather than in the loop: the worker should not learn to branch on `origin`, and
`origin` is the notifier's to interpret in the first place.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from claude_on_the_fly.jobs.core import Result

logger = logging.getLogger(__name__)

# `origin` with no `kind` predates cron entirely and can only have come from the
# Slack producer, so that is what an absent value means.
DEFAULT_KIND = "slack"


class _Notifier(Protocol):
    async def notify(self, origin: dict[str, Any], result: Result) -> None: ...


class LogNotifier:
    """Appends a job's reply to its cron entry's own log file.

    `append` is injected rather than imported so this does not drag the cron
    module (and its yaml/croniter/liquid imports) into the worker process.
    """

    def __init__(self, append: Any) -> None:
        self._append = append

    async def notify(self, origin: dict[str, Any], result: Result) -> None:
        entry = origin.get("entry")
        if not entry:
            # Raising rather than returning: returning normally is what marks a
            # result delivered, and a cron result with nowhere to go is a bug
            # worth keeping visible in `undelivered()` until it is fixed.
            raise ValueError(f"cron origin has no 'entry' to log against: {origin!r}")
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        status = "done" if result.ok else "FAILED"
        body = result.text.rstrip()
        self._append(str(entry), f"--- {stamp} reply ({status}) ---\n{body}\n")


class RoutingNotifier:
    """Dispatches on `origin["kind"]` to one of the notifiers it was built with."""

    def __init__(self, routes: dict[str, _Notifier]) -> None:
        self._routes = routes

    async def notify(self, origin: dict[str, Any], result: Result) -> None:
        kind = str(origin.get("kind") or DEFAULT_KIND)
        notifier = self._routes.get(kind)
        if notifier is None:
            # Raising keeps the result in `undelivered()` rather than marking it
            # delivered to nowhere. A typo'd kind is then visible as a stuck
            # reply instead of a silently discarded one.
            raise ValueError(
                f"no notifier for origin kind {kind!r}; "
                f"known kinds: {sorted(self._routes)}"
            )
        await notifier.notify(origin, result)
