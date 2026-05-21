"""Tracker Protocol: contract every issue-source adapter must satisfy.

Adding a new tracker (Linear, GitHub Issues, etc.) means implementing this
Protocol and registering the class in `tracker.SUPPORTED_TRACKERS`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .issue import Issue, IssueSummary

if TYPE_CHECKING:
    from ..config import TrackerCommonConfig


@runtime_checkable
class Tracker(Protocol):
    """Read-only interface to an issue-source platform.

    The orchestrator never writes via this Protocol. Status transitions, comments,
    and label edits are agent-side concerns done through whatever tools the agent
    has available (acli, gh, REST APIs).
    """

    @classmethod
    def from_config(cls, cfg: TrackerCommonConfig) -> Tracker:
        """Construct an adapter from a TrackerCommonConfig (or subclass). Each
        adapter decides which config fields it needs."""
        ...

    async def fetch_one(self, key: str) -> Issue:
        """Fetch full details for one ticket by its identifier (e.g. 'ACE-1133')."""
        ...

    async def fetch_candidates(self, cfg: TrackerCommonConfig) -> list[Issue]:
        """Return tickets matching the active-state gate. Daemon polls this every tick."""
        ...

    async def fetch_summaries_by_keys(self, keys: list[str]) -> dict[str, IssueSummary]:
        """Batched snapshot fetch for reconciliation. Returns key → IssueSummary
        (state + adapter-specific `extra` fields). Keys not visible to this
        account are absent."""
        ...

    def is_terminal(self, summary: IssueSummary, cfg: TrackerCommonConfig) -> bool:
        """True when the issue has reached a terminal state — orchestrator
        cancels the worker AND removes its workspace.

        Jira: `state in cfg.terminal_states`.
        GitHub: PR closed or merged.
        """
        ...

    def is_active(self, summary: IssueSummary, cfg: TrackerCommonConfig) -> bool:
        """True when the issue should keep its worker running. False means
        cancel the worker but leave the workspace ("parked").

        Jira: state in active_states AND gate label still attached (or no gate).
        GitHub: PR open AND current user still in `reviewRequests`.
        """
        ...

    def issue_to_summary(self, issue: Issue) -> IssueSummary:
        """Project a full Issue (from `fetch_one`) into an IssueSummary so
        `is_terminal`/`is_active` can be applied without an extra fetch.

        Each adapter knows which fields it needs in the summary's `extra`.
        Jira: `{"labels": issue.labels}`. GitHub: derives review-requested
        status from whatever it stashed on the Issue at fetch time.
        """
        ...

    async def aclose(self) -> None:
        """Release any held resources (HTTP clients, etc.)."""
        ...
