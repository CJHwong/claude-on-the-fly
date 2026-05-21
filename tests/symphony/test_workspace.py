"""Workspace key sanitization, scratch-dir lifecycle. No git involvement."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_on_the_fly.symphony.workspace import (
    WORKSPACES_ROOT,
    ensure_workspace,
    read_workspace_identifier,
    remove_workspace,
    sanitize_key,
)


@pytest.fixture(autouse=True)
def _stub_persona():
    """ensure_workspace calls agent.ensure_persona; stub it so tests don't touch ~/.claude-on-the-fly/CLAUDE.md."""
    with patch("claude_on_the_fly.symphony.workspace.agent.ensure_persona") as mock:
        yield mock


def test_sanitize_safe_chars():
    assert sanitize_key("PROJ-1133") == "PROJ-1133"
    assert sanitize_key("PROJ_v1.2") == "PROJ_v1.2"


def test_sanitize_replaces_unsafe():
    assert sanitize_key("weird key") == "weird_key"
    assert sanitize_key("a/b\\c") == "a_b_c"
    assert sanitize_key("../escape") == ".._escape"


def test_ensure_workspace_creates_dir(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    path = ensure_workspace("PROJ-1", root=root)
    assert path == root / "jira" / "PROJ-1"
    assert path.is_dir()


def test_ensure_workspace_reuses_existing(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    a = ensure_workspace("PROJ-1", root=root)
    b = ensure_workspace("PROJ-1", root=root)
    assert a == b
    assert a.is_dir()


def test_ensure_workspace_sanitizes_key(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    path = ensure_workspace("weird/key", root=root)
    assert path.name == "weird_key"
    assert path.parent.name == "jira"
    assert path.is_dir()


def test_ensure_workspace_routes_by_source(tmp_path: Path) -> None:
    """GitHub PRs land under a different subdir so identifiers can't collide
    with Jira keys on disk."""
    root = tmp_path / "wt"
    jira_path = ensure_workspace("PROJ-1", source="jira", root=root)
    gh_path = ensure_workspace("owner/repo#1", source="github", root=root)
    assert jira_path == root / "jira" / "PROJ-1"
    assert gh_path == root / "github" / "owner_repo_1"
    assert jira_path != gh_path


def test_ensure_workspace_writes_identifier_sidecar(tmp_path: Path) -> None:
    """The sidecar stashes the original (unsanitized) identifier so
    startup_cleanup can reverse the sanitization when the dir name alone
    is lossy (`/` and `#` both become `_`)."""
    root = tmp_path / "wt"
    path = ensure_workspace("owner/repo#42", source="github", root=root)
    sidecar = path / ".identifier"
    assert sidecar.is_file()
    assert sidecar.read_text() == "owner/repo#42"
    # Helper round-trip.
    assert read_workspace_identifier(path) == "owner/repo#42"


def test_read_workspace_identifier_returns_none_when_missing(
    tmp_path: Path,
) -> None:
    """Legacy dirs created before the sidecar landed return None — caller
    falls back to the dir name."""
    legacy = tmp_path / "wt" / "jira" / "PROJ-1"
    legacy.mkdir(parents=True)
    assert read_workspace_identifier(legacy) is None


def test_ensure_workspace_sidecar_idempotent(
    tmp_path: Path,
) -> None:
    """Re-calling ensure_workspace on the same identifier doesn't rewrite
    the sidecar unnecessarily (preserves mtime so file watchers don't
    re-fire)."""
    root = tmp_path / "wt"
    path = ensure_workspace("owner/repo#1", source="github", root=root)
    sidecar = path / ".identifier"
    first_mtime = sidecar.stat().st_mtime_ns
    # Second call with the same identifier — sidecar should be untouched.
    ensure_workspace("owner/repo#1", source="github", root=root)
    assert sidecar.stat().st_mtime_ns == first_mtime


def test_ensure_workspace_calls_ensure_persona(tmp_path: Path, _stub_persona) -> None:
    root = tmp_path / "wt"
    path = ensure_workspace("PROJ-1", root=root)
    _stub_persona.assert_called_once_with(path)


def test_remove_workspace_deletes_tree(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    path = ensure_workspace("PROJ-1", root=root)
    (path / "junk").mkdir()
    (path / "junk" / "file.txt").write_text("data")
    remove_workspace(path)
    assert not path.exists()


def test_remove_workspace_idempotent_when_missing(tmp_path: Path) -> None:
    remove_workspace(tmp_path / "does-not-exist")  # must not raise


def test_default_root_is_under_data_dir() -> None:
    """WORKSPACES_ROOT lives under the standard data root, not ~/code."""
    assert WORKSPACES_ROOT.parts[-3:] == (
        ".claude-on-the-fly",
        "workspaces",
        "symphony",
    )
