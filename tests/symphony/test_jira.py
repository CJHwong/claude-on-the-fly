"""JQL composition, Issue normalization, JiraTracker via acli subprocess."""

from __future__ import annotations

import json
from collections.abc import Iterable
from unittest.mock import AsyncMock, patch

import pytest

from claude_on_the_fly.symphony.config import JiraTrackerConfig, TrackerConfig
from claude_on_the_fly.symphony.tracker.issue import Issue, IssueSummary
from claude_on_the_fly.symphony.tracker.jira import (
    JiraAcliError,
    JiraTracker,
    compose_jql,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tracker_cfg(**overrides: object) -> JiraTrackerConfig:
    base = {
        "kind": "jira",
        "base_url": "https://x.atlassian.net",
        "project_key": "PROJ",
    }
    base.update(overrides)
    return TrackerConfig.from_dict(base)


def _issue_payload(key: str, status: str) -> dict:
    return {
        "id": "10042",
        "key": key,
        "fields": {
            "summary": "Test issue",
            "status": {"name": status},
            "priority": {"id": "3"},
            "labels": [],
            "issuelinks": [],
            "parent": None,
            "description": None,
            "created": "2026-01-01T00:00:00",
            "updated": "2026-01-01T00:00:00",
        },
    }


class _FakeAcliProc:
    """Fake `asyncio.subprocess.Process` returned by create_subprocess_exec."""

    def __init__(self, *, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _patch_acli(scripts: Iterable[_FakeAcliProc]) -> AsyncMock:
    """Patch `asyncio.create_subprocess_exec` to return procs in order.

    Each call to `create_subprocess_exec` pops the next prepared proc.
    """
    queue = list(scripts)

    async def _exec(*_args: object, **_kwargs: object) -> _FakeAcliProc:
        if not queue:
            raise AssertionError("acli subprocess called more times than expected")
        return queue.pop(0)

    return AsyncMock(side_effect=_exec)


def _tracker() -> JiraTracker:
    return JiraTracker(base_url="https://j.example.com")


# ---------------------------------------------------------------------------
# compose_jql
# ---------------------------------------------------------------------------


def test_compose_jql_minimal() -> None:
    """No `jql` set → just the project scope."""
    cfg = _tracker_cfg()
    jql = compose_jql(cfg)
    assert jql == 'project = "PROJ"'


def test_compose_jql_wraps_full_clause() -> None:
    cfg = _tracker_cfg(jql='status not in ("Done") AND assignee = currentUser()')
    jql = compose_jql(cfg)
    assert (
        jql
        == 'project = "PROJ" AND (status not in ("Done") AND assignee = currentUser())'
    )


# ---------------------------------------------------------------------------
# JiraTrackerConfig drops email/api_token
# ---------------------------------------------------------------------------


def test_config_rejects_legacy_email_and_api_token() -> None:
    """Operators with old configs get a clear message, not silent acceptance."""
    with pytest.raises(ValueError, match="acli auth login"):
        TrackerConfig.from_dict(
            {
                "kind": "jira",
                "base_url": "https://x.atlassian.net",
                "project_key": "PROJ",
                "email": "me@x.com",
                "api_token": "tok",
            }
        )


def test_config_validate_requires_base_url() -> None:
    cfg = TrackerConfig.from_dict(
        {"kind": "jira", "base_url": "", "project_key": "PROJ"}
    )
    with pytest.raises(ValueError, match="base_url is required"):
        cfg.validate()


def test_config_validate_requires_project_key() -> None:
    cfg = TrackerConfig.from_dict(
        {
            "kind": "jira",
            "base_url": "https://x.atlassian.net",
            "project_key": "",
        }
    )
    with pytest.raises(ValueError, match="project_key is required"):
        cfg.validate()


# ---------------------------------------------------------------------------
# fetch_summaries_by_keys (empty list)
# ---------------------------------------------------------------------------


async def test_fetch_summaries_by_keys_empty_short_circuits() -> None:
    tracker = _tracker()
    result = await tracker.fetch_summaries_by_keys([], _tracker_cfg())
    assert result == {}
    await tracker.aclose()


# ---------------------------------------------------------------------------
# Issue.from_jira (unchanged, still part of the contract)
# ---------------------------------------------------------------------------


def test_issue_from_jira_normalizes_labels_lowercase() -> None:
    payload = {
        "id": "10042",
        "key": "PROJ-1",
        "fields": {
            "summary": "t",
            "status": {"name": "To Do"},
            "labels": ["Foo", "BAR"],
            "issuelinks": [],
            "parent": None,
            "description": None,
            "priority": None,
            "created": None,
            "updated": None,
        },
    }
    issue = Issue.from_jira(payload, "https://x.atlassian.net")
    assert issue.labels == ("foo", "bar")
    assert issue.url == "https://x.atlassian.net/browse/PROJ-1"
    assert issue.source == "jira"
    assert issue.body_text is None


def test_issue_from_jira_collects_only_inward_blockers() -> None:
    payload = {
        "id": "1",
        "key": "PROJ-1",
        "fields": {
            "summary": "t",
            "status": {"name": "To Do"},
            "labels": [],
            "parent": None,
            "description": None,
            "priority": None,
            "created": None,
            "updated": None,
            "issuelinks": [
                {
                    "type": {"inward": "is blocked by"},
                    "inwardIssue": {
                        "key": "PROJ-100",
                        "fields": {"status": {"name": "In Progress"}},
                    },
                },
                {"type": {"inward": "relates to"}, "inwardIssue": {"key": "PROJ-200"}},
                {
                    "type": {"inward": "is blocked by"},
                    "outwardIssue": {"key": "PROJ-300"},
                },
            ],
        },
    }
    issue = Issue.from_jira(payload, "https://x")
    assert len(issue.blocked_by) == 1
    assert issue.blocked_by[0].key == "PROJ-100"
    assert issue.blocked_by[0].state == "In Progress"


def test_issue_from_jira_priority_coercion() -> None:
    def payload(pid: object) -> dict:
        return {
            "id": "1",
            "key": "PROJ-1",
            "fields": {
                "summary": "t",
                "status": {"name": "To Do"},
                "labels": [],
                "parent": None,
                "description": None,
                "priority": {"id": pid} if pid is not None else None,
                "issuelinks": [],
                "created": None,
                "updated": None,
            },
        }

    assert Issue.from_jira(payload("3"), "https://x").priority == 3
    assert Issue.from_jira(payload(None), "https://x").priority is None
    assert Issue.from_jira(payload("not a number"), "https://x").priority is None


# ---------------------------------------------------------------------------
# JiraTracker __init__ validation
# ---------------------------------------------------------------------------


def test_init_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url required"):
        JiraTracker(base_url="")


def test_init_no_longer_needs_email_or_token() -> None:
    """Bare base_url is enough — acli handles auth."""
    JiraTracker(base_url="https://x.atlassian.net")


# ---------------------------------------------------------------------------
# fetch_one (acli mock)
# ---------------------------------------------------------------------------


async def test_fetch_one_success() -> None:
    payload = _issue_payload("PROJ-1", "In Progress")
    procs = [
        _FakeAcliProc(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")
    ]
    with (
        patch("asyncio.create_subprocess_exec", _patch_acli(procs)),
        patch(
            "claude_on_the_fly.symphony.tracker.jira.shutil.which",
            return_value="/x/acli",
        ),
    ):
        tracker = _tracker()
        issue = await tracker.fetch_one("PROJ-1")
        assert issue.identifier == "PROJ-1"
        assert issue.state == "In Progress"
        await tracker.aclose()


async def test_fetch_one_not_found() -> None:
    procs = [
        _FakeAcliProc(
            returncode=1,
            stdout=b"",
            stderr=b"\xe2\x9c\x97 Error: Issue does not exist or you do not have permission to see it.",
        )
    ]
    with (
        patch("asyncio.create_subprocess_exec", _patch_acli(procs)),
        patch(
            "claude_on_the_fly.symphony.tracker.jira.shutil.which",
            return_value="/x/acli",
        ),
    ):
        tracker = _tracker()
        with pytest.raises(RuntimeError, match="not found"):
            await tracker.fetch_one("PROJ-99")
        await tracker.aclose()


async def test_fetch_one_raises_on_generic_acli_error() -> None:
    procs = [
        _FakeAcliProc(returncode=2, stdout=b"", stderr=b"some unrelated acli failure")
    ]
    with (
        patch("asyncio.create_subprocess_exec", _patch_acli(procs)),
        patch(
            "claude_on_the_fly.symphony.tracker.jira.shutil.which",
            return_value="/x/acli",
        ),
    ):
        tracker = _tracker()
        with pytest.raises(JiraAcliError, match="acli exit=2"):
            await tracker.fetch_one("PROJ-1")
        await tracker.aclose()


async def test_fetch_one_raises_when_acli_missing() -> None:
    with patch(
        "claude_on_the_fly.symphony.tracker.jira.shutil.which", return_value=None
    ):
        tracker = _tracker()
        with pytest.raises(JiraAcliError, match="acli is not installed"):
            await tracker.fetch_one("PROJ-1")
        await tracker.aclose()


# ---------------------------------------------------------------------------
# fetch_summaries_by_keys
# ---------------------------------------------------------------------------


async def test_fetch_summaries_by_keys_marks_jql_membership() -> None:
    """The membership query `key in (...) AND (jql)` returns only matching
    keys. fetch_summaries_by_keys returns a summary for EVERY input key:
    matches get matches_jql=True + real status, the rest get matches_jql=False.
    """
    # Query returned PROJ-1 (still matches the jql) but not PROJ-2.
    search_payload = [
        {"key": "PROJ-1", "fields": {"status": {"name": "In Progress"}}},
    ]
    procs = [
        _FakeAcliProc(
            returncode=0, stdout=json.dumps(search_payload).encode(), stderr=b""
        )
    ]
    cfg = _tracker_cfg(jql='status != "Done"')
    with (
        patch("asyncio.create_subprocess_exec", _patch_acli(procs)),
        patch(
            "claude_on_the_fly.symphony.tracker.jira.shutil.which",
            return_value="/x/acli",
        ),
    ):
        tracker = _tracker()
        result = await tracker.fetch_summaries_by_keys(["PROJ-1", "PROJ-2"], cfg)
        assert result["PROJ-1"].extra["matches_jql"] is True
        assert result["PROJ-1"].state == "In Progress"
        # PROJ-2 dropped out of the jql → returned with matches_jql=False.
        assert result["PROJ-2"].extra["matches_jql"] is False
        await tracker.aclose()


# ---------------------------------------------------------------------------
# fetch_candidates: search then per-key view
# ---------------------------------------------------------------------------


async def test_fetch_candidates_two_pass() -> None:
    """Search returns keys only (acli's allowlist) then per-key view."""
    search_payload = [
        {"key": "PROJ-10", "fields": {"status": {"name": "To Do"}}},
        {"key": "PROJ-11", "fields": {"status": {"name": "In Progress"}}},
    ]
    procs = [
        _FakeAcliProc(
            returncode=0, stdout=json.dumps(search_payload).encode(), stderr=b""
        ),
        _FakeAcliProc(
            returncode=0,
            stdout=json.dumps(_issue_payload("PROJ-10", "To Do")).encode(),
            stderr=b"",
        ),
        _FakeAcliProc(
            returncode=0,
            stdout=json.dumps(_issue_payload("PROJ-11", "In Progress")).encode(),
            stderr=b"",
        ),
    ]
    with (
        patch("asyncio.create_subprocess_exec", _patch_acli(procs)),
        patch(
            "claude_on_the_fly.symphony.tracker.jira.shutil.which",
            return_value="/x/acli",
        ),
    ):
        tracker = _tracker()
        cfg = _tracker_cfg()
        issues = await tracker.fetch_candidates(cfg)
        assert {i.identifier for i in issues} == {"PROJ-10", "PROJ-11"}
        await tracker.aclose()


async def test_fetch_candidates_view_failure_is_skipped() -> None:
    """If one view call fails, the others still return."""
    search_payload = [
        {"key": "PROJ-10", "fields": {"status": {"name": "To Do"}}},
        {"key": "PROJ-11", "fields": {"status": {"name": "In Progress"}}},
    ]
    procs = [
        _FakeAcliProc(
            returncode=0, stdout=json.dumps(search_payload).encode(), stderr=b""
        ),
        _FakeAcliProc(
            returncode=0,
            stdout=json.dumps(_issue_payload("PROJ-10", "To Do")).encode(),
            stderr=b"",
        ),
        _FakeAcliProc(returncode=1, stdout=b"", stderr=b"transient acli failure"),
    ]
    with (
        patch("asyncio.create_subprocess_exec", _patch_acli(procs)),
        patch(
            "claude_on_the_fly.symphony.tracker.jira.shutil.which",
            return_value="/x/acli",
        ),
    ):
        tracker = _tracker()
        cfg = _tracker_cfg()
        issues = await tracker.fetch_candidates(cfg)
        assert [i.identifier for i in issues] == ["PROJ-10"]
        await tracker.aclose()


async def test_fetch_candidates_empty_search() -> None:
    procs = [_FakeAcliProc(returncode=0, stdout=b"[]", stderr=b"")]
    with (
        patch("asyncio.create_subprocess_exec", _patch_acli(procs)),
        patch(
            "claude_on_the_fly.symphony.tracker.jira.shutil.which",
            return_value="/x/acli",
        ),
    ):
        tracker = _tracker()
        cfg = _tracker_cfg()
        issues = await tracker.fetch_candidates(cfg)
        assert issues == []
        await tracker.aclose()


# ---------------------------------------------------------------------------
# async context manager
# ---------------------------------------------------------------------------


async def test_async_context_manager() -> None:
    async with JiraTracker(base_url="https://j.example.com") as tracker:
        assert isinstance(tracker, JiraTracker)


# ---------------------------------------------------------------------------
# is_terminal / is_active predicates — JQL-membership model
# ---------------------------------------------------------------------------


def _bare_tracker() -> JiraTracker:
    return JiraTracker(base_url="https://j.example.com")


def test_is_terminal_always_false() -> None:
    """Jira has no terminal status list anymore — cleanup is deferred to
    startup GC, so is_terminal is always False."""
    tracker = _bare_tracker()
    cfg = _tracker_cfg()
    assert tracker.is_terminal(IssueSummary(state="Done"), cfg) is False
    assert tracker.is_terminal(IssueSummary(state="In Progress"), cfg) is False


def test_is_active_reads_matches_jql_flag() -> None:
    tracker = _bare_tracker()
    cfg = _tracker_cfg()
    assert (
        tracker.is_active(IssueSummary(state="x", extra={"matches_jql": True}), cfg)
        is True
    )
    assert (
        tracker.is_active(IssueSummary(state="x", extra={"matches_jql": False}), cfg)
        is False
    )


def test_is_active_false_when_flag_absent() -> None:
    """A summary that didn't go through fetch_summaries_by_keys (no flag) is
    treated as not-matching — safer to stop than to keep a stale worker."""
    tracker = _bare_tracker()
    assert tracker.is_active(IssueSummary(state="x", extra={}), _tracker_cfg()) is False


def test_issue_to_summary_packs_state_and_labels() -> None:
    tracker = _bare_tracker()
    payload = {
        "id": "10",
        "key": "PROJ-1",
        "fields": {
            "summary": "t",
            "status": {"name": "In Progress"},
            "labels": ["stevedore", "needs-design"],
            "issuelinks": [],
            "parent": None,
            "description": None,
            "priority": None,
            "created": None,
            "updated": None,
        },
    }
    issue = Issue.from_jira(payload, "https://x")
    summary = tracker.issue_to_summary(issue)
    assert summary.state == "In Progress"
    assert summary.extra["labels"] == ("stevedore", "needs-design")
