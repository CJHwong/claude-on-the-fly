"""Jira Cloud adapter via the `acli` CLI (no direct REST, no email/token).

Auth lives in `acli auth login`. Symphony shells out to `acli jira workitem ...`
for every read. Writes (comments, transitions) remain agent-side and are not
this module's concern.

acli search has a hard-coded allowlist of fields. The forbidden fields include
`issuelinks`, `parent`, `created`, and `updated` — but those are needed for
`Issue.from_jira`. The candidate fetch therefore does it in two passes:
search → keys, then parallel `view` calls per key for the full payload.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from ..config import JiraTrackerConfig, TrackerCommonConfig
from .issue import Issue, IssueSummary

logger = logging.getLogger(__name__)

# Fields requested from `acli workitem view`. `view` accepts the full Jira
# field set; only `search` has the restricted allowlist.
_VIEW_FIELDS = (
    "summary",
    "status",
    "priority",
    "labels",
    "issuelinks",
    "parent",
    "created",
    "updated",
    "description",
    "issuetype",
)

# Fields requested from `acli workitem search`. acli rejects issuelinks,
# parent, created, updated for search — so we only ask for what acli allows
# and upgrade to a `view` call when we need the full payload.
_SEARCH_FIELDS_KEYS_ONLY = ("key", "status")
_SEARCH_FIELDS_SUMMARY = ("key", "status", "labels")


class JiraAcliError(RuntimeError):
    """acli exited non-zero. stderr captured in the message."""


class IssueNotFoundError(JiraAcliError):
    """acli reported the issue does not exist or is not visible."""


def compose_jql(cfg: TrackerCommonConfig) -> str:
    """Compose the candidate JQL: `project = "<key>" AND (<jql>)`.

    `jql` is the full filter clause (no leading `AND`). When empty, the
    query is just `project = "<key>"` (every ticket in the project — usually
    too broad, but the operator's call).
    """
    if not isinstance(cfg, JiraTrackerConfig):
        raise TypeError(f"compose_jql expects JiraTrackerConfig, got {type(cfg)}")
    base = f'project = "{cfg.project_key}"'
    if cfg.jql:
        base = f"{base} AND ({cfg.jql})"
    return base


class JiraTracker:
    @classmethod
    def from_config(cls, cfg: TrackerCommonConfig) -> JiraTracker:
        if not isinstance(cfg, JiraTrackerConfig):
            raise TypeError(f"JiraTracker expects JiraTrackerConfig, got {type(cfg)}")
        return cls(base_url=cfg.base_url)

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        if not base_url:
            raise ValueError("base_url required")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def _run_acli(self, args: list[str]) -> str:
        """Run `acli jira <args>`. Returns stdout text on success.

        Raises `IssueNotFoundError` if stderr looks like a 404. Raises
        `JiraAcliError` for any other non-zero exit or timeout.
        """
        if shutil.which("acli") is None:
            raise JiraAcliError(
                "acli is not installed or not on PATH. Install acli and run "
                "`acli auth login`."
            )
        # Log without the full body of large args (descriptions, JQL).
        logger.debug("acli jira %s", " ".join(args[:3]))
        proc = await asyncio.create_subprocess_exec(
            "acli",
            "jira",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise JiraAcliError(
                f"acli jira {args[0] if args else ''} timed out after {self._timeout}s"
            ) from exc

        if proc.returncode != 0:
            err_text = stderr_b.decode("utf-8", errors="replace").strip()
            # acli's not-found error: "Issue does not exist or you do not have permission..."
            if "does not exist" in err_text or "do not have permission" in err_text:
                raise IssueNotFoundError(err_text)
            raise JiraAcliError(
                f"acli exit={proc.returncode}: {err_text or '<no stderr>'}"
            )
        return stdout_b.decode("utf-8", errors="replace")

    async def fetch_one(self, key: str) -> Issue:
        """Fetch a single ticket by key, full fields."""
        logger.debug("fetch_one: %s", key)
        try:
            out = await self._run_acli(
                [
                    "workitem",
                    "view",
                    key,
                    "--fields",
                    ",".join(_VIEW_FIELDS),
                    "--json",
                ]
            )
        except IssueNotFoundError as exc:
            raise RuntimeError(f"Jira issue {key} not found (or no access)") from exc
        payload = json.loads(out) if out.strip() else {}
        if not isinstance(payload, dict):
            raise JiraAcliError(
                f"unexpected acli view payload for {key}: not an object"
            )
        return Issue.from_jira(payload, self._base_url)

    async def fetch_summaries_by_keys(
        self, keys: list[str], cfg: TrackerCommonConfig
    ) -> dict[str, IssueSummary]:
        """Reconciliation snapshot: which of `keys` still match the candidate JQL.

        Runs `key in (keys) AND (<jql>)` and returns a summary for EVERY input
        key (not just matches), with `extra["matches_jql"]` set. Keys that
        still match get their real status; keys that dropped out get a
        placeholder state and `matches_jql=False`. We return all keys — never
        omit — because the orchestrator treats a missing key as "transient
        failure, skip", which would leave a stale worker running forever.
        """
        if not keys:
            return {}
        if not isinstance(cfg, JiraTrackerConfig):
            raise TypeError(
                f"JiraTracker.fetch_summaries_by_keys expects JiraTrackerConfig, "
                f"got {type(cfg)}"
            )
        quoted = ", ".join(f'"{k}"' for k in keys)
        jql = f"key in ({quoted})"
        if cfg.jql:
            jql = f"{jql} AND ({cfg.jql})"
        out = await self._run_acli(
            [
                "workitem",
                "search",
                "--jql",
                jql,
                "--fields",
                ",".join(_SEARCH_FIELDS_SUMMARY),
                "--limit",
                str(max(len(keys), 50)),
                "--json",
            ]
        )
        items = self._parse_search_items(out)
        matched: dict[str, str] = {}
        for item in items:
            key = item.get("key")
            status_name = (item.get("fields") or {}).get("status", {}).get("name")
            if key and status_name:
                matched[str(key)] = str(status_name)
        result: dict[str, IssueSummary] = {}
        for key in keys:
            if key in matched:
                result[key] = IssueSummary(
                    state=matched[key], extra={"matches_jql": True}
                )
            else:
                result[key] = IssueSummary(
                    state="(left queue)", extra={"matches_jql": False}
                )
        return result

    async def fetch_candidates(self, cfg: TrackerCommonConfig) -> list[Issue]:
        """Search for dispatch candidates per the configured JQL.

        Two passes: search returns keys (acli's search field allowlist excludes
        issuelinks/parent/created/updated), then parallel view calls for the
        full payload.
        """
        jql = compose_jql(cfg)
        logger.debug("fetch_candidates: jql=%s", jql)
        out = await self._run_acli(
            [
                "workitem",
                "search",
                "--jql",
                jql,
                "--fields",
                ",".join(_SEARCH_FIELDS_KEYS_ONLY),
                "--limit",
                "100",
                "--json",
            ]
        )
        items = self._parse_search_items(out)
        keys = [str(item.get("key")) for item in items if item.get("key")]
        if not keys:
            return []
        results = await asyncio.gather(
            *(self.fetch_one(k) for k in keys), return_exceptions=True
        )
        out_issues: list[Issue] = []
        for key, res in zip(keys, results, strict=True):
            if isinstance(res, Issue):
                out_issues.append(res)
            else:
                logger.warning(
                    "fetch_candidates: view %s failed, skipping: %s", key, res
                )
        return out_issues

    @staticmethod
    def _parse_search_items(out: str) -> list[dict[str, Any]]:
        if not out.strip():
            return []
        payload = json.loads(out)
        # acli search returns a top-level JSON array.
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        # Be defensive — if a future acli rev wraps it.
        if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
            return [item for item in payload["issues"] if isinstance(item, dict)]
        raise JiraAcliError("unexpected acli search payload shape")

    def is_terminal(self, summary: IssueSummary, cfg: TrackerCommonConfig) -> bool:
        """Jira: always False. There is no terminal status list anymore — a
        ticket that leaves the candidate JQL is treated as parked (worker
        cancelled, workspace kept) and its scratch dir is GC'd at startup.
        """
        return False

    def is_active(self, summary: IssueSummary, cfg: TrackerCommonConfig) -> bool:
        """Jira: the ticket still matches the candidate JQL.

        `fetch_summaries_by_keys` sets `extra["matches_jql"]` by re-running
        `key in (...) AND (<jql>)`. A ticket that moved to Done, got
        reassigned, or otherwise left the JQL flips this False and the worker
        is cancelled (workspace kept).
        """
        return bool(summary.extra.get("matches_jql", False))

    def issue_to_summary(self, issue: Issue) -> IssueSummary:
        """Project a refreshed Jira Issue into a summary. Used by callers that
        already hold a full Issue. `matches_jql` is unknown from an Issue
        alone (it needs the JQL query), so it's omitted — callers that need
        the active/terminal decision use `fetch_summaries_by_keys` instead.
        """
        return IssueSummary(state=issue.state, extra={"labels": issue.labels})

    async def aclose(self) -> None:
        """No persistent resources to release (subprocess is per-call)."""
        return None

    async def __aenter__(self) -> JiraTracker:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
