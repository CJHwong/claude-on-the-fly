"""OrchestratorState: claim/release, exhausted parking."""

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
    assert not s.is_claimed("1")
    s.claim(issue)
    assert s.is_claimed("1")
    assert s.running_count() == 1
    s.release("1")
    assert not s.is_claimed("1")
    assert s.running_count() == 0


def test_double_claim_raises():
    s = OrchestratorState()
    issue = _issue()
    s.claim(issue)
    with pytest.raises(RuntimeError):
        s.claim(issue)


def test_exhausted_blocks_re_eligibility():
    s = OrchestratorState()
    s.mark_exhausted("1")
    assert s.is_exhausted("1")
    # Release does not clear exhausted (it's daemon-lifetime parking).
    s.release("1")
    assert s.is_exhausted("1")


def test_running_by_state():
    s = OrchestratorState()
    s.claim(_issue("1", "PROJ-1"))
    s.claim(_issue("2", "PROJ-2"))
    s.update_running_state("1", "Building")
    assert s.running_by_state("To Do") == 1
    assert s.running_by_state("Building") == 1
