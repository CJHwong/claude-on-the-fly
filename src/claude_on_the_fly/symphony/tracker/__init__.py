"""Tracker adapters. Today only Jira is implemented; add more by satisfying
the `Tracker` Protocol from `base.py` and registering in `SUPPORTED_TRACKERS`.
"""

from __future__ import annotations

from .base import Tracker
from .issue import BlockerRef, Issue
from .jira import JiraTracker

# Registry: tracker.kind name (lowercased) -> adapter class.
# Each adapter must implement Tracker Protocol and provide from_config(cfg).
SUPPORTED_TRACKERS: dict[str, type[Tracker]] = {
    "jira": JiraTracker,
}


def make_tracker(cfg) -> Tracker:
    """Construct the adapter named by cfg.kind. Raises if no adapter is registered."""
    cls = SUPPORTED_TRACKERS.get(cfg.kind.lower())
    if cls is None:
        raise ValueError(
            f"tracker.kind={cfg.kind!r} unsupported. "
            f"Available: {sorted(SUPPORTED_TRACKERS)}"
        )
    return cls.from_config(cfg)


def make_trackers(config) -> dict[str, Tracker]:
    """Construct one tracker per entry in `config.trackers`. Keys preserve
    the user's source naming (`jira`, `github`, ...). Raises if any
    configured kind has no registered adapter."""
    return {
        source: make_tracker(tracker_cfg)
        for source, tracker_cfg in config.trackers.items()
    }


__all__ = [
    "BlockerRef",
    "Issue",
    "JiraTracker",
    "SUPPORTED_TRACKERS",
    "Tracker",
    "make_tracker",
    "make_trackers",
]
