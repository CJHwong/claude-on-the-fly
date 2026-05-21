"""Tracker Protocol: contract every issue-source adapter must satisfy.

Adding a new tracker (Linear, GitHub Issues, etc.) means implementing this
Protocol and registering the class in `tracker.SUPPORTED_TRACKERS`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .issue import Issue, IssueSummary

if TYPE_CHECKING:
    from ..config import TrackerConfig


@runtime_checkable
class Tracker(Protocol):
    """Read-only interface to an issue-source platform.

    The orchestrator never writes via this Protocol. Status transitions, comments,
    and label edits are agent-side concerns done through whatever tools the agent
    has available (acli, gh, REST APIs).
    """

    @classmethod
    def from_config(cls, cfg: TrackerConfig) -> Tracker:
        """Construct an adapter from a TrackerConfig. Each adapter decides which
        config fields it needs."""
        ...

    async def fetch_one(self, key: str) -> Issue:
        """Fetch full details for one ticket by its identifier (e.g. 'ACE-1133')."""
        ...

    async def fetch_candidates(self, cfg: TrackerConfig) -> list[Issue]:
        """Return tickets matching the active-state gate. Daemon polls this every tick."""
        ...

    async def fetch_summaries_by_keys(self, keys: list[str]) -> dict[str, IssueSummary]:
        """Batched snapshot fetch for reconciliation. Returns key → IssueSummary
        (state + labels). Keys not visible to this account are absent."""
        ...

    async def aclose(self) -> None:
        """Release any held resources (HTTP clients, etc.)."""
        ...
