"""OrchestratorState: claim/release, mark_turn_end, RunningEntry fields."""

from __future__ import annotations

import pytest

from claude_on_the_fly.symphony.state import OrchestratorState
from claude_on_the_fly.symphony.tracker.issue import Issue


def _issue(issue_id: str = "1", identifier: str = "PROJ-1") -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title="t",
        state="To Do",
        description_raw=None,
        priority=None,
        labels=(),
        blocked_by=(),
        parent_key=None,
        url="",
        created_at=None,
        updated_at=None,
    )


def test_claim_release():
    s = OrchestratorState()
    issue = _issue()
    assert not s.is_claimed(issue.key)
    s.claim(issue)
    assert s.is_claimed(issue.key)
    assert s.running_count() == 1
    s.release(issue.key)
    assert not s.is_claimed(issue.key)
    assert s.running_count() == 0


def test_double_claim_raises():
    s = OrchestratorState()
    issue = _issue()
    s.claim(issue)
    with pytest.raises(RuntimeError):
        s.claim(issue)


def test_running_by_state():
    s = OrchestratorState()
    i1 = _issue("1", "PROJ-1")
    i2 = _issue("2", "PROJ-2")
    s.claim(i1)
    s.claim(i2)
    s.update_running_state(i1.key, "Building")
    assert s.running_by_state("To Do") == 1
    assert s.running_by_state("Building") == 1


def test_mark_turn_end_sets_timestamp():
    s = OrchestratorState()
    issue = _issue("1", "PROJ-1")
    s.claim(issue)
    entry = s.get_running(issue.key)
    assert entry is not None
    assert entry.last_turn_end_at is None
    s.mark_turn_end(issue.key)
    assert entry.last_turn_end_at is not None
    assert entry.last_turn_end_at >= entry.started_at


def test_get_running_returns_none_for_unknown():
    s = OrchestratorState()
    assert s.get_running("nope") is None


def test_running_entry_default_fields():
    s = OrchestratorState()
    issue = _issue("1", "PROJ-1")
    s.claim(issue)
    entry = s.get_running(issue.key)
    assert entry is not None
    assert entry.task is None
    assert entry.workspace is None
    assert entry.last_turn_end_at is None
    assert entry.failure_attempt == 0
    assert entry.source == "jira"  # default; matches Issue.source default


def test_all_running_returns_all_entries() -> None:
    s = OrchestratorState()
    s.claim(_issue("1", "PROJ-1"))
    s.claim(_issue("2", "PROJ-2"))
    running = s.all_running()
    assert len(running) == 2
    identifiers = {r.issue_identifier for r in running}
    assert identifiers == {"PROJ-1", "PROJ-2"}


def test_composite_keys_isolate_cross_source_collisions():
    """Two issues with the same raw id but different sources are tracked
    independently — the composite key prevents collision."""
    s = OrchestratorState()
    jira_issue = _issue("42", "PROJ-42")
    gh_issue = Issue(
        id="42",  # same raw id as jira_issue
        identifier="owner/repo#42",
        title="t",
        state="open",
        description_raw=None,
        priority=None,
        labels=(),
        blocked_by=(),
        parent_key=None,
        url="",
        created_at=None,
        updated_at=None,
        source="github",
    )
    s.claim(jira_issue)
    s.claim(gh_issue)  # would have raised if keyed by raw id alone
    assert s.running_count() == 2
    assert s.is_claimed(jira_issue.key)
    assert s.is_claimed(gh_issue.key)
    assert jira_issue.key != gh_issue.key
