"""Tests for the structured preflight checks."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_on_the_fly.checks import (
    FRONTEND_ENV_VARS,
    SUPERVISABLE_FRONTENDS,
    all_ok,
    check_all,
    check_backend,
    check_binaries,
    check_frontend,
    check_gmail,
    check_slack,
    check_telegram,
    first_failure,
    resolve_slack_ids,
    slack_deprecations,
)


class TestSlackResolvers:
    def test_new_name_wins_and_no_deprecation(self):
        env = {"SLACK_ALLOWED_SENDER_IDS": "U1,B2"}
        assert resolve_slack_ids(env, "SLACK_ALLOWED_SENDER_IDS") == {"U1", "B2"}
        assert slack_deprecations(env) == []

    def test_legacy_allow_lists_merge(self):
        env = {"SLACK_ALLOWED_USER_IDS": "U1, U2", "SLACK_ALLOWED_BOT_IDS": "B9"}
        assert resolve_slack_ids(env, "SLACK_ALLOWED_SENDER_IDS") == {"U1", "U2", "B9"}

    def test_deprecation_reports_legacy_in_use(self):
        env = {"SLACK_USER_TOKEN": "xoxp-1", "SLACK_BLOCKED_USER_IDS": "U9"}
        pairs = dict(slack_deprecations(env))
        assert pairs["SLACK_USER_TOKEN"] == "SLACK_TOKEN"
        assert pairs["SLACK_BLOCKED_USER_IDS"] == "SLACK_BLOCKED_SENDER_IDS"

    def test_no_deprecation_when_preferred_set(self):
        env = {"SLACK_TOKEN": "xoxb-1", "SLACK_USER_TOKEN": "xoxp-old"}
        assert slack_deprecations(env) == []


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class TestCheckTelegram:
    def test_all_ok(self):
        results = check_telegram(
            {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "42"}
        )
        assert all_ok(results)
        assert {r.name for r in results} == {
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_USER_ID",
        }

    def test_missing_token(self):
        results = check_telegram({"TELEGRAM_ALLOWED_USER_ID": "42"})
        fail = first_failure(results)
        assert fail is not None
        assert fail.name == "TELEGRAM_BOT_TOKEN"
        assert fail.status == "missing"
        assert fail.fix_hint is not None

    def test_missing_user_id(self):
        results = check_telegram({"TELEGRAM_BOT_TOKEN": "tok"})
        names_missing = {r.name for r in results if r.status == "missing"}
        assert "TELEGRAM_ALLOWED_USER_ID" in names_missing

    def test_non_integer_user_id_is_invalid(self):
        results = check_telegram(
            {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "not-a-number"}
        )
        bad = [r for r in results if r.status == "invalid"]
        assert len(bad) == 1
        assert bad[0].name == "TELEGRAM_ALLOWED_USER_ID"
        assert "must be an integer" in bad[0].detail
        assert "not-a-number" in bad[0].detail


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


class TestCheckSlack:
    def test_all_ok(self):
        results = check_slack(
            {"SLACK_APP_TOKEN": "xapp-1", "SLACK_USER_TOKEN": "xoxp-1"}
        )
        assert all_ok(results)

    def test_missing_app_token(self):
        results = check_slack({"SLACK_USER_TOKEN": "xoxp-1"})
        fail = first_failure(results)
        assert fail is not None
        assert fail.name == "SLACK_APP_TOKEN"
        assert fail.status == "missing"

    def test_bad_app_token_format(self):
        results = check_slack(
            {"SLACK_APP_TOKEN": "not-xapp", "SLACK_USER_TOKEN": "xoxp-1"}
        )
        fail = first_failure(results)
        assert fail is not None
        assert fail.status == "invalid"
        assert "xapp-" in fail.detail

    def test_bad_user_token_format(self):
        results = check_slack(
            {"SLACK_APP_TOKEN": "xapp-1", "SLACK_USER_TOKEN": "not-xoxp"}
        )
        bad = [r for r in results if r.status == "invalid"]
        assert any(r.name == "SLACK_USER_TOKEN" for r in bad)

    def test_bot_token_ok(self):
        results = check_slack(
            {"SLACK_APP_TOKEN": "xapp-1", "SLACK_BOT_TOKEN": "xoxb-1"}
        )
        assert all_ok(results)

    def test_bad_bot_token_format(self):
        results = check_slack(
            {"SLACK_APP_TOKEN": "xapp-1", "SLACK_BOT_TOKEN": "not-xoxb"}
        )
        bad = [r for r in results if r.status == "invalid"]
        assert any(r.name == "SLACK_BOT_TOKEN" for r in bad)

    def test_slack_token_user(self):
        results = check_slack({"SLACK_APP_TOKEN": "xapp-1", "SLACK_TOKEN": "xoxp-1"})
        assert all_ok(results)
        assert any(r.name == "SLACK_TOKEN" and "(user)" in r.detail for r in results)

    def test_slack_token_bot(self):
        results = check_slack({"SLACK_APP_TOKEN": "xapp-1", "SLACK_TOKEN": "xoxb-1"})
        assert all_ok(results)
        assert any(r.name == "SLACK_TOKEN" and "(bot)" in r.detail for r in results)

    def test_bad_slack_token_format(self):
        results = check_slack({"SLACK_APP_TOKEN": "xapp-1", "SLACK_TOKEN": "nope"})
        bad = [r for r in results if r.status == "invalid"]
        assert any(r.name == "SLACK_TOKEN" for r in bad)

    def test_missing_all_bearer_tokens(self):
        results = check_slack({"SLACK_APP_TOKEN": "xapp-1"})
        fail = first_failure(results)
        assert fail is not None
        assert fail.name == "SLACK_TOKEN"
        assert fail.status == "missing"

    def test_slack_token_wins_over_legacy(self):
        results = check_slack(
            {
                "SLACK_APP_TOKEN": "xapp-1",
                "SLACK_TOKEN": "xoxb-new",
                "SLACK_USER_TOKEN": "xoxp-old",
            }
        )
        assert all_ok(results)
        assert any(r.name == "SLACK_TOKEN" and "(bot)" in r.detail for r in results)
        assert not any(r.name == "SLACK_USER_TOKEN" for r in results)


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


class TestCheckGmail:
    def test_all_ok(self):
        results = check_gmail(
            {"GMAIL_GCP_PROJECT": "my-proj", "GMAIL_ALLOWED_SENDERS": "a@b.com"}
        )
        assert all_ok(results)

    def test_missing_project(self):
        results = check_gmail({"GMAIL_ALLOWED_SENDERS": "a@b.com"})
        fail = first_failure(results)
        assert fail is not None
        assert fail.name == "GMAIL_GCP_PROJECT"

    def test_empty_senders_after_parse(self):
        results = check_gmail(
            {"GMAIL_GCP_PROJECT": "my-proj", "GMAIL_ALLOWED_SENDERS": ", , "}
        )
        bad = [r for r in results if r.name == "GMAIL_ALLOWED_SENDERS"]
        assert len(bad) == 1
        assert bad[0].status == "missing"

    def test_senders_count_in_detail(self):
        results = check_gmail(
            {
                "GMAIL_GCP_PROJECT": "my-proj",
                "GMAIL_ALLOWED_SENDERS": "a@b.com, c@d.com, e@f.com",
            }
        )
        sender_result = next(r for r in results if r.name == "GMAIL_ALLOWED_SENDERS")
        assert sender_result.status == "ok"
        assert "3" in sender_result.detail


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class TestCheckBackend:
    def test_default_claude_native(self):
        results = check_backend({})
        assert all_ok(results)
        assert any(r.name == "AGENT_BACKEND" and "claude" in r.detail for r in results)

    def test_unknown_backend(self):
        results = check_backend({"AGENT_BACKEND": "gemini"})
        fail = first_failure(results)
        assert fail is not None
        assert fail.name == "AGENT_BACKEND"
        assert fail.status == "invalid"

    def test_unknown_mode(self):
        results = check_backend({"CLAUDE_MODE": "magic"})
        bad = [r for r in results if r.status == "invalid"]
        assert any(r.name == "CLAUDE_MODE" for r in bad)

    def test_codex_native(self):
        results = check_backend({"AGENT_BACKEND": "codex"})
        assert all_ok(results)

    def test_ollama_mode_requires_model(self):
        results = check_backend({"CLAUDE_MODE": "ollama"})
        bad = [r for r in results if r.status == "missing"]
        assert any(r.name == "OLLAMA_MODEL" for r in bad)

    def test_ollama_with_model_ok(self):
        results = check_backend(
            {"CLAUDE_MODE": "ollama", "OLLAMA_MODEL": "deepseek-v4-flash:cloud"}
        )
        assert all_ok(results)

    def test_codex_ollama_validates_codex_mode_var(self):
        results = check_backend({"AGENT_BACKEND": "codex", "CODEX_MODE": "magic"})
        bad = [r for r in results if r.status == "invalid"]
        assert any(r.name == "CODEX_MODE" for r in bad)

    def test_claude_pty_mode_accepted(self):
        """`pty` is a valid CLAUDE_MODE (claude-only); env validation passes."""
        results = check_backend({"CLAUDE_MODE": "pty"})
        bad = [r for r in results if r.name == "CLAUDE_MODE" and r.status == "invalid"]
        assert not bad

    def test_codex_pty_mode_rejected(self):
        """pty is claude-only; codex should not accept it."""
        results = check_backend({"AGENT_BACKEND": "codex", "CODEX_MODE": "pty"})
        bad = [r for r in results if r.status == "invalid"]
        assert any(r.name == "CODEX_MODE" for r in bad)


# ---------------------------------------------------------------------------
# Binaries (shutil.which)
# ---------------------------------------------------------------------------


class TestCheckBinaries:
    @patch("claude_on_the_fly.checks.shutil.which", return_value="/usr/bin/claude")
    def test_claude_present(self, _mock_which):
        results = check_binaries({})
        assert all_ok(results)

    @patch("claude_on_the_fly.checks.shutil.which", return_value=None)
    def test_claude_missing(self, _mock_which):
        results = check_binaries({})
        fail = first_failure(results)
        assert fail is not None
        assert fail.name == "claude"
        assert fail.fix_hint is not None

    @patch(
        "claude_on_the_fly.checks.shutil.which",
        side_effect=lambda name: "/usr/bin/" + name if name != "ollama" else None,
    )
    def test_ollama_mode_requires_ollama(self, _mock_which):
        results = check_binaries({"CLAUDE_MODE": "ollama"})
        bad = [r for r in results if r.name == "ollama"]
        assert len(bad) == 1
        assert bad[0].status == "missing"

    @patch("claude_on_the_fly.checks.shutil.which", return_value="/usr/bin/codex")
    def test_codex_backend_checks_codex(self, _mock_which):
        results = check_binaries({"AGENT_BACKEND": "codex"})
        assert any(r.name == "codex" for r in results)


# ---------------------------------------------------------------------------
# Aggregators
# ---------------------------------------------------------------------------


class TestAggregators:
    def test_check_frontend_dispatches(self):
        assert check_frontend(
            "telegram", {"TELEGRAM_BOT_TOKEN": "t"}
        ) == check_telegram({"TELEGRAM_BOT_TOKEN": "t"})

    def test_check_frontend_schedule_requires_yaml(self, monkeypatch, tmp_path):
        # Point DATA_DIR somewhere empty; schedule.yaml absent → missing result.
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        results = check_frontend("schedule", {})
        assert any(r.status == "missing" and r.name == "schedule.yaml" for r in results)

    def test_check_frontend_schedule_ok_when_yaml_exists(self, monkeypatch, tmp_path):
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        (tmp_path / "schedule.yaml").write_text("jobs: []")
        results = check_frontend("schedule", {})
        assert all(r.status == "ok" for r in results)

    def test_check_frontend_symphony_requires_yaml(self, monkeypatch, tmp_path):
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        results = check_frontend("symphony", {})
        assert any(r.status == "missing" and r.name == "symphony.yaml" for r in results)

    def test_check_frontend_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown frontend"):
            check_frontend("nope", {})

    @patch("claude_on_the_fly.checks.shutil.which", return_value="/usr/bin/claude")
    def test_check_all_groups(self, _mock_which):
        results = check_all(
            {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_ID": "1"}
        )
        assert set(results) == {
            "telegram",
            "slack",
            "gmail",
            "schedule",
            "symphony",
            "backend",
            "binaries",
        }

    def test_check_all_uses_os_environ_by_default(self, monkeypatch):
        # Set a clearly-bad value and confirm it's picked up.
        monkeypatch.setenv("AGENT_BACKEND", "gemini")
        results = check_all()
        bad = [r for r in results["backend"] if r.status == "invalid"]
        assert any(r.name == "AGENT_BACKEND" for r in bad)


# ---------------------------------------------------------------------------
# Env var declarations — used by env_editor mapping
# ---------------------------------------------------------------------------


class TestEnvVarDeclarations:
    def test_each_supervisable_has_an_entry(self):
        for name in SUPERVISABLE_FRONTENDS:
            assert name in FRONTEND_ENV_VARS

    def test_telegram_declares_token_and_user_id(self):
        vars_ = FRONTEND_ENV_VARS["telegram"]
        assert "TELEGRAM_BOT_TOKEN" in vars_
        assert "TELEGRAM_ALLOWED_USER_ID" in vars_

    def test_slack_declares_both_tokens(self):
        vars_ = FRONTEND_ENV_VARS["slack"]
        assert "SLACK_APP_TOKEN" in vars_
        assert "SLACK_USER_TOKEN" in vars_
        assert "SLACK_ALLOWED_BOT_IDS" in vars_
        assert "SLACK_SILENT_SENDER_IDS" in vars_

    def test_gmail_declares_project_and_senders(self):
        vars_ = FRONTEND_ENV_VARS["gmail"]
        assert "GMAIL_GCP_PROJECT" in vars_
        assert "GMAIL_ALLOWED_SENDERS" in vars_

    def test_schedule_and_symphony_have_no_env_vars(self):
        assert FRONTEND_ENV_VARS["schedule"] == ()
        assert FRONTEND_ENV_VARS["symphony"] == ()
