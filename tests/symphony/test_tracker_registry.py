"""Tracker Protocol registry / make_tracker dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_on_the_fly.symphony.config import SymphonyConfig, TrackerConfig
from claude_on_the_fly.symphony.tracker import (
    SUPPORTED_TRACKERS,
    GitHubTracker,
    JiraTracker,
    Tracker,
    make_tracker,
    make_trackers,
)


def _cfg(kind: str = "jira") -> TrackerConfig:
    return TrackerConfig.from_dict(
        {
            "kind": kind,
            "base_url": "https://x.atlassian.net",
            "project_key": "PROJ",
        }
    )


def test_supported_trackers_registered():
    assert "jira" in SUPPORTED_TRACKERS
    assert SUPPORTED_TRACKERS["jira"] is JiraTracker
    assert "github" in SUPPORTED_TRACKERS
    assert SUPPORTED_TRACKERS["github"] is GitHubTracker


def test_make_tracker_returns_jira():
    t = make_tracker(_cfg("jira"))
    assert isinstance(t, JiraTracker)


def test_make_tracker_unknown_kind_raises():
    cfg = _cfg("linear")
    with pytest.raises(ValueError, match="unsupported"):
        make_tracker(cfg)


def test_make_tracker_error_lists_supported():
    cfg = _cfg("notreal")
    with pytest.raises(ValueError, match=r"Available: \['github', 'jira'\]"):
        make_tracker(cfg)


def test_jira_tracker_satisfies_protocol():
    t = make_tracker(_cfg())
    assert isinstance(t, Tracker)  # runtime_checkable Protocol


def test_github_tracker_satisfies_protocol():
    from unittest.mock import patch

    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    with patch(
        "claude_on_the_fly.symphony.tracker.github.shutil.which",
        return_value="/usr/local/bin/gh",
    ):
        t = make_tracker(GitHubTrackerConfig(kind="github"))
    assert isinstance(t, Tracker)
    assert isinstance(t, GitHubTracker)


def test_make_trackers_skips_disabled():
    # github is disabled, so it must never be built — which also means its `gh`
    # preflight (shutil.which) never runs. Only the enabled jira survives.
    cfg = SymphonyConfig.from_dict(
        {
            "trackers": {
                "jira": {
                    "base_url": "https://x.atlassian.net",
                    "project_key": "PROJ",
                },
                "github": {"enabled": False},
            }
        },
        base=Path("/tmp"),
    )
    built = make_trackers(cfg)
    assert set(built) == {"jira"}
    assert isinstance(built["jira"], JiraTracker)
