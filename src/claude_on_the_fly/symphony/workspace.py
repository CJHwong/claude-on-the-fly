"""Per-ticket scratch directory lifecycle.

The daemon creates an empty per-ticket dir under the standard data root and
symlinks the global CLAUDE.md persona into it. The agent is expected to use
existing source-repo clones declared in the prompt — git worktrees live next
to those clones, NOT inside the daemon's scratch dir.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from claude_on_the_fly import agent

logger = logging.getLogger(__name__)

WORKSPACES_ROOT = agent.DATA_DIR / "workspaces" / "symphony"
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_key(key: str) -> str:
    return _SANITIZE_RE.sub("_", key)


def ensure_workspace(identifier: str, root: Path = WORKSPACES_ROOT) -> Path:
    """Idempotent: mkdir the per-ticket dir, symlink in the persona, return the path."""
    path = root / sanitize_key(identifier)
    path.mkdir(parents=True, exist_ok=True)
    agent.ensure_persona(path)
    logger.info("ensure_workspace: %s", path)
    return path


def remove_workspace(path: Path) -> None:
    if not path.exists():
        return
    logger.info("remove_workspace: %s", path)
    shutil.rmtree(path, ignore_errors=True)
