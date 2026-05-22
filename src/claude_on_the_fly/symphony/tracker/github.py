"""GitHub PR adapter — shells out to `gh` CLI.

Identifier shape is `owner/repo#<number>`. Candidate selection runs in two
stages:

1. **Server-side search** for PRs directly requesting the user's review:
       is:pr is:open -is:draft user-review-requested:@me
   (Team-level review requests are skipped — `user-review-requested:` is the
   strict "you, personally" filter. `review-requested:` matches both user
   AND team requests.)

2. **Client-side SHA dedup**: drop PRs where the user has already reviewed
   the current head SHA. Server-side `-reviewed-by:@me` is too coarse —
   it filters out PRs we'd want to re-review after the author pushed new
   commits. We use one batched GraphQL search that returns each PR's
   `headRefOid` + the user's latest review commit; filter locally.

Triggers re-review when:
- Never reviewed → trigger
- Reviewed at SHA X, head is now Y ≠ X → trigger (new commits to review)
- Reviewed at SHA X, head is still X → SKIP (don't re-review identical code)

This is stateless — no local cache. Every tick re-queries fresh state.
The agent's "I'm done" signal is submitting any review on the current head
(approve / request-changes / comment-with-substance); the next tick's
SHA-match filter drops the PR until the author pushes again.

`gh` must be on PATH and authenticated (`gh auth status`). We validate `gh`
exists at construction time so misconfigured installs fail fast.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from datetime import datetime
from typing import Any

from ..config import TrackerCommonConfig
from .issue import Issue, IssueSummary

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^([^/]+)/([^#]+)#(\d+)$")


def parse_identifier(identifier: str) -> tuple[str, str, int]:
    """`owner/repo#123` → ('owner', 'repo', 123). Raises on malformed input."""
    m = _IDENT_RE.match(identifier)
    if not m:
        raise ValueError(
            f"invalid GitHub PR identifier {identifier!r}; expected 'owner/repo#<number>'"
        )
    return m.group(1), m.group(2), int(m.group(3))


def _normalize_state(state: str) -> str:
    """gh returns uppercase ("OPEN", "MERGED", "CLOSED"). Normalize to lower."""
    return (state or "").lower()


class GhCliError(RuntimeError):
    """Raised when a `gh` subprocess exits non-zero. Carries stderr for logs."""

    def __init__(self, args: list[str], returncode: int, stderr: str):
        # BaseException.args is typed as tuple[Any, ...]; copy the list into
        # a tuple so the assignment matches the declared shape.
        self.args = tuple(args)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"gh {' '.join(args)} exited {returncode}: {stderr.strip() or '<no stderr>'}"
        )


class GitHubTracker:
    """Adapter for GitHub pull-request review work."""

    def __init__(self, *, timeout_s: float = 30.0) -> None:
        if shutil.which("gh") is None:
            raise RuntimeError(
                "`gh` CLI not found on PATH. Install GitHub CLI and run `gh auth login`."
            )
        self._timeout_s = timeout_s
        self._login: str | None = None  # lazily resolved by _get_login()
        self._login_lock = asyncio.Lock()

    @classmethod
    def from_config(cls, cfg: TrackerCommonConfig) -> GitHubTracker:
        # GitHubTrackerConfig carries no GitHub-specific fields today; gh CLI
        # handles auth via its own state. Common fields like active_states /
        # terminal_states are used at the predicate level, not at construction.
        return cls()

    async def _run_gh(self, args: list[str]) -> bytes:
        """Spawn `gh <args>` and return stdout bytes. Raises GhCliError on
        non-zero exit. Exposed at module level so tests can patch it."""
        logger.debug("gh %s", " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise GhCliError(args, -1, "timed out") from None
        if proc.returncode != 0:
            raise GhCliError(
                args, proc.returncode or -1, stderr.decode("utf-8", "replace")
            )
        return stdout

    async def _run_gh_json(self, args: list[str]) -> Any:
        raw = await self._run_gh(args)
        if not raw.strip():
            return None
        return json.loads(raw)

    async def _get_login(self) -> str:
        """Resolve and cache the authenticated GitHub login. Used to detect
        whether the current user is in a PR's `reviewRequests`."""
        if self._login is not None:
            return self._login
        async with self._login_lock:
            if self._login is None:
                stdout = await self._run_gh(["api", "user", "--jq", ".login"])
                self._login = stdout.decode("utf-8").strip()
                logger.info("GitHubTracker: authenticated as %s", self._login)
        return self._login

    @staticmethod
    def _identifier_from_payload(payload: dict) -> str:
        """`{repository: {nameWithOwner: "o/r"}, number: 7}` → 'o/r#7'."""
        repo = (payload.get("repository") or {}).get("nameWithOwner") or ""
        number = payload.get("number")
        return f"{repo}#{number}" if repo and number is not None else ""

    @staticmethod
    def _labels_tuple(payload: dict) -> tuple[str, ...]:
        return tuple(
            str(lbl.get("name", "")).lower()
            for lbl in (payload.get("labels") or [])
            if isinstance(lbl, dict) and lbl.get("name")
        )

    def _payload_to_issue(
        self,
        payload: dict,
        *,
        user_reviewed_current_head: bool | None = None,
    ) -> Issue:
        identifier = self._identifier_from_payload(payload)
        state = _normalize_state(payload.get("state") or "")
        labels = self._labels_tuple(payload)
        url = payload.get("url") or ""
        body = payload.get("body") or ""
        extra: dict[str, Any] = {}
        if user_reviewed_current_head is not None:
            extra["user_reviewed_current_head"] = user_reviewed_current_head
        head_oid = payload.get("headRefOid")
        if head_oid:
            extra["head_ref_oid"] = head_oid
        return Issue(
            id=str(payload.get("id") or ""),
            identifier=identifier,
            title=str(payload.get("title") or ""),
            state=state,
            description_raw=None,
            priority=None,  # GitHub PRs have no native priority
            labels=labels,
            blocked_by=(),
            parent_key=None,
            url=url,
            created_at=payload.get("createdAt"),
            updated_at=payload.get("updatedAt"),
            type="PullRequest",
            source="github",
            body_text=body,
            extra=extra,
        )

    # GraphQL query for fetch_candidates: one round-trip pulls every PR
    # requesting our review along with its head SHA and our latest review
    # (if any), so we can filter SHA-stale reviews client-side without N+1.
    _SEARCH_GQL = """
    query($q: String!, $first: Int!) {
      search(query: $q, type: ISSUE, first: $first) {
        nodes {
          ... on PullRequest {
            id
            number
            title
            body
            url
            state
            createdAt
            updatedAt
            headRefOid
            repository { nameWithOwner }
            labels(first: 20) { nodes { name } }
            latestReviews(first: 20) {
              nodes {
                author { login }
                commit { oid }
              }
            }
          }
        }
      }
    }
    """

    async def fetch_candidates(self, cfg: TrackerCommonConfig) -> list[Issue]:
        """Open non-draft PRs DIRECTLY requesting the user's review where
        the user hasn't already reviewed the *current head SHA* AND the PR
        is older than `cool_down_ms` (0 = no delay).

        Uses GraphQL search so each PR's `headRefOid` + the user's latest
        review's commit OID come back in one call. We filter SHA-stale
        reviews client-side: drop PRs where the user reviewed at the same
        SHA the PR's head now points at (no new code to review), keep PRs
        where the user reviewed at an older SHA (re-review the new commits),
        and skip PRs younger than the configured cool-down window.

        Stateless — no local cache. Every tick re-queries fresh state.
        """
        login = await self._get_login()
        query = (
            getattr(cfg, "search_query", None)
            or "is:pr is:open -is:draft user-review-requested:@me"
        )
        args = [
            "api",
            "graphql",
            "-f",
            f"query={self._SEARCH_GQL}",
            "-f",
            f"q={query}",
            "-F",
            "first=100",
        ]
        try:
            payload = await self._run_gh_json(args)
        except GhCliError as exc:
            logger.error("github fetch_candidates failed: %s", exc)
            raise
        nodes = (
            ((payload or {}).get("data") or {}).get("search", {}).get("nodes")
        ) or []

        candidates: list[Issue] = []
        skipped_already_reviewed = 0
        skipped_too_fresh = 0
        now_s = time.time()
        for node in nodes:
            if not isinstance(node, dict):
                continue
            head_oid = node.get("headRefOid")
            latest = (node.get("latestReviews") or {}).get("nodes") or []
            user_review_oid = self._user_latest_review_oid(latest, login)
            user_reviewed_head = bool(
                user_review_oid and head_oid and user_review_oid == head_oid
            )
            if user_reviewed_head:
                skipped_already_reviewed += 1
                continue
            cool_down_ms = getattr(cfg, "cool_down_ms", 0) or 0
            if cool_down_ms > 0:
                created_at = node.get("createdAt")
                if created_at:
                    created_dt = datetime.fromisoformat(created_at)
                    age_s = now_s - created_dt.timestamp()
                    if age_s * 1000 < cool_down_ms:
                        skipped_too_fresh += 1
                        continue
            candidates.append(
                self._payload_to_issue(node, user_reviewed_current_head=False)
            )
        if skipped_already_reviewed or skipped_too_fresh:
            logger.info(
                "github fetch_candidates: %d candidate(s), %d skipped (already reviewed at head), %d skipped (cool-down)",
                len(candidates),
                skipped_already_reviewed,
                skipped_too_fresh,
            )
        return candidates

    @staticmethod
    def _user_latest_review_oid(reviews: list, login: str) -> str | None:
        """Pluck the commit.oid from the user's latest review entry, if any."""
        for review in reviews:
            if not isinstance(review, dict):
                continue
            author = review.get("author") or {}
            if author.get("login") != login:
                continue
            commit = review.get("commit") or {}
            return commit.get("oid")
        return None

    async def fetch_one(self, key: str) -> Issue:
        """`owner/repo#N` → fully populated Issue, with
        `user_reviewed_current_head` derived from headRefOid + reviews."""
        owner, repo, number = parse_identifier(key)
        args = [
            "pr",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "number,title,body,labels,state,createdAt,updatedAt,headRefOid,reviews,url,id",
        ]
        payload = await self._run_gh_json(args)
        if not isinstance(payload, dict):
            raise RuntimeError(f"github fetch_one({key}): unexpected payload shape")
        # `gh pr view` doesn't return repository.nameWithOwner, so inject it
        # so _identifier_from_payload can rebuild the canonical identifier.
        payload.setdefault("repository", {"nameWithOwner": f"{owner}/{repo}"})
        login = await self._get_login()
        reviewed = self._user_reviewed_current_head(payload, login)
        return self._payload_to_issue(payload, user_reviewed_current_head=reviewed)

    @staticmethod
    def _user_reviewed_current_head(payload: dict, login: str) -> bool:
        """True iff `login` has a review whose `commit.oid` matches the PR's
        current `headRefOid`.

        Scans `payload["reviews"]` (a list, not a `latestReviews` shape) and
        finds the user's most-recent review by `submittedAt`, then checks
        whether that review's commit OID equals the PR's head SHA. Returns
        False if the user has never reviewed OR if every review they have
        is at an older SHA.
        """
        head_oid = payload.get("headRefOid") or ""
        if not head_oid:
            return False
        latest_oid: str | None = None
        latest_when: str | None = None
        for review in payload.get("reviews") or []:
            if not isinstance(review, dict):
                continue
            author = review.get("author") or {}
            if author.get("login") != login:
                continue
            when = review.get("submittedAt") or ""
            # ISO 8601 strings sort lexicographically by time.
            if latest_when is None or when > latest_when:
                latest_when = when
                latest_oid = (review.get("commit") or {}).get("oid")
        return latest_oid is not None and latest_oid == head_oid

    async def fetch_summaries_by_keys(self, keys: list[str]) -> dict[str, IssueSummary]:
        """Per-key `gh pr view` fetch, run concurrently. The N subprocess
        spawns still happen but they're awaited together — for 5 running
        PRs that's one wall-clock round-trip instead of five sequential."""
        if not keys:
            return {}
        login = await self._get_login()

        # Parse identifiers up front; invalid ones get logged and skipped.
        parsed: list[tuple[str, str, str, int]] = []  # (key, owner, repo, number)
        for key in keys:
            try:
                owner, repo, number = parse_identifier(key)
            except ValueError:
                logger.warning(
                    "github fetch_summaries: skipping invalid identifier %r", key
                )
                continue
            parsed.append((key, owner, repo, number))

        if not parsed:
            return {}

        async def _fetch_one_summary(
            key: str, owner: str, repo: str, number: int
        ) -> tuple[str, IssueSummary] | None:
            args = [
                "pr",
                "view",
                str(number),
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "state,reviews,headRefOid",
            ]
            try:
                payload = await self._run_gh_json(args)
            except GhCliError as exc:
                # PR vanished / private / network blip: drop from summaries so
                # the orchestrator handles "missing" the same way it does for Jira.
                logger.warning("github fetch_summaries[%s] failed: %s", key, exc)
                return None
            if not isinstance(payload, dict):
                return None
            state = _normalize_state(payload.get("state") or "")
            reviewed = self._user_reviewed_current_head(payload, login)
            return key, IssueSummary(
                state=state,
                extra={"user_reviewed_current_head": reviewed},
            )

        results = await asyncio.gather(
            *(_fetch_one_summary(k, o, r, n) for k, o, r, n in parsed),
            return_exceptions=True,
        )
        out: dict[str, IssueSummary] = {}
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("github fetch_summaries: %s", result)
                continue
            if result is None:
                continue
            key, summary = result
            out[key] = summary
        return out

    def is_terminal(self, summary: IssueSummary, cfg: TrackerCommonConfig) -> bool:
        """GitHub PRs are terminal once closed or merged."""
        return summary.state in cfg.terminal_states

    def is_active(self, summary: IssueSummary, cfg: TrackerCommonConfig) -> bool:
        """Keep running while the PR is open AND the user hasn't reviewed
        the *current head SHA*.

        The agent's "done" signal is submitting any review at the current
        head SHA — that flips `user_reviewed_current_head` True on the next
        reconcile and the worker exits. If the author pushes new commits
        after the review, the head SHA changes and the worker (or a fresh
        dispatch) re-engages.
        """
        if summary.state not in cfg.active_states:
            return False
        return not bool(summary.extra.get("user_reviewed_current_head"))

    def issue_to_summary(self, issue: Issue) -> IssueSummary:
        return IssueSummary(
            state=issue.state,
            extra={
                "user_reviewed_current_head": bool(
                    issue.extra.get("user_reviewed_current_head")
                )
            },
        )

    async def aclose(self) -> None:
        # No persistent resources to release; subprocesses are awaited per call.
        return None

    async def __aenter__(self) -> GitHubTracker:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
