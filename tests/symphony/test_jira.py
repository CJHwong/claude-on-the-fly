"""JQL composition, Issue normalization, JiraTracker HTTP methods."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from claude_on_the_fly.symphony.config import TrackerConfig
from claude_on_the_fly.symphony.tracker.issue import Issue
from claude_on_the_fly.symphony.tracker.jira import JiraTracker, compose_jql


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tracker(**overrides: object) -> TrackerConfig:
    base = {
        "kind": "jira",
        "base_url": "https://x.atlassian.net",
        "email": "me@x.com",
        "api_token": "tok",
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


def _make_mock_client(status_code: int, json_body: dict) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_body
    mock_resp.raise_for_status = MagicMock()
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=mock_resp
        )

    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.post = AsyncMock(return_value=mock_resp)
    client.aclose = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# compose_jql
# ---------------------------------------------------------------------------


def test_compose_jql_minimal() -> None:
    cfg = _tracker()
    jql = compose_jql(cfg)
    assert 'project = "PROJ"' in jql
    assert '"To Do"' in jql
    assert '"In Progress"' in jql


def test_compose_jql_quotes_multi_word_states() -> None:
    cfg = _tracker(active_states=["To Do", "In Progress", "Pending Review"])
    jql = compose_jql(cfg)
    assert '"Pending Review"' in jql


def test_compose_jql_appends_extra() -> None:
    cfg = _tracker(jql_extra='AND labels = "stevedore"')
    jql = compose_jql(cfg)
    assert jql.endswith('AND labels = "stevedore"')


# ---------------------------------------------------------------------------
# fetch_states_by_keys (empty list)
# ---------------------------------------------------------------------------


def test_fetch_states_by_keys_empty_short_circuits() -> None:
    import asyncio

    tracker = JiraTracker(
        base_url="https://x.atlassian.net",
        email="me@x.com",
        api_token="tok",
    )
    result = asyncio.run(tracker.fetch_states_by_keys([]))
    assert result == {}
    asyncio.run(tracker.aclose())


# ---------------------------------------------------------------------------
# Issue.from_jira
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
        JiraTracker(base_url="", email="e@x.com", api_token="tok")


def test_init_requires_email_and_token() -> None:
    with pytest.raises(ValueError, match="email and api_token required"):
        JiraTracker(base_url="https://x.atlassian.net", email="", api_token="")
    with pytest.raises(ValueError, match="email and api_token required"):
        JiraTracker(base_url="https://x.atlassian.net", email="e@x.com", api_token="")


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


def test_from_env_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", "https://j.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "bot@j.example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "s3cret")
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _make_mock_client(200, {})
        tracker = JiraTracker.from_env()
        assert tracker._base_url == "https://j.example.com"


def test_from_env_missing_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="Missing env var"):
        JiraTracker.from_env()


# ---------------------------------------------------------------------------
# fetch_one (HTTP mock)
# ---------------------------------------------------------------------------


@patch("httpx.AsyncClient")
async def test_fetch_one_success(mock_client_cls: MagicMock) -> None:
    mock_client_cls.return_value = _make_mock_client(
        200, _issue_payload("PROJ-1", "In Progress")
    )
    tracker = JiraTracker(
        base_url="https://j.example.com", email="e@x.com", api_token="tok"
    )
    issue = await tracker.fetch_one("PROJ-1")
    assert issue.identifier == "PROJ-1"
    assert issue.state == "In Progress"
    await tracker.aclose()


@patch("httpx.AsyncClient")
async def test_fetch_one_not_found(mock_client_cls: MagicMock) -> None:
    mock_client_cls.return_value = _make_mock_client(404, {})
    tracker = JiraTracker(
        base_url="https://j.example.com", email="e@x.com", api_token="tok"
    )
    with pytest.raises(RuntimeError, match="not found"):
        await tracker.fetch_one("PROJ-99")
    await tracker.aclose()


@patch("httpx.AsyncClient")
async def test_fetch_one_raises_on_error(mock_client_cls: MagicMock) -> None:
    mock_client_cls.return_value = _make_mock_client(500, {})
    tracker = JiraTracker(
        base_url="https://j.example.com", email="e@x.com", api_token="tok"
    )
    with pytest.raises(httpx.HTTPStatusError):
        await tracker.fetch_one("PROJ-1")
    await tracker.aclose()


# ---------------------------------------------------------------------------
# fetch_states_by_keys (non-empty, HTTP mock)
# ---------------------------------------------------------------------------


@patch("httpx.AsyncClient")
async def test_fetch_states_by_keys_non_empty(mock_client_cls: MagicMock) -> None:
    jira_resp = {
        "issues": [
            {"key": "PROJ-1", "fields": {"status": {"name": "In Progress"}}},
            {"key": "PROJ-2", "fields": {"status": {"name": "Done"}}},
        ]
    }
    mock_client_cls.return_value = _make_mock_client(200, jira_resp)
    tracker = JiraTracker(
        base_url="https://j.example.com", email="e@x.com", api_token="tok"
    )
    result = await tracker.fetch_states_by_keys(["PROJ-1", "PROJ-2"])
    assert result == {"PROJ-1": "In Progress", "PROJ-2": "Done"}
    await tracker.aclose()


# ---------------------------------------------------------------------------
# fetch_candidates (HTTP mock)
# ---------------------------------------------------------------------------


@patch("httpx.AsyncClient")
async def test_fetch_candidates(mock_client_cls: MagicMock) -> None:
    jira_resp = {"issues": [_issue_payload("PROJ-10", "To Do")]}
    mock_client_cls.return_value = _make_mock_client(200, jira_resp)
    tracker = JiraTracker(
        base_url="https://j.example.com", email="e@x.com", api_token="tok"
    )
    cfg = _tracker()
    issues = await tracker.fetch_candidates(cfg)
    assert len(issues) == 1
    assert issues[0].identifier == "PROJ-10"
    await tracker.aclose()


# ---------------------------------------------------------------------------
# async context manager
# ---------------------------------------------------------------------------


@patch("httpx.AsyncClient")
async def test_async_context_manager(mock_client_cls: MagicMock) -> None:
    mock_client_cls.return_value = _make_mock_client(200, {})
    async with JiraTracker(
        base_url="https://j.example.com", email="e@x.com", api_token="tok"
    ) as tracker:
        assert isinstance(tracker, JiraTracker)
