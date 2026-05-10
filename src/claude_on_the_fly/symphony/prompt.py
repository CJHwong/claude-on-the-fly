"""Pure-markdown prompt loader, Liquid renderer, mtime-based hot reload."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from liquid import Environment, StrictUndefined

logger = logging.getLogger(__name__)

_LIQUID_ENV = Environment(undefined=StrictUndefined)
_TEMPLATE_CACHE: dict[str, object] = {}


def _compile(source: str):
    """Compile-once cache: same prompt source maps to the same Liquid template object.
    PromptStore reads new source on mtime change, so the cache key naturally invalidates."""
    cached = _TEMPLATE_CACHE.get(source)
    if cached is None:
        cached = _LIQUID_ENV.from_string(source)
        _TEMPLATE_CACHE[source] = cached
    return cached


class PromptStore:
    """Loads the prompt template from disk and reparses on mtime change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime: float | None = None
        self._source: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> str:
        text = self._path.read_text().strip()
        try:
            self._mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._mtime = None
        self._source = text
        return text

    def maybe_reload(self) -> str:
        """Reparse on mtime change. Falls back to last-known-good if read fails."""
        if self._source is None:
            return self.load()
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            logger.warning("prompt file disappeared: %s", self._path)
            return self._source
        if mtime == self._mtime:
            return self._source
        try:
            text = self._path.read_text().strip()
        except Exception as exc:
            logger.error("prompt reload failed (keeping last good): %s", exc)
            return self._source
        self._mtime = mtime
        self._source = text
        logger.info("prompt reloaded from %s", self._path)
        return text


def render_prompt(
    template_source: str,
    *,
    issue,
    attempt: int,
    workspace_path: Path,
    gate_label: str | None,
) -> str:
    """Render the Liquid prompt body. Unknown variables raise per SPEC §5.4."""
    if not template_source:
        return (
            f"You are working on Jira ticket {issue.identifier}: {issue.title}\n"
            f"Status: {issue.state}\nURL: {issue.url}\n"
            f"Workspace: {workspace_path}\n"
        )
    template = _compile(template_source)
    return template.render(
        issue=_issue_context(issue),
        attempt=attempt,
        workspace_path=str(workspace_path),
        gate_label=gate_label or "",
    )


def _issue_context(issue) -> dict:
    return {
        "id": issue.id,
        "identifier": issue.identifier,
        "title": issue.title,
        "type": issue.type,
        "state": issue.state,
        "priority": issue.priority,
        "labels": list(issue.labels),
        "url": issue.url,
        "parent_key": issue.parent_key or "",
        "description_json": (
            json.dumps(issue.description_raw)
            if issue.description_raw is not None
            else ""
        ),
    }
