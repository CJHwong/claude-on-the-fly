"""Phase 6 — RemoteSource parsing, shallow_merge semantics, and end-to-end
load_remote_config against a temporary on-disk git repo."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from claude_on_the_fly.symphony.remote_config import (
    RemoteConfigError,
    RemoteSource,
    load_remote_config,
    shallow_merge,
)


# ---------------------------------------------------------------------------
# RemoteSource.parse
# ---------------------------------------------------------------------------


class TestRemoteSourceParse:
    def test_git_plus_https_with_ref(self) -> None:
        s = RemoteSource.parse("git+https://github.com/org/repo@main")
        assert s.url == "https://github.com/org/repo"
        assert s.ref == "main"
        assert s.subpath == ""

    def test_plain_https_without_ref_defaults_to_head(self) -> None:
        s = RemoteSource.parse("https://github.com/org/repo")
        assert s.url == "https://github.com/org/repo"
        assert s.ref == "HEAD"

    def test_ssh_url_preserves_user_at_host(self) -> None:
        """The `@` in `git@github.com` must not be parsed as a ref."""
        s = RemoteSource.parse("git+ssh://git@github.com/org/repo")
        assert s.url == "ssh://git@github.com/org/repo"
        assert s.ref == "HEAD"

    def test_ssh_url_with_ref(self) -> None:
        s = RemoteSource.parse("git+ssh://git@github.com/org/repo@develop")
        assert s.url == "ssh://git@github.com/org/repo"
        assert s.ref == "develop"

    def test_subpath_propagates(self) -> None:
        s = RemoteSource.parse(
            "git+https://github.com/org/repo@main", default_path="pm/"
        )
        assert s.subpath == "pm"

    def test_empty_value_raises(self) -> None:
        with pytest.raises(RemoteConfigError, match="empty"):
            RemoteSource.parse("")


# ---------------------------------------------------------------------------
# shallow_merge
# ---------------------------------------------------------------------------


class TestShallowMerge:
    def test_local_only_keys_skipped(self) -> None:
        remote = {"polling_ms": 30000, "trackers": {"jira": {"max_concurrent": 1}}}
        local = {
            "config_source": "git+url@main",
            "config_path": "pm/",
            "config_refresh_ms": 30000,
        }
        merged = shallow_merge(remote=remote, local=local)
        assert "config_source" not in merged
        assert merged["polling_ms"] == 30000

    def test_local_overrides_scalar_when_remote_has_key(self) -> None:
        remote = {"polling_ms": 30000, "trackers": {}}
        local = {"polling_ms": 5000}
        merged = shallow_merge(remote=remote, local=local)
        assert merged["polling_ms"] == 5000

    def test_local_introducing_new_top_level_key_rejected(self) -> None:
        remote = {"polling_ms": 30000, "trackers": {}}
        local = {"made_up_key": "nope"}
        with pytest.raises(RemoteConfigError, match="unknown key 'made_up_key'"):
            shallow_merge(remote=remote, local=local)

    def test_local_introducing_new_tracker_rejected(self) -> None:
        remote = {"trackers": {"jira": {"max_concurrent": 1}}}
        local = {"trackers": {"linear": {"max_concurrent": 1}}}
        with pytest.raises(RemoteConfigError, match="unknown tracker 'linear'"):
            shallow_merge(remote=remote, local=local)

    def test_tracker_scalar_override_merges(self) -> None:
        remote = {
            "trackers": {
                "jira": {
                    "kind": "jira",
                    "max_concurrent": 1,
                    "jql_extra": "AND assignee in (alice)",
                },
            }
        }
        local = {
            "trackers": {
                "jira": {
                    "jql_extra": "AND assignee in (bob)",
                    "max_concurrent": 3,
                },
            }
        }
        merged = shallow_merge(remote=remote, local=local)
        assert merged["trackers"]["jira"]["kind"] == "jira"  # preserved
        assert merged["trackers"]["jira"]["max_concurrent"] == 3
        assert merged["trackers"]["jira"]["jql_extra"] == "AND assignee in (bob)"

    def test_tracker_dict_field_merges_entrywise(self) -> None:
        """A local override of one instruction_by_repo entry must keep the
        remote's other entries (entry-wise merge, not wholesale replace)."""
        remote = {
            "trackers": {
                "github": {
                    "kind": "github",
                    "instruction_by_repo": {"org/a": "x", "org/b": "y"},
                },
            }
        }
        local = {"trackers": {"github": {"instruction_by_repo": {"org/a": "z"}}}}
        merged = shallow_merge(remote=remote, local=local)
        assert merged["trackers"]["github"]["instruction_by_repo"] == {
            "org/a": "z",  # local override
            "org/b": "y",  # remote entry preserved
        }


# ---------------------------------------------------------------------------
# load_remote_config end-to-end against a temp git repo
# ---------------------------------------------------------------------------


def _have_git() -> bool:
    return shutil.which("git") is not None


@pytest.mark.skipif(not _have_git(), reason="git not on PATH")
class TestLoadRemoteConfig:
    @staticmethod
    def _make_remote_repo(tmp_path: Path, body: str, *, branch: str = "main") -> Path:
        remote = tmp_path / "remote-repo"
        remote.mkdir()
        subprocess.run(["git", "init", "-q", "-b", branch], cwd=remote, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@local"], cwd=remote, check=True
        )
        subprocess.run(["git", "config", "user.name", "test"], cwd=remote, check=True)
        (remote / "symphony.yaml").write_text(body)
        (remote / "symphony-prompt.md").write_text("default prompt")
        subprocess.run(["git", "add", "."], cwd=remote, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=remote, check=True)
        return remote

    def test_no_config_source_returns_none(self, tmp_path: Path) -> None:
        local = tmp_path / "symphony.yaml"
        local.write_text("tracker:\n  base_url: https://x\n  project_key: P\n")
        working, merged, source = load_remote_config(
            local, cache_root=tmp_path / "cache"
        )
        assert working is None
        assert merged is None
        assert source is None

    def test_clones_remote_and_merges(self, tmp_path: Path) -> None:
        remote = self._make_remote_repo(
            tmp_path,
            textwrap.dedent(
                """\
                trackers:
                  jira:
                    kind: jira
                    base_url: https://example.atlassian.net
                    project_key: FIS
                    max_concurrent: 1
                polling_ms: 30000
                """
            ),
        )
        local = tmp_path / "symphony.yaml"
        local.write_text(
            textwrap.dedent(
                f"""\
                config_source: git+file://{remote}@main
                trackers:
                  jira:
                    max_concurrent: 3
                polling_ms: 5000
                """
            )
        )
        working, merged, source = load_remote_config(
            local, cache_root=tmp_path / "cache"
        )
        assert source is not None
        assert source.ref == "main"
        assert working is not None
        assert (working / "symphony.yaml").is_file()
        assert merged is not None
        assert merged["polling_ms"] == 5000  # local override
        assert merged["trackers"]["jira"]["max_concurrent"] == 3  # local override
        assert merged["trackers"]["jira"]["project_key"] == "FIS"  # remote inherited

    def test_clones_remote_without_ref_uses_default_branch(
        self, tmp_path: Path
    ) -> None:
        """config_source with no @ref (ref defaults to HEAD) must clone the
        remote's default branch — NOT pass `--branch HEAD`, which git rejects."""
        remote = self._make_remote_repo(
            tmp_path,
            "trackers:\n  jira:\n    kind: jira\n"
            "    base_url: https://x\n    project_key: P\n",
            branch="main",
        )
        local = tmp_path / "symphony.yaml"
        local.write_text(f"config_source: git+file://{remote}\n")  # no @ref
        working, merged, source = load_remote_config(
            local, cache_root=tmp_path / "cache"
        )
        assert source is not None
        assert source.ref == "HEAD"
        assert working is not None and (working / "symphony.yaml").is_file()
        assert merged is not None
        assert merged["trackers"]["jira"]["project_key"] == "P"

    def test_remote_missing_symphony_yaml_raises(self, tmp_path: Path) -> None:
        # Create remote without symphony.yaml.
        remote = tmp_path / "empty-remote"
        remote.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=remote, check=True)
        subprocess.run(["git", "config", "user.email", "t@l"], cwd=remote, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=remote, check=True)
        (remote / "readme").write_text("x")
        subprocess.run(["git", "add", "."], cwd=remote, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=remote, check=True)

        local = tmp_path / "symphony.yaml"
        local.write_text(f"config_source: git+file://{remote}@main\n")
        with pytest.raises(RemoteConfigError, match="remote config not found"):
            load_remote_config(local, cache_root=tmp_path / "cache")

    def test_unknown_local_key_against_remote_rejected(self, tmp_path: Path) -> None:
        remote = self._make_remote_repo(
            tmp_path,
            "polling_ms: 30000\ntrackers: {}\n",
        )
        local = tmp_path / "symphony.yaml"
        local.write_text(f"config_source: git+file://{remote}@main\nmade_up: yes\n")
        with pytest.raises(RemoteConfigError, match="unknown key 'made_up'"):
            load_remote_config(local, cache_root=tmp_path / "cache")
