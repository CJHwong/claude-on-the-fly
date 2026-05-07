"""Workspace key sanitization, scratch-dir lifecycle. No git involvement."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_on_the_fly.symphony.workspace import (
    WORKSPACES_ROOT,
    ensure_workspace,
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
    assert path == root / "PROJ-1"
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
    assert path.is_dir()


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
