"""Per-ticket scratch directory lifecycle.

The daemon creates an empty per-ticket dir under the standard data root and
symlinks the global CLAUDE.md persona into it. The agent is expected to use
existing source-repo clones declared in the prompt — git worktrees live next
to those clones, NOT inside the daemon's scratch dir.

Layout: `WORKSPACES_ROOT / <source> / <sanitized-key>`. The source prefix
prevents collisions between trackers that may mint overlapping keys
(e.g. raw Jira numeric ids vs GitHub PR numeric ids).
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

# Sidecar file inside each workspace recording the original (unsanitized)
# ticket identifier. Needed for startup_cleanup to reverse the sanitization
# (e.g. dir name `hardcoretech_gf-external-api_754` → `hardcoretech/gf-external-api#754`).
_IDENTIFIER_SIDECAR = ".identifier"


def sanitize_key(key: str) -> str:
    return _SANITIZE_RE.sub("_", key)


def ensure_workspace(
    identifier: str,
    *,
    source: str = "jira",
    root: Path = WORKSPACES_ROOT,
) -> Path:
    """Idempotent: mkdir the per-ticket dir, symlink in the persona, return the path.

    Path layout is `root / source / sanitize_key(identifier)`. Default
    source is `jira` for backward compat with code that hasn't been updated
    to pass an explicit source yet.

    Also writes a `.identifier` sidecar containing the original (unsanitized)
    identifier so `startup_cleanup` can reverse the sanitization for sources
    where it's lossy (e.g. github PRs sanitize `/` and `#` both to `_`).
    """
    path = root / source / sanitize_key(identifier)
    path.mkdir(parents=True, exist_ok=True)
    sidecar = path / _IDENTIFIER_SIDECAR
    try:
        if not sidecar.is_file() or sidecar.read_text().strip() != identifier:
            sidecar.write_text(identifier)
    except OSError as exc:
        logger.warning("ensure_workspace: identifier sidecar write failed: %s", exc)
    agent.ensure_persona(path)
    logger.info("ensure_workspace: %s", path)
    return path


def read_workspace_identifier(path: Path) -> str | None:
    """Read the original identifier stashed by `ensure_workspace` (or None
    if the sidecar is missing — happens for older dirs created before this
    sidecar landed)."""
    sidecar = path / _IDENTIFIER_SIDECAR
    if not sidecar.is_file():
        return None
    try:
        text = sidecar.read_text().strip()
    except OSError:
        return None
    return text or None


def remove_workspace(path: Path) -> None:
    if not path.exists():
        return
    logger.info("remove_workspace: %s", path)
    shutil.rmtree(path, ignore_errors=True)
