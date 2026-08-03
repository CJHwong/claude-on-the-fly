"""Tests for tui.env_editor — pure functions."""

from __future__ import annotations

import os
import stat

import pytest

from claude_on_the_fly.tui.env_editor import (
    EnvDiff,
    affected_daemons,
    diff_env,
    edit_and_diff,
)

# ---------------------------------------------------------------------------
# diff_env
# ---------------------------------------------------------------------------


class TestDiffEnv:
    def test_no_change(self):
        d = diff_env({"A": "1"}, {"A": "1"})
        assert d.is_empty()

    def test_added(self):
        d = diff_env({}, {"A": "1"})
        assert d.added == {"A": "1"}
        assert d.removed == {}
        assert d.changed == {}

    def test_removed(self):
        d = diff_env({"A": "1"}, {})
        assert d.removed == {"A": "1"}

    def test_changed(self):
        d = diff_env({"A": "1"}, {"A": "2"})
        assert d.changed == {"A": ("1", "2")}

    def test_none_value_treated_as_missing(self):
        # python-dotenv yields None for a bare key with no value.
        d = diff_env({"A": None}, {"A": "1"})
        assert d.added == {"A": "1"}
        assert d.changed == {}

    def test_changed_keys_aggregates_all_three(self):
        d = diff_env({"A": "1", "B": "x"}, {"A": "2", "C": "y"})
        # A changed, B removed, C added
        assert d.changed_keys() == {"A", "B", "C"}


# ---------------------------------------------------------------------------
# affected_daemons
# ---------------------------------------------------------------------------


class TestAffectedDaemons:
    def test_telegram_var_affects_telegram(self):
        diff = EnvDiff(added={"TELEGRAM_BOT_TOKEN": "tok"})
        assert affected_daemons(diff) == {"telegram"}

    def test_slack_vars_affect_slack(self):
        diff = EnvDiff(changed={"SLACK_USER_TOKEN": ("a", "b")})
        assert affected_daemons(diff) == {"slack"}

    def test_telegram_vars_affect_telegram(self):
        diff = EnvDiff(removed={"TELEGRAM_BOT_TOKEN": "t"})
        assert affected_daemons(diff) == {"telegram"}

    def test_unknown_var_affects_nothing(self):
        diff = EnvDiff(added={"NOT_A_KNOWN_VAR": "x"})
        assert affected_daemons(diff) == set()

    def test_mixed_keys_aggregate(self):
        diff = EnvDiff(
            added={"TELEGRAM_BOT_TOKEN": "t"},
            changed={"SLACK_APP_TOKEN": ("a", "b")},
        )
        assert affected_daemons(diff) == {"telegram", "slack"}

    def test_empty_diff(self):
        assert affected_daemons(EnvDiff()) == set()


# ---------------------------------------------------------------------------
# edit_and_diff
# ---------------------------------------------------------------------------


class TestEditAndDiff:
    def test_creates_file_if_missing(self, tmp_path):
        env_file = tmp_path / ".env"

        def runner(cmd, check=False):
            # Simulate the editor writing a value.
            env_file.write_text("TELEGRAM_BOT_TOKEN=tok\n")

        diff = edit_and_diff(env_file, runner=runner)
        assert env_file.exists()
        assert diff.added == {"TELEGRAM_BOT_TOKEN": "tok"}

    def test_creates_new_env_file_owner_only(self, tmp_path):
        env_file = tmp_path / ".env"

        edit_and_diff(env_file, runner=lambda cmd, check=False: None)

        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    def test_no_change_yields_empty_diff(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("A=1\n")
        diff = edit_and_diff(env_file, runner=lambda cmd, check=False: None)
        assert diff.is_empty()

    def test_runner_receives_editor_and_path(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("A=1\n")
        monkeypatch.setenv("EDITOR", "code --wait")
        captured = []

        def runner(cmd, check=False):
            captured.append(list(cmd))

        edit_and_diff(env_file, runner=runner)
        assert captured == [["code", "--wait", str(env_file)]]

    def test_default_editor_is_vi(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("A=1\n")
        monkeypatch.delenv("EDITOR", raising=False)
        captured = []
        edit_and_diff(
            env_file, runner=lambda cmd, check=False: captured.append(list(cmd))
        )
        assert captured[0][0] == "vi"

    def test_detects_change_in_value(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=old\n")

        def runner(cmd, check=False):
            env_file.write_text("TELEGRAM_BOT_TOKEN=new\n")

        diff = edit_and_diff(env_file, runner=runner)
        assert diff.changed == {"TELEGRAM_BOT_TOKEN": ("old", "new")}


# ---------------------------------------------------------------------------
# open_in_editor — generic config-file editor launch (used for cron.yaml)
# ---------------------------------------------------------------------------


class TestOpenInEditor:
    def test_seeds_template_when_missing(self, tmp_path):

        from claude_on_the_fly.tui.env_editor import open_in_editor

        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)

        target = tmp_path / "sub" / "cron.yaml"
        created = open_in_editor(target, seed="# template\n", runner=fake_runner)
        assert created is True
        assert target.read_text() == "# template\n"
        # The editor was launched with the path as the last arg.
        assert calls and str(target) == calls[0][-1]

    def test_existing_file_not_overwritten(self, tmp_path):
        from claude_on_the_fly.tui.env_editor import open_in_editor

        target = tmp_path / "cron.yaml"
        target.write_text("existing: yes\n")
        created = open_in_editor(
            target, seed="# template\n", runner=lambda cmd, **k: None
        )
        assert created is False
        assert target.read_text() == "existing: yes\n"


def test_example_yaml_is_a_valid_template(tmp_path):
    """The seeded config must be syntactically sound, and must stop short of
    loading: it ships with an empty `entries` list on purpose, so a fresh install
    tells you to add one rather than starting a daemon that does nothing."""
    import yaml

    from claude_on_the_fly.cron import EXAMPLE_YAML, load_config

    parsed = yaml.safe_load(EXAMPLE_YAML)
    assert isinstance(parsed, dict)
    assert parsed["entries"] == []

    cfg_path = tmp_path / "cron.yaml"
    cfg_path.write_text(EXAMPLE_YAML, encoding="utf-8")
    with pytest.raises(ValueError, match="at least one entry"):
        load_config(cfg_path)


def test_a_file_created_between_the_check_and_the_open_is_not_clobbered(
    tmp_path, monkeypatch
):
    """O_EXCL is the point: two TUIs opening the editor at once must not have
    one truncate the other's new file. Losing the race means using what is
    already there."""
    env_file = tmp_path / ".env"
    real_open = os.open

    def racing_open(path, flags, mode=0o777):
        # Another process got there first, between exists() and here.
        if flags & os.O_EXCL:
            env_file.write_text("SOME_KEY=written-by-the-other-process\n")
            raise FileExistsError(17, "File exists")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", racing_open)
    diff = edit_and_diff(env_file, runner=lambda cmd, check=False: None)

    assert env_file.read_text() == "SOME_KEY=written-by-the-other-process\n"
    assert not diff.changed
