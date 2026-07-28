"""GitHubTracker: identifier parsing, gh CLI mocking, SHA-aware dedup."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.symphony.config import GitHubTrackerConfig
from claude_on_the_fly.symphony.tracker.github import (
    GhCliError,
    GitHubTracker,
    parse_identifier,
)
from claude_on_the_fly.symphony.tracker.issue import IssueSummary

# ---------------------------------------------------------------------------
# parse_identifier
# ---------------------------------------------------------------------------


def test_parse_identifier_happy_path() -> None:
    assert parse_identifier("owner/repo#123") == ("owner", "repo", 123)
    assert parse_identifier("hardcoretech/fms#42") == ("hardcoretech", "fms", 42)
    assert parse_identifier("user.name/some-repo.io#1") == (
        "user.name",
        "some-repo.io",
        1,
    )


def test_parse_identifier_rejects_garbage() -> None:
    for bad in ("", "PROJ-1", "owner/repo", "owner/repo#", "#1", "owner#1"):
        with pytest.raises(ValueError, match="invalid GitHub PR identifier"):
            parse_identifier(bad)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_init_fails_when_gh_missing() -> None:
    with (
        patch(
            "claude_on_the_fly.symphony.tracker.github.shutil.which", return_value=None
        ),
        pytest.raises(RuntimeError, match=r"gh.*CLI not found"),
    ):
        GitHubTracker()


def test_from_config_returns_tracker() -> None:
    cfg = GitHubTrackerConfig(kind="github")
    with patch(
        "claude_on_the_fly.symphony.tracker.github.shutil.which",
        return_value="/usr/local/bin/gh",
    ):
        t = GitHubTracker.from_config(cfg)
    assert isinstance(t, GitHubTracker)


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def _tracker_cfg() -> GitHubTrackerConfig:
    return GitHubTrackerConfig(kind="github")


def _tracker() -> GitHubTracker:
    with patch(
        "claude_on_the_fly.symphony.tracker.github.shutil.which",
        return_value="/usr/local/bin/gh",
    ):
        return GitHubTracker()


def test_is_terminal_true_for_closed_and_merged() -> None:
    t = _tracker()
    cfg = _tracker_cfg()
    assert t.is_terminal(IssueSummary(state="closed"), cfg) is True
    assert t.is_terminal(IssueSummary(state="merged"), cfg) is True


def test_is_terminal_false_for_open() -> None:
    t = _tracker()
    assert t.is_terminal(IssueSummary(state="open"), _tracker_cfg()) is False


def test_is_active_false_when_user_reviewed_current_head() -> None:
    """The gate for GitHub is SHA-aware: a review at the current head means
    the agent has done its job and the worker should exit."""
    t = _tracker()
    cfg = _tracker_cfg()
    reviewed = IssueSummary(state="open", extra={"user_reviewed_current_head": True})
    assert t.is_active(reviewed, cfg) is False


def test_is_active_true_when_open_and_not_yet_reviewed_at_head() -> None:
    """Never reviewed OR reviewed only at older SHAs (= new commits since)
    both surface as `user_reviewed_current_head=False` → keep working."""
    t = _tracker()
    cfg = _tracker_cfg()
    fresh = IssueSummary(state="open", extra={"user_reviewed_current_head": False})
    assert t.is_active(fresh, cfg) is True


def test_is_active_treats_missing_flag_as_not_reviewed() -> None:
    """Defensive default: missing flag → assume not reviewed at head."""
    t = _tracker()
    cfg = _tracker_cfg()
    summary = IssueSummary(state="open", extra={})
    assert t.is_active(summary, cfg) is True


def test_is_active_requires_open_state() -> None:
    """Even if not-yet-reviewed, a closed/merged PR is not active."""
    t = _tracker()
    cfg = _tracker_cfg()
    closed = IssueSummary(state="closed", extra={"user_reviewed_current_head": False})
    assert t.is_active(closed, cfg) is False


def test_issue_to_summary_carries_review_flag() -> None:
    """The worker fetches a refreshed Issue post-turn; issue_to_summary
    must surface the SHA-aware flag so the predicates can act."""
    from claude_on_the_fly.symphony.tracker.issue import Issue

    t = _tracker()
    issue = Issue(
        id="pr_node",
        identifier="owner/repo#1",
        title="t",
        state="open",
        description_raw=None,
        priority=None,
        labels=(),
        blocked_by=(),
        parent_key=None,
        url="https://gh/x",
        created_at=None,
        updated_at=None,
        source="github",
        extra={"user_reviewed_current_head": True, "head_ref_oid": "abc"},
    )
    summary = t.issue_to_summary(issue)
    assert summary.state == "open"
    assert summary.extra["user_reviewed_current_head"] is True


# ---------------------------------------------------------------------------
# _user_done_with_head (SHA-match AND not re-requested)
# ---------------------------------------------------------------------------


def test_user_done_with_head_picks_latest_review_only() -> None:
    """Older reviews at different SHAs shouldn't fool the check. The user's
    LATEST review (by submittedAt) is what counts."""
    payload = {
        "headRefOid": "newSHA",
        "reviews": [
            {
                "author": {"login": "CJHwong"},
                "commit": {"oid": "oldSHA"},
                "submittedAt": "2026-05-20T00:00:00Z",
            },
            {
                "author": {"login": "CJHwong"},
                "commit": {"oid": "newSHA"},
                "submittedAt": "2026-05-21T00:00:00Z",
            },
        ],
    }
    assert GitHubTracker._user_done_with_head(payload, "CJHwong") is True


def test_user_done_with_head_returns_false_when_only_old_sha() -> None:
    """Reviewed at older SHA, then author pushed new commits → re-review needed."""
    payload = {
        "headRefOid": "newSHA",
        "reviews": [
            {
                "author": {"login": "CJHwong"},
                "commit": {"oid": "oldSHA"},
                "submittedAt": "2026-05-20T00:00:00Z",
            },
        ],
    }
    assert GitHubTracker._user_done_with_head(payload, "CJHwong") is False


def test_user_done_with_head_returns_false_when_never_reviewed() -> None:
    payload = {
        "headRefOid": "newSHA",
        "reviews": [
            {
                "author": {"login": "someone-else"},
                "commit": {"oid": "newSHA"},
                "submittedAt": "2026-05-21T00:00:00Z",
            },
        ],
    }
    assert GitHubTracker._user_done_with_head(payload, "CJHwong") is False


def test_user_done_with_head_returns_false_when_head_missing() -> None:
    """Defensive: if the payload has no headRefOid (e.g. malformed response),
    treat as not-done so the worker keeps running rather than silently
    exiting on missing data."""
    payload = {
        "reviews": [
            {
                "author": {"login": "CJHwong"},
                "commit": {"oid": "anySHA"},
                "submittedAt": "2026-05-21T00:00:00Z",
            },
        ],
    }
    assert GitHubTracker._user_done_with_head(payload, "CJHwong") is False


def test_user_done_with_head_returns_false_when_user_re_requested_rest_shape() -> None:
    """Re-request override: even with a review at the current head, if the
    user is back in `reviewRequests` (REST shape from `gh pr view`), treat
    as not-done so the worker re-engages."""
    payload = {
        "headRefOid": "currentSHA",
        "reviews": [
            {
                "author": {"login": "CJHwong"},
                "commit": {"oid": "currentSHA"},
                "submittedAt": "2026-05-21T12:00:00Z",
            },
        ],
        "reviewRequests": [{"__typename": "User", "login": "CJHwong"}],
    }
    assert GitHubTracker._user_done_with_head(payload, "CJHwong") is False


def test_user_done_with_head_returns_false_when_user_re_requested_graphql_shape() -> (
    None
):
    """Same as above but with the GraphQL `{nodes: [{requestedReviewer}]}` shape."""
    payload = {
        "headRefOid": "currentSHA",
        "reviews": {
            "nodes": [
                {
                    "author": {"login": "CJHwong"},
                    "commit": {"oid": "currentSHA"},
                    "submittedAt": "2026-05-21T12:00:00Z",
                },
            ]
        },
        "reviewRequests": {
            "nodes": [{"requestedReviewer": {"__typename": "User", "login": "CJHwong"}}]
        },
    }
    assert GitHubTracker._user_done_with_head(payload, "CJHwong") is False


def test_user_done_with_head_ignores_other_users_in_review_requests() -> None:
    """Someone else being re-requested doesn't reactivate the current user."""
    payload = {
        "headRefOid": "currentSHA",
        "reviews": [
            {
                "author": {"login": "CJHwong"},
                "commit": {"oid": "currentSHA"},
                "submittedAt": "2026-05-21T12:00:00Z",
            },
        ],
        "reviewRequests": [{"__typename": "User", "login": "someone-else"}],
    }
    assert GitHubTracker._user_done_with_head(payload, "CJHwong") is True


# ---------------------------------------------------------------------------
# Subprocess wiring
# ---------------------------------------------------------------------------


def _stub_proc(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


@pytest.mark.asyncio
async def test_run_gh_returns_stdout_on_success() -> None:
    t = _tracker()
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_stub_proc(b"hello\n")),
    ):
        out = await t._run_gh(["api", "user"])
    assert out == b"hello\n"


@pytest.mark.asyncio
async def test_run_gh_raises_on_nonzero_exit() -> None:
    t = _tracker()
    with (
        patch(
            "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_stub_proc(b"", b"not found", returncode=1)),
        ),
        pytest.raises(GhCliError) as excinfo,
    ):
        await t._run_gh(["api", "user"])
    assert "not found" in str(excinfo.value)
    assert excinfo.value.returncode == 1


@pytest.mark.asyncio
async def test_get_login_caches_result() -> None:
    t = _tracker()
    fake_proc = _stub_proc(b"CJHwong\n")
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_spawn:
        a = await t._get_login()
        b = await t._get_login()
    assert a == "CJHwong"
    assert b == "CJHwong"
    # Subprocess was only invoked once across two calls thanks to the cache.
    assert mock_spawn.call_count == 1


# ---------------------------------------------------------------------------
# fetch_candidates (GraphQL)
# ---------------------------------------------------------------------------


def _graphql_pr_node(
    *,
    number: int,
    repo: str = "owner/repo",
    head_oid: str = "currentSHA",
    user_latest_oid: str | None = None,
    user_login: str = "CJHwong",
    state: str = "OPEN",
    labels: list[str] | None = None,
    body: str = "PR body",
    review_requested_logins: list[str] | None = None,
) -> dict:
    # The search GraphQL query now pulls the full `reviews` list and uses
    # submittedAt ordering (same algorithm as fetch_one's REST data), so the
    # fixture mirrors that shape.
    reviews = []
    if user_latest_oid is not None:
        reviews.append(
            {
                "author": {"login": user_login},
                "submittedAt": "2026-05-21T11:00:00Z",
                "commit": {"oid": user_latest_oid},
            }
        )
    # An unrelated reviewer's entry to make sure we filter by login.
    reviews.append(
        {
            "author": {"login": "someone-else"},
            "submittedAt": "2026-05-21T12:00:00Z",
            "commit": {"oid": "irrelevant"},
        }
    )
    review_requests_nodes = [
        {"requestedReviewer": {"__typename": "User", "login": login}}
        for login in (review_requested_logins or [])
    ]
    return {
        "id": f"PR_id_{number}",
        "number": number,
        "title": f"PR {number}",
        "body": body,
        "url": f"https://github.com/{repo}/pull/{number}",
        "state": state,
        "createdAt": "2026-05-20T10:00:00Z",
        "updatedAt": "2026-05-21T11:00:00Z",
        "headRefOid": head_oid,
        "repository": {"nameWithOwner": repo},
        "labels": {"nodes": [{"name": n} for n in (labels or [])]},
        "reviewRequests": {"nodes": review_requests_nodes},
        "reviews": {"nodes": reviews},
    }


def _graphql_envelope(nodes: list[dict]) -> bytes:
    return json.dumps({"data": {"search": {"nodes": nodes}}}).encode()


@pytest.mark.asyncio
async def test_fetch_candidates_filters_already_reviewed_at_head() -> None:
    """SHA-aware dedup: PRs where the user's latest review SHA matches the
    PR's headRefOid are skipped — no new code to re-review."""
    t = _tracker()
    t._login = "CJHwong"
    nodes = [
        _graphql_pr_node(
            number=1, head_oid="abc123", user_latest_oid=None
        ),  # never reviewed → keep
        _graphql_pr_node(
            number=2, head_oid="abc123", user_latest_oid="abc123"
        ),  # reviewed at head → skip
        _graphql_pr_node(
            number=3, head_oid="newSHA", user_latest_oid="oldSHA"
        ),  # reviewed at older SHA → keep
    ]
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_stub_proc(_graphql_envelope(nodes))),
    ):
        candidates = await t.fetch_candidates(_tracker_cfg())

    identifiers = {c.identifier for c in candidates}
    assert "owner/repo#1" in identifiers  # never reviewed
    assert "owner/repo#3" in identifiers  # reviewed only at old SHA
    assert "owner/repo#2" not in identifiers  # already reviewed at head
    for c in candidates:
        assert c.extra["user_reviewed_current_head"] is False


@pytest.mark.asyncio
async def test_fetch_candidates_keeps_pr_when_user_re_requested_at_same_head() -> None:
    """Re-request override: a PR where the user's last review matches the
    current head SHA is STILL a candidate if the user is back in
    `reviewRequests` (someone re-requested after their prior review)."""
    t = _tracker()
    t._login = "CJHwong"
    nodes = [
        # SHA matches head AND no re-request → skip (existing dedup).
        _graphql_pr_node(number=1, head_oid="abc", user_latest_oid="abc"),
        # SHA matches head BUT user re-requested → keep (new behavior).
        _graphql_pr_node(
            number=2,
            head_oid="abc",
            user_latest_oid="abc",
            review_requested_logins=["CJHwong"],
        ),
        # SHA matches head, someone else re-requested → skip (still done).
        _graphql_pr_node(
            number=3,
            head_oid="abc",
            user_latest_oid="abc",
            review_requested_logins=["someone-else"],
        ),
    ]
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_stub_proc(_graphql_envelope(nodes))),
    ):
        candidates = await t.fetch_candidates(_tracker_cfg())

    identifiers = {c.identifier for c in candidates}
    assert "owner/repo#1" not in identifiers
    assert "owner/repo#2" in identifiers
    assert "owner/repo#3" not in identifiers


@pytest.mark.asyncio
async def test_fetch_candidates_query_pulls_review_requests() -> None:
    """The GraphQL query must include `reviewRequests` so the dispatcher
    can detect the re-request override."""
    t = _tracker()
    t._login = "CJHwong"
    captured: dict = {}

    async def capture(*args, **kwargs):
        captured["args"] = list(args)
        return _stub_proc(_graphql_envelope([]))

    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=capture,
    ):
        await t.fetch_candidates(_tracker_cfg())

    joined = " ".join(captured["args"])
    assert "reviewRequests" in joined
    assert "requestedReviewer" in joined


@pytest.mark.asyncio
async def test_fetch_candidates_query_uses_user_review_requested_not_team() -> None:
    """The candidate query must use `user-review-requested:@me` so
    team-level review requests don't sneak in."""
    t = _tracker()
    t._login = "CJHwong"
    captured: dict = {}

    async def capture(*args, **kwargs):
        captured["args"] = list(args)
        return _stub_proc(_graphql_envelope([]))

    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=capture,
    ):
        await t.fetch_candidates(_tracker_cfg())

    cmd = captured["args"]
    # The query string is passed via `-f q=...`.
    joined = " ".join(cmd)
    assert "user-review-requested:@me" in joined
    # Defensive: must NOT use the team-inclusive `review-requested:@me`.
    # (`user-review-requested:` is a strict superset substring of the team
    # form, so check by token presence rather than naive `in`.)
    assert "team-review-requested" not in joined


@pytest.mark.asyncio
async def test_fetch_candidates_empty_response() -> None:
    t = _tracker()
    t._login = "CJHwong"
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_stub_proc(_graphql_envelope([]))),
    ):
        result = await t.fetch_candidates(_tracker_cfg())
    assert result == []


# ---------------------------------------------------------------------------
# fetch_one
# ---------------------------------------------------------------------------


def _pr_view_payload(
    *,
    state: str = "OPEN",
    user_review_oid: str | None = None,
    head_oid: str = "headSHA",
    review_requested_logins: list[str] | None = None,
) -> dict:
    reviews = []
    if user_review_oid is not None:
        reviews.append(
            {
                "author": {"login": "CJHwong"},
                "commit": {"oid": user_review_oid},
                "submittedAt": "2026-05-21T12:00:00Z",
                "state": "COMMENTED",
            }
        )
    reviews.append(
        {
            "author": {"login": "someone-else"},
            "commit": {"oid": "irrelevant"},
            "submittedAt": "2026-05-20T08:00:00Z",
            "state": "COMMENTED",
        }
    )
    return {
        "id": "PR_id",
        "number": 42,
        "title": "Fix login",
        "body": "PR description",
        "labels": [{"name": "bug"}],
        "state": state,
        "createdAt": "2026-05-20T10:00:00Z",
        "updatedAt": "2026-05-21T11:00:00Z",
        "headRefOid": head_oid,
        "url": "https://github.com/owner/repo/pull/42",
        "reviews": reviews,
        "reviewRequests": [
            {"__typename": "User", "login": login}
            for login in (review_requested_logins or [])
        ],
    }


@pytest.mark.asyncio
async def test_fetch_one_marks_reviewed_when_sha_matches() -> None:
    t = _tracker()
    t._login = "CJHwong"
    payload = _pr_view_payload(head_oid="SHA1", user_review_oid="SHA1")
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_stub_proc(json.dumps(payload).encode())),
    ):
        issue = await t.fetch_one("owner/repo#42")
    assert issue.extra["user_reviewed_current_head"] is True
    assert issue.extra["head_ref_oid"] == "SHA1"


@pytest.mark.asyncio
async def test_fetch_one_marks_not_reviewed_when_sha_differs() -> None:
    """Reviewed at older SHA → not reviewed at current head → keep working."""
    t = _tracker()
    t._login = "CJHwong"
    payload = _pr_view_payload(head_oid="newSHA", user_review_oid="oldSHA")
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_stub_proc(json.dumps(payload).encode())),
    ):
        issue = await t.fetch_one("owner/repo#42")
    assert issue.extra["user_reviewed_current_head"] is False


@pytest.mark.asyncio
async def test_fetch_one_marks_not_done_when_user_re_requested_at_same_head() -> None:
    """SHA matches head but user is back in reviewRequests → not done."""
    t = _tracker()
    t._login = "CJHwong"
    payload = _pr_view_payload(
        head_oid="SHA1",
        user_review_oid="SHA1",
        review_requested_logins=["CJHwong"],
    )
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_stub_proc(json.dumps(payload).encode())),
    ):
        issue = await t.fetch_one("owner/repo#42")
    assert issue.extra["user_reviewed_current_head"] is False


@pytest.mark.asyncio
async def test_fetch_one_marks_not_reviewed_when_never_reviewed() -> None:
    t = _tracker()
    t._login = "CJHwong"
    payload = _pr_view_payload(head_oid="SHA1", user_review_oid=None)
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_stub_proc(json.dumps(payload).encode())),
    ):
        issue = await t.fetch_one("owner/repo#42")
    assert issue.extra["user_reviewed_current_head"] is False


@pytest.mark.asyncio
async def test_fetch_one_normalizes_merged_state() -> None:
    t = _tracker()
    t._login = "CJHwong"
    payload = _pr_view_payload(state="MERGED")
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_stub_proc(json.dumps(payload).encode())),
    ):
        issue = await t.fetch_one("owner/repo#42")
    assert issue.state == "merged"


# ---------------------------------------------------------------------------
# fetch_summaries_by_keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_summaries_applies_sha_aware_review_check() -> None:
    t = _tracker()
    t._login = "CJHwong"

    payloads = [
        # PR 1: reviewed at older SHA → should still be active (new commits).
        json.dumps(
            {
                "state": "OPEN",
                "headRefOid": "newSHA",
                "reviews": [
                    {
                        "author": {"login": "CJHwong"},
                        "commit": {"oid": "oldSHA"},
                        "submittedAt": "2026-05-20T00:00:00Z",
                    }
                ],
            }
        ).encode(),
        # PR 2: reviewed at current SHA → done.
        json.dumps(
            {
                "state": "OPEN",
                "headRefOid": "SHA2",
                "reviews": [
                    {
                        "author": {"login": "CJHwong"},
                        "commit": {"oid": "SHA2"},
                        "submittedAt": "2026-05-21T00:00:00Z",
                    }
                ],
            }
        ).encode(),
    ]
    procs = [_stub_proc(p) for p in payloads]

    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=procs),
    ):
        result = await t.fetch_summaries_by_keys(["owner/repo#1", "owner/repo#2"])

    assert result["owner/repo#1"].extra["user_reviewed_current_head"] is False
    assert result["owner/repo#2"].extra["user_reviewed_current_head"] is True


@pytest.mark.asyncio
async def test_fetch_summaries_treats_re_requested_user_as_not_done() -> None:
    """A worker on a PR where the user got re-requested at the same head SHA
    must keep running, so `user_reviewed_current_head` surfaces as False."""
    t = _tracker()
    t._login = "CJHwong"
    payload = _stub_proc(
        json.dumps(
            {
                "state": "OPEN",
                "headRefOid": "currentSHA",
                "reviews": [
                    {
                        "author": {"login": "CJHwong"},
                        "commit": {"oid": "currentSHA"},
                        "submittedAt": "2026-05-21T00:00:00Z",
                    }
                ],
                "reviewRequests": [{"__typename": "User", "login": "CJHwong"}],
            }
        ).encode()
    )
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=[payload]),
    ):
        result = await t.fetch_summaries_by_keys(["owner/repo#1"])

    assert result["owner/repo#1"].extra["user_reviewed_current_head"] is False


@pytest.mark.asyncio
async def test_fetch_summaries_drops_missing_pr() -> None:
    """A PR that vanished (404 / private) shouldn't poison the batch."""
    t = _tracker()
    t._login = "CJHwong"

    good = _stub_proc(
        json.dumps({"state": "OPEN", "headRefOid": "x", "reviews": []}).encode()
    )
    bad = _stub_proc(b"", b"not found", returncode=1)

    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=[good, bad]),
    ):
        result = await t.fetch_summaries_by_keys(["owner/repo#1", "owner/repo#999"])

    assert "owner/repo#1" in result
    assert "owner/repo#999" not in result


@pytest.mark.asyncio
async def test_fetch_summaries_empty_input_short_circuits() -> None:
    t = _tracker()
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
    ) as mock_spawn:
        result = await t.fetch_summaries_by_keys([])
    assert result == {}
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_summaries_skips_invalid_identifiers() -> None:
    t = _tracker()
    t._login = "CJHwong"
    payload = _stub_proc(
        json.dumps({"state": "OPEN", "headRefOid": "x", "reviews": []}).encode()
    )
    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=[payload]),
    ):
        result = await t.fetch_summaries_by_keys(["owner/repo#1", "garbage-key"])
    assert list(result.keys()) == ["owner/repo#1"]


@pytest.mark.asyncio
async def test_fetch_candidates_uses_custom_search_query() -> None:
    """When GitHubTrackerConfig.search_query is set, it's sent to gh instead
    of the default."""
    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    t = _tracker()
    t._login = "CJHwong"
    cfg = GitHubTrackerConfig(
        kind="github",
        search_query="is:pr is:open org:gofreight label:bug",
    )
    captured: dict = {}

    async def capture(*args, **kwargs):
        captured["args"] = list(args)
        return _stub_proc(_graphql_envelope([]))

    with patch(
        "claude_on_the_fly.symphony.tracker.github.asyncio.create_subprocess_exec",
        new=capture,
    ):
        await t.fetch_candidates(cfg)

    joined = " ".join(captured["args"])
    assert "org:gofreight" in joined
    assert "label:bug" in joined


# ---------------------------------------------------------------------------
# async context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_context_manager_closes_cleanly() -> None:
    async with _tracker() as t:
        assert isinstance(t, GitHubTracker)
