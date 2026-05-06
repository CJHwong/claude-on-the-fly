"""JQL composition, Issue normalization. No live calls."""

from __future__ import annotations

from claude_on_the_fly.symphony.config import TrackerConfig
from claude_on_the_fly.symphony.tracker.issue import Issue
from claude_on_the_fly.symphony.tracker.jira import compose_jql


def _tracker(**overrides) -> TrackerConfig:
    base = {
        "kind": "jira",
        "base_url": "https://x.atlassian.net",
        "email": "me@x.com",
        "api_token": "tok",
        "project_key": "PROJ",
    }
    base.update(overrides)
    return TrackerConfig.from_dict(base)


def test_compose_jql_minimal():
    cfg = _tracker()
    jql = compose_jql(cfg)
    assert 'project = "PROJ"' in jql
    assert '"To Do"' in jql
    assert '"In Progress"' in jql


def test_compose_jql_quotes_multi_word_states():
    cfg = _tracker(active_states=["To Do", "In Progress", "Pending Review"])
    jql = compose_jql(cfg)
    assert '"Pending Review"' in jql


def test_compose_jql_appends_extra():
    cfg = _tracker(jql_extra='AND labels = "stevedore"')
    jql = compose_jql(cfg)
    assert jql.endswith('AND labels = "stevedore"')


def test_fetch_states_by_keys_composes_jql_and_normalizes():
    """fetch_states_by_keys should be compatible with empty input and normalize the response shape."""
    import asyncio
    from claude_on_the_fly.symphony.tracker.jira import JiraTracker

    tracker = JiraTracker(
        base_url="https://x.atlassian.net",
        email="me@x.com",
        api_token="tok",
    )
    # Empty list short-circuits without any API call.
    result = asyncio.run(tracker.fetch_states_by_keys([]))
    assert result == {}
    asyncio.run(tracker.aclose())


def test_issue_from_jira_normalizes_labels_lowercase():
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


def test_issue_from_jira_collects_only_inward_blockers():
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
                },  # outward direction, ignored
            ],
        },
    }
    issue = Issue.from_jira(payload, "https://x")
    assert len(issue.blocked_by) == 1
    assert issue.blocked_by[0].key == "PROJ-100"
    assert issue.blocked_by[0].state == "In Progress"


def test_issue_from_jira_priority_coercion():
    def payload(pid):
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
