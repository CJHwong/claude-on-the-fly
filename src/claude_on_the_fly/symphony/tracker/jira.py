"""Jira Cloud REST adapter. Async, no SDK, Basic auth."""

from __future__ import annotations

import base64
import logging
import os

import httpx

from ..config import TrackerConfig
from .issue import Issue

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = (
    "summary",
    "status",
    "priority",
    "labels",
    "issuelinks",
    "parent",
    "created",
    "updated",
    "description",
)


def compose_jql(cfg: TrackerConfig) -> str:
    """Compose the JQL used to fetch dispatch candidates.

    Wraps active_states in quotes so multi-word names like "To Do" are valid.
    """
    states = ", ".join(f'"{s}"' for s in cfg.active_states)
    base = f'project = "{cfg.project_key}" AND status in ({states})'
    if cfg.jql_extra:
        base = f"{base} {cfg.jql_extra}"
    return base


class JiraTracker:
    def __init__(
        self, base_url: str, email: str, api_token: str, *, timeout: float = 15.0
    ) -> None:
        if not base_url:
            raise ValueError("base_url required")
        if not email or not api_token:
            raise ValueError("email and api_token required")
        self._base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    @classmethod
    def from_env(cls) -> JiraTracker:
        try:
            base_url = os.environ["JIRA_BASE_URL"]
            email = os.environ["JIRA_EMAIL"]
            token = os.environ["JIRA_API_TOKEN"]
        except KeyError as exc:
            raise RuntimeError(
                f"Missing env var: {exc.args[0]}. Set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN."
            ) from None
        return cls(base_url, email, token)

    async def fetch_one(self, key: str) -> Issue:
        """Fetch a single ticket by key, full fields."""
        logger.debug("fetch_one: %s", key)
        resp = await self._client.get(
            f"/rest/api/3/issue/{key}",
            params={"fields": ",".join(_REQUIRED_FIELDS)},
        )
        if resp.status_code == 404:
            raise RuntimeError(f"Jira issue {key} not found (or no access)")
        resp.raise_for_status()
        return Issue.from_jira(resp.json(), self._base_url)

    async def fetch_candidates(self, cfg: TrackerConfig) -> list[Issue]:
        """Search for dispatch candidates per the configured JQL."""
        jql = compose_jql(cfg)
        logger.debug("fetch_candidates: jql=%s", jql)
        body = {
            "jql": jql,
            "fields": list(_REQUIRED_FIELDS),
            "maxResults": 100,
        }
        resp = await self._client.post("/rest/api/3/search/jql", json=body)
        resp.raise_for_status()
        payload = resp.json() or {}
        return [
            Issue.from_jira(item, self._base_url)
            for item in payload.get("issues") or []
        ]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> JiraTracker:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
