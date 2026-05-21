"""Prompt loader, mtime hot reload, Liquid render."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from liquid.exceptions import UndefinedError

from claude_on_the_fly.symphony.prompt import PromptStore, render_prompt
from claude_on_the_fly.symphony.tracker.issue import Issue


def _issue() -> Issue:
    return Issue(
        id="1",
        identifier="PROJ-150",
        title="t",
        state="To Do",
        description_raw=None,
        priority=None,
        labels=("stevedore",),
        blocked_by=(),
        parent_key=None,
        url="https://x/browse/PROJ-150",
        created_at=None,
        updated_at=None,
    )


def test_prompt_store_load(tmp_path):
    p = tmp_path / "prompt.md"
    p.write_text("hello world\n")
    store = PromptStore(p)
    assert store.load() == "hello world"


def test_prompt_store_reload_on_mtime_change(tmp_path):
    p = tmp_path / "prompt.md"
    p.write_text("first")
    store = PromptStore(p)
    first = store.load()
    assert store.maybe_reload() == first

    time.sleep(0.01)
    p.write_text("second")
    os.utime(p, None)
    assert store.maybe_reload() == "second"


def test_prompt_store_no_reload_when_unchanged(tmp_path):
    p = tmp_path / "prompt.md"
    p.write_text("static")
    store = PromptStore(p)
    store.load()
    assert store.maybe_reload() == "static"


def test_prompt_store_keeps_last_good_on_disappear(tmp_path):
    p = tmp_path / "prompt.md"
    p.write_text("hello")
    store = PromptStore(p)
    good = store.load()
    p.unlink()
    assert store.maybe_reload() == good


def test_render_prompt_basic(tmp_path):
    out = render_prompt(
        "Ticket {{ issue.identifier }} attempt {{ attempt }} at {{ workspace_path }}",
        issue=_issue(),
        attempt=2,
        workspace_path=Path("/tmp/ws"),
        gate_label="stevedore",
    )
    assert "PROJ-150" in out
    assert "attempt 2" in out
    assert "/tmp/ws" in out


def test_render_prompt_exposes_body_text_and_source() -> None:
    """GitHub-shaped Issues stash the PR body in `body_text` and `source`
    is the tracker kind. Templates need both — without them, the GitHub
    prompt template crashes with UndefinedError on `{{ issue.body_text }}`."""
    gh_issue = Issue(
        id="pr_node",
        identifier="owner/repo#42",
        title="Fix bug",
        state="open",
        description_raw=None,
        priority=None,
        labels=(),
        blocked_by=(),
        parent_key=None,
        url="https://github.com/owner/repo/pull/42",
        created_at=None,
        updated_at=None,
        source="github",
        body_text="Some PR description text.",
    )
    out = render_prompt(
        "src={{ issue.source }} body={{ issue.body_text }}",
        issue=gh_issue,
        attempt=0,
        workspace_path=Path("/tmp/ws"),
        gate_label=None,
    )
    assert "src=github" in out
    assert "body=Some PR description text." in out


def test_render_prompt_body_text_empty_string_when_none() -> None:
    """Jira Issues leave body_text=None (description lives in description_json).
    Liquid is in StrictUndefined mode, so `body_text` must still be set to
    an empty string in the context — not omitted — to avoid raising."""
    out = render_prompt(
        "[{{ issue.body_text }}]",
        issue=_issue(),  # source=jira (default), body_text=None
        attempt=0,
        workspace_path=Path("/tmp/ws"),
        gate_label=None,
    )
    assert out == "[]"


def test_render_prompt_unknown_var_raises():
    with pytest.raises(UndefinedError):
        render_prompt(
            "{{ does_not_exist }}",
            issue=_issue(),
            attempt=0,
            workspace_path=Path("/tmp/ws"),
            gate_label=None,
        )


def test_render_prompt_default_when_template_empty():
    out = render_prompt(
        "",
        issue=_issue(),
        attempt=0,
        workspace_path=Path("/tmp/ws"),
        gate_label=None,
    )
    assert "PROJ-150" in out
    assert "/tmp/ws" in out


def test_prompt_store_path_property(tmp_path: Path) -> None:
    p = tmp_path / "prompt.md"
    p.write_text("hello")
    store = PromptStore(p)
    assert store.path == p


def test_prompt_store_load_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "nonexistent.md"
    store = PromptStore(p)
    with pytest.raises(FileNotFoundError):
        store.load()


def test_prompt_store_load_stat_fails_after_read(tmp_path: Path) -> None:
    """stat() raises FileNotFoundError after read_text() succeeds (race)."""
    from unittest.mock import MagicMock

    p = tmp_path / "prompt.md"
    p.write_text("hello")
    store = PromptStore(p)
    # Mock stat() to fail even though file exists
    mock_path = MagicMock()
    mock_path.read_text.return_value = "hello"
    mock_path.stat.side_effect = FileNotFoundError("gone")
    store._path = mock_path

    result = store.load()
    assert result == "hello"
    assert store._mtime is None


def test_prompt_store_maybe_reload_without_prior_load(tmp_path: Path) -> None:
    p = tmp_path / "prompt.md"
    p.write_text("content")
    store = PromptStore(p)
    # maybe_reload() without load() calls load() first
    result = store.maybe_reload()
    assert result == "content"


def test_prompt_store_maybe_reload_read_failure(tmp_path: Path) -> None:
    """read_text() raises after mtime change: should keep last good source."""
    from unittest.mock import MagicMock

    p = tmp_path / "prompt.md"
    p.write_text("initial")
    store = PromptStore(p)
    store.load()

    # Mock stat() to return a new mtime, then read_text() fails
    mock_path = MagicMock()
    mock_path.stat.return_value.st_mtime = 99999.0  # different mtime
    mock_path.read_text.side_effect = PermissionError("denied")
    store._path = mock_path

    result = store.maybe_reload()
    assert result == "initial"
