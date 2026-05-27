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
    )
    assert out == "[]"


def test_render_prompt_unknown_var_raises():
    with pytest.raises(UndefinedError):
        render_prompt(
            "{{ does_not_exist }}",
            issue=_issue(),
            attempt=0,
            workspace_path=Path("/tmp/ws"),
        )


def test_render_prompt_default_when_template_empty():
    out = render_prompt(
        "",
        issue=_issue(),
        attempt=0,
        workspace_path=Path("/tmp/ws"),
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


# ---------------------------------------------------------------------------
# instruction_path / list_instructions — per-tracker pick from local + remote
# ---------------------------------------------------------------------------


class TestInstructionResolution:
    def test_resolves_local_file(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import instruction_path

        local = tmp_path / "local"
        (local / "github").mkdir(parents=True)
        (local / "github" / "_default.md").write_text("hi")
        path = instruction_path(
            source="github",
            instruction="_default",
            local_root=local,
            remote_root=None,
        )
        assert path == local / "github" / "_default.md"

    def test_local_wins_over_remote(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import instruction_path

        local = tmp_path / "local"
        remote = tmp_path / "remote"
        (local / "github").mkdir(parents=True)
        (remote / "github").mkdir(parents=True)
        (local / "github" / "pm.md").write_text("local pm")
        (remote / "github" / "pm.md").write_text("remote pm")
        path = instruction_path(
            source="github", instruction="pm", local_root=local, remote_root=remote
        )
        assert path == local / "github" / "pm.md"

    def test_falls_to_remote_when_not_local(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import instruction_path

        local = tmp_path / "local"
        remote = tmp_path / "remote"
        (remote / "github").mkdir(parents=True)
        (remote / "github" / "qa.md").write_text("remote qa")
        path = instruction_path(
            source="github", instruction="qa", local_root=local, remote_root=remote
        )
        assert path == remote / "github" / "qa.md"

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import instruction_path

        path = instruction_path(
            source="github",
            instruction="nope",
            local_root=tmp_path,
            remote_root=None,
        )
        assert path is None

    def test_list_unions_local_and_remote(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import list_instructions

        local = tmp_path / "local"
        remote = tmp_path / "remote"
        (local / "github").mkdir(parents=True)
        (remote / "github").mkdir(parents=True)
        (local / "github" / "_default.md").write_text("x")
        (local / "github" / "rnd.md").write_text("x")
        (remote / "github" / "pm.md").write_text("x")
        (remote / "github" / "qa.md").write_text("x")
        names = list_instructions(source="github", local_root=local, remote_root=remote)
        assert names == ["_default", "pm", "qa", "rnd"]

    def test_list_always_includes_default(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import list_instructions

        names = list_instructions(
            source="github", local_root=tmp_path, remote_root=None
        )
        assert names == ["_default"]


class TestInstructionResolverPerRepo:
    def _seed(self, tmp_path: Path, kind: str, stem: str, body: str) -> None:
        d = tmp_path / "local" / kind
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.md").write_text(body)

    def test_default_when_no_override(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import InstructionResolver

        self._seed(tmp_path, "github", "_default", "default body")
        r = InstructionResolver(
            kind="github",
            default_instruction="_default",
            instruction_by_repo={},
            local_root=tmp_path / "local",
            remote_root=None,
        )
        assert r.resolve_for("hardcoretech/fms#42") == "default body"

    def test_per_repo_override_wins(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import InstructionResolver

        self._seed(tmp_path, "github", "_default", "default body")
        self._seed(tmp_path, "github", "fms-review", "fms body")
        r = InstructionResolver(
            kind="github",
            default_instruction="_default",
            instruction_by_repo={"hardcoretech/fms": "fms-review"},
            local_root=tmp_path / "local",
            remote_root=None,
        )
        # Mapped repo → override; unmapped repo → default.
        assert r.resolve_for("hardcoretech/fms#42") == "fms body"
        assert r.resolve_for("hardcoretech/svc-rocket#7") == "default body"

    def test_non_github_identifier_uses_default(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import InstructionResolver

        self._seed(tmp_path, "jira", "_default", "jira body")
        r = InstructionResolver(
            kind="jira",
            default_instruction="_default",
            instruction_by_repo={},  # jira never has a per-repo map
            local_root=tmp_path / "local",
            remote_root=None,
        )
        assert r.resolve_for("FIS-123") == "jira body"

    def test_missing_file_falls_back_to_builtin(self, tmp_path: Path) -> None:
        from claude_on_the_fly.symphony.prompt import InstructionResolver

        r = InstructionResolver(
            kind="github",
            default_instruction="_default",
            instruction_by_repo={},
            local_root=tmp_path / "local",  # nothing seeded
            remote_root=None,
        )
        assert r.resolve_for("owner/repo#1") == ""


def test_github_config_parses_instruction_by_repo() -> None:
    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    cfg = GitHubTrackerConfig.from_dict(
        {
            "kind": "github",
            "instruction": "_default",
            "instruction_by_repo": {"hardcoretech/fms": "fms-review"},
        }
    )
    assert cfg.instruction_by_repo == {"hardcoretech/fms": "fms-review"}


def test_github_config_rejects_non_mapping_instruction_by_repo() -> None:
    import pytest as _pytest

    from claude_on_the_fly.symphony.config import GitHubTrackerConfig

    with _pytest.raises(ValueError, match="instruction_by_repo must be a mapping"):
        GitHubTrackerConfig.from_dict(
            {"kind": "github", "instruction_by_repo": "not-a-map"}
        )
