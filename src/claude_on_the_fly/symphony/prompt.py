"""Pure-markdown prompt loader, Liquid renderer, mtime-based hot reload."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from liquid import Environment, StrictUndefined

logger = logging.getLogger(__name__)

_LIQUID_ENV = Environment(undefined=StrictUndefined)
_TEMPLATE_CACHE: dict[str, object] = {}
# Each prompt edit produces a new source string (new cache key); without a
# bound the cache grows for the daemon's lifetime. Cap it and evict oldest.
_TEMPLATE_CACHE_MAX = 64


def _compile(source: str):
    """Compile-once cache: same prompt source maps to the same Liquid template object.
    PromptStore reads new source on mtime change, so the cache key naturally invalidates."""
    cached = _TEMPLATE_CACHE.get(source)
    if cached is None:
        cached = _LIQUID_ENV.from_string(source)
        if len(_TEMPLATE_CACHE) >= _TEMPLATE_CACHE_MAX:
            # FIFO eviction: drop the oldest-inserted entry (dicts preserve
            # insertion order). Stale prompt versions are never reused anyway.
            _TEMPLATE_CACHE.pop(next(iter(_TEMPLATE_CACHE)))
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


def instruction_path(
    *,
    source: str,
    instruction: str,
    local_root: Path,
) -> Path | None:
    """Resolve `<instruction>.md` for a tracker.

    Looks at:
        local_root/<source>/<instruction>.md         (e.g. ~/.claude-on-the-fly/symphony/github/pm.md)

    Returns the path if it exists, or None (caller falls back to the
    built-in prompt).
    """
    name = (instruction or "_default").strip()
    candidate = local_root / source / f"{name}.md"
    return candidate if candidate.is_file() else None


def list_instructions(*, source: str, local_root: Path) -> list[str]:
    """Discover available instruction stems for a tracker.

    Returns sorted unique filename stems (without `.md`). `_default` is always
    included so the dropdown never renders empty even before any file exists.
    """
    names: set[str] = {"_default"}
    directory = local_root / source
    if directory.is_dir():
        for path in directory.glob("*.md"):
            names.add(path.stem)
    return sorted(names)


def _github_repo(identifier: str) -> str | None:
    """`owner/repo#42` → `owner/repo`; other shapes → None."""
    if "/" not in identifier or "#" not in identifier:
        return None
    head, _, _ = identifier.partition("#")
    owner, _, repo = head.partition("/")
    if not owner.strip() or not repo.strip():
        return None
    return f"{owner.strip()}/{repo.strip()}"


class InstructionResolver:
    """Per-tracker prompt resolver. Returns the instruction source string for
    an issue, honoring a per-repo override map on top of the default stem.

    For most trackers `instruction_by_repo` is empty → every issue uses the
    default stem. GitHub trackers can map `owner/repo` → a different stem.
    Resolved files are cached as PromptStores (one per stem) and hot-reload
    on mtime per call.
    """

    def __init__(
        self,
        *,
        kind: str,
        default_instruction: str,
        instruction_by_repo: dict[str, str] | None,
        local_root: Path,
    ) -> None:
        self._kind = kind
        self._default = default_instruction or "_default"
        self._by_repo = dict(instruction_by_repo or {})
        self._local = local_root
        self._stores: dict[str, PromptStore | None] = {}

    def _stem_for(self, identifier: str) -> str:
        if self._by_repo:
            repo = _github_repo(identifier)
            if repo and repo in self._by_repo:
                return self._by_repo[repo]
        return self._default

    def _store_for_stem(self, stem: str) -> PromptStore | None:
        if stem not in self._stores:
            path = instruction_path(
                source=self._kind,
                instruction=stem,
                local_root=self._local,
            )
            if path is None:
                # A missing `_default` is expected (rely on the built-in); a
                # missing NON-default stem is almost always a config typo in
                # `instruction:` / `instruction_by_repo:`, so surface it louder.
                log = logger.warning if stem == "_default" else logger.error
                log(
                    "[%s] instruction '%s' not found under %s; "
                    "using built-in fallback prompt",
                    self._kind,
                    stem,
                    self._local / self._kind,
                )
                self._stores[stem] = None
            else:
                store = PromptStore(path)
                store.load()
                self._stores[stem] = store
        return self._stores[stem]

    def resolve_for(self, identifier: str) -> str:
        store = self._store_for_stem(self._stem_for(identifier))
        return store.maybe_reload() if store is not None else ""


def render_prompt(
    template_source: str,
    *,
    issue,
    attempt: int,
    workspace_path: Path,
) -> str:
    """Render the Liquid prompt body. Unknown variables raise per SPEC §5.4."""
    if not template_source:
        # Source-neutral fallback (works for Jira keys and GitHub owner/repo#N).
        return (
            f"You are working on {issue.identifier}: {issue.title}\n"
            f"Status: {issue.state}\nURL: {issue.url}\n"
            f"Workspace: {workspace_path}\n"
        )
    template = _compile(template_source)
    return template.render(
        issue=_issue_context(issue),
        attempt=attempt,
        workspace_path=str(workspace_path),
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
        # Multi-tracker fields. `body_text` is GitHub's PR body (Jira leaves
        # this empty; the ADF lives in description_json). `source` lets a
        # single shared template branch on tracker kind if needed.
        "body_text": issue.body_text or "",
        "source": issue.source,
    }
