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
        exit_label="stevedore",
    )
    assert "PROJ-150" in out
    assert "attempt 2" in out
    assert "/tmp/ws" in out


def test_render_prompt_unknown_var_raises():
    with pytest.raises(UndefinedError):
        render_prompt(
            "{{ does_not_exist }}",
            issue=_issue(),
            attempt=0,
            workspace_path=Path("/tmp/ws"),
            exit_label=None,
        )


def test_render_prompt_default_when_template_empty():
    out = render_prompt(
        "",
        issue=_issue(),
        attempt=0,
        workspace_path=Path("/tmp/ws"),
        exit_label=None,
    )
    assert "PROJ-150" in out
    assert "/tmp/ws" in out
