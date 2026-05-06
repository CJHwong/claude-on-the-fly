"""Workspace key sanitization, scratch-dir lifecycle. No git involvement."""

from __future__ import annotations


from claude_on_the_fly.symphony.tracker.issue import Issue
from claude_on_the_fly.symphony.workspace import (
    Workspace,
    ensure_workspace,
    remove_workspace,
    sanitize_key,
)


def test_sanitize_safe_chars():
    assert sanitize_key("PROJ-1133") == "PROJ-1133"
    assert sanitize_key("PROJ_v1.2") == "PROJ_v1.2"


def test_sanitize_replaces_unsafe():
    assert sanitize_key("weird key") == "weird_key"
    assert sanitize_key("a/b\\c") == "a_b_c"
    assert sanitize_key("../escape") == ".._escape"


def _issue(identifier: str = "PROJ-1") -> Issue:
    return Issue(
        id="1",
        identifier=identifier,
        title="t",
        state="To Do",
        description_raw=None,
        priority=None,
        labels=(),
        blocked_by=(),
        parent_key=None,
        url="",
        created_at=None,
        updated_at=None,
    )


def test_ensure_workspace_creates_dir(tmp_path):
    root = tmp_path / "wt"
    ws = ensure_workspace(_issue("PROJ-1"), root)
    assert ws.path == (root / "PROJ-1").resolve()
    assert ws.path.is_dir()
    assert ws.created_now is True


def test_ensure_workspace_reuses_existing(tmp_path):
    root = tmp_path / "wt"
    ws1 = ensure_workspace(_issue("PROJ-1"), root)
    ws2 = ensure_workspace(_issue("PROJ-1"), root)
    assert ws2.created_now is False
    assert ws1.path == ws2.path


def test_ensure_workspace_sanitizes_key(tmp_path):
    root = tmp_path / "wt"
    ws = ensure_workspace(_issue("weird/key"), root)
    assert ws.path.name == "weird_key"
    assert ws.path.is_dir()


def test_remove_workspace_deletes_tree(tmp_path):
    root = tmp_path / "wt"
    ws = ensure_workspace(_issue("PROJ-1"), root)
    (ws.path / "junk").mkdir()
    (ws.path / "junk" / "file.txt").write_text("data")
    remove_workspace(ws)
    assert not ws.path.exists()


def test_remove_workspace_idempotent_when_missing(tmp_path):
    ws = Workspace(path=tmp_path / "does-not-exist", created_now=False)
    remove_workspace(ws)  # must not raise
