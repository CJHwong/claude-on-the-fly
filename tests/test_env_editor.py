"""Tests for tui.env_editor — pure functions."""

from __future__ import annotations


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

    def test_gmail_vars_affect_gmail(self):
        diff = EnvDiff(removed={"GMAIL_GCP_PROJECT": "p"})
        assert affected_daemons(diff) == {"gmail"}

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
# open_in_editor — generic config-file editor launch (used for symphony.yaml)
# ---------------------------------------------------------------------------


class TestOpenInEditor:
    def test_seeds_template_when_missing(self, tmp_path):

        from claude_on_the_fly.tui.env_editor import open_in_editor

        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)

        target = tmp_path / "sub" / "symphony.yaml"
        created = open_in_editor(target, seed="# template\n", runner=fake_runner)
        assert created is True
        assert target.read_text() == "# template\n"
        # The editor was launched with the path as the last arg.
        assert calls and str(target) == calls[0][-1]

    def test_existing_file_not_overwritten(self, tmp_path):
        from claude_on_the_fly.tui.env_editor import open_in_editor

        target = tmp_path / "symphony.yaml"
        target.write_text("existing: yes\n")
        created = open_in_editor(
            target, seed="# template\n", runner=lambda cmd, **k: None
        )
        assert created is False
        assert target.read_text() == "existing: yes\n"


def test_example_yaml_parses_and_validates(tmp_path, monkeypatch):
    """The embedded template (seeded on first edit) must be a valid config."""
    from claude_on_the_fly.symphony.config import EXAMPLE_YAML, load_config

    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text(EXAMPLE_YAML)
    from claude_on_the_fly.symphony.config import JiraTrackerConfig

    cfg = load_config(cfg_path)
    cfg.validate()
    assert set(cfg.trackers) == {"jira", "github"}
    jira = cfg.trackers["jira"]
    assert isinstance(jira, JiraTrackerConfig)  # narrows for project_key
    assert jira.project_key == "PROJ"
    assert jira.instruction == "_default"
