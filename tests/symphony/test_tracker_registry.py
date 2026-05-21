"""Tracker Protocol registry / make_tracker dispatch."""

from __future__ import annotations

import pytest

from claude_on_the_fly.symphony.config import TrackerConfig
from claude_on_the_fly.symphony.tracker import (
    SUPPORTED_TRACKERS,
    JiraTracker,
    Tracker,
    make_tracker,
)


def _cfg(kind: str = "jira") -> TrackerConfig:
    return TrackerConfig.from_dict(
        {
            "kind": kind,
            "base_url": "https://x.atlassian.net",
            "email": "me@x.com",
            "api_token": "tok",
            "project_key": "PROJ",
        }
    )


def test_supported_trackers_registered():
    assert "jira" in SUPPORTED_TRACKERS
    assert SUPPORTED_TRACKERS["jira"] is JiraTracker


def test_make_tracker_returns_jira():
    t = make_tracker(_cfg("jira"))
    assert isinstance(t, JiraTracker)


def test_make_tracker_unknown_kind_raises():
    cfg = _cfg("linear")
    with pytest.raises(ValueError, match="unsupported"):
        make_tracker(cfg)


def test_make_tracker_error_lists_supported():
    cfg = _cfg("notreal")
    with pytest.raises(ValueError, match=r"Available: \['jira'\]"):
        make_tracker(cfg)


def test_jira_tracker_satisfies_protocol():
    t = make_tracker(_cfg())
    assert isinstance(t, Tracker)  # runtime_checkable Protocol
