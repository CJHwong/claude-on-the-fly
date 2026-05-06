"""Per-ticket scratch directory lifecycle.

The daemon only creates and removes a directory; it does not run git. Any worktrees
inside are the agent's responsibility (creation, branch hygiene, cleanup).

Safety invariants (Symphony SPEC §9.5):
- Sanitized key: only [A-Za-z0-9._-]
- workspace_path stays inside worktree_root after .resolve()
- The agent runs with cwd == workspace_path
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .tracker.issue import Issue

logger = logging.getLogger(__name__)

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class Workspace:
    path: Path
    created_now: bool


def sanitize_key(key: str) -> str:
    return _SANITIZE_RE.sub("_", key)


def _assert_inside(path: Path, root: Path) -> None:
    rp = path.resolve()
    rr = root.resolve()
    try:
        rp.relative_to(rr)
    except ValueError as exc:
        raise RuntimeError(f"workspace path {rp} escapes root {rr}") from exc


def ensure_workspace(issue: Issue, root: Path) -> Workspace:
    key = sanitize_key(issue.identifier)
    path = root / key
    root.mkdir(parents=True, exist_ok=True)
    _assert_inside(path, root)
    created_now = not path.exists()
    if created_now:
        path.mkdir()
        logger.info("ensure_workspace: created %s", path)
    else:
        logger.info("ensure_workspace: reusing %s", path)
    return Workspace(path=path, created_now=created_now)


def remove_workspace(ws: Workspace) -> None:
    """Delete the scratch dir. Worktrees the agent created inside become orphan
    pointers in their source repos until `git worktree prune` runs there."""
    if not ws.path.exists():
        return
    logger.info("remove_workspace: %s", ws.path)
    shutil.rmtree(ws.path, ignore_errors=True)
