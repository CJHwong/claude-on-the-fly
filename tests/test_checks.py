"""Tests for the structured preflight checks."""

from __future__ import annotations

import json
from pathlib import Path
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
    check_jobs,
    check_pty_hooks,
    check_slack,
    check_telegram,
    first_failure,
    resolve_jobs_token,
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

    def test_slash_command_absent_is_fine(self):
        # Opt-in: no command means the picker is shortcut-only, not an error.
        results = check_slack({"SLACK_APP_TOKEN": "xapp-1", "SLACK_TOKEN": "xoxb-1"})
        assert all_ok(results)
        assert not any(r.name == "SLACK_SLASH_COMMAND" for r in results)

    def test_slash_command_valid(self):
        results = check_slack(
            {
                "SLACK_APP_TOKEN": "xapp-1",
                "SLACK_TOKEN": "xoxb-1",
                "SLACK_SLASH_COMMAND": "/cof-hoss",
            }
        )
        assert all_ok(results)

    def test_slash_command_missing_slash(self):
        # Registers fine and then never fires, so fail loudly at startup.
        results = check_slack(
            {
                "SLACK_APP_TOKEN": "xapp-1",
                "SLACK_TOKEN": "xoxb-1",
                "SLACK_SLASH_COMMAND": "cof-hoss",
            }
        )
        fail = first_failure(results)
        assert fail is not None
        assert fail.name == "SLACK_SLASH_COMMAND"
        assert fail.status == "invalid"

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
# Jobs (background worker)
# ---------------------------------------------------------------------------


class TestCheckJobs:
    def test_ok_via_shared_slack_token(self):
        results = check_jobs({"SLACK_TOKEN": "xoxb-123"})
        assert all_ok(results)
        assert results[0].name == "SLACK_TOKEN"

    def test_ok_via_jobs_override(self):
        results = check_jobs({"JOBS_SLACK_TOKEN": "xoxp-abc", "SLACK_TOKEN": "xoxb-x"})
        assert all_ok(results)
        assert results[0].name == "JOBS_SLACK_TOKEN"
        assert "user" in results[0].detail  # kind inferred from prefix

    def test_missing_token(self):
        results = check_jobs({})
        assert not all_ok(results)

    def test_invalid_override_prefix(self):
        results = check_jobs({"JOBS_SLACK_TOKEN": "nope"})
        assert results[0].status == "invalid"
        assert results[0].name == "JOBS_SLACK_TOKEN"

    def test_resolve_jobs_token_override_wins(self):
        assert resolve_jobs_token(
            {"JOBS_SLACK_TOKEN": "xoxb-j", "SLACK_TOKEN": "xoxp-s"}
        ) == (
            "JOBS_SLACK_TOKEN",
            "xoxb-j",
        )

    def test_resolve_jobs_token_falls_back(self):
        assert resolve_jobs_token({"SLACK_TOKEN": "xoxp-s"}) == (
            "SLACK_TOKEN",
            "xoxp-s",
        )


class TestJobsRegistration:
    def test_jobs_is_supervisable(self):
        assert "jobs" in SUPERVISABLE_FRONTENDS

    def test_jobs_declares_env_vars(self):
        vars_ = FRONTEND_ENV_VARS["jobs"]
        assert "JOBS_SLACK_TOKEN" in vars_
        # SLACK_TOKEN is shared with the slack frontend (editing it affects both).
        assert "SLACK_TOKEN" in vars_

    def test_check_frontend_dispatches_to_check_jobs(self):
        env = {"SLACK_TOKEN": "xoxb-1"}
        assert check_frontend("jobs", env) == check_jobs(env)

    def test_jobs_needs_no_config_file(self):
        # jobs is absent from _config_files → check_config_file returns [], so
        # check_frontend is env-only.
        results = check_frontend("jobs", {"SLACK_TOKEN": "xoxb-1"})
        assert all_ok(results)


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
            "jobs",
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


# ---------------------------------------------------------------------------
# check_pty_hooks — identifies the shims by contract, not install path
# ---------------------------------------------------------------------------


class TestCheckPtyHooks:
    """The wiring is what matters, not which install wrote it. Several tools
    vendor the same two shims into their own prefixes and rewrite
    `statusLine.command` to their copy; a path match then reports a working
    setup as missing and takes down every daemon running under CLAUDE_MODE=pty.
    """

    def _shims(self, root: Path, *, prefix: str = "vendor-tool") -> tuple[Path, Path]:
        """A statusline + Stop shim pair under an arbitrary install prefix."""
        hooks = root / prefix / "hooks"
        hooks.mkdir(parents=True)
        statusline = hooks / "statusline.sh"
        statusline.write_text(
            '#!/usr/bin/env bash\nif [ -n "${CLAUDE_PTY_SIDECAR:-}" ]; then :; fi\n'
        )
        stop = hooks / "stop_envelope.sh"
        stop.write_text(
            '#!/usr/bin/env bash\nif [ -z "${CLAUDE_PTY_ENVELOPE:-}" ]; then exit 0; fi\n'
        )
        return statusline, stop

    def _settings(self, root: Path, statusline: str, stop: str, monkeypatch) -> None:
        config_dir = root / "claude-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "settings.json").write_text(
            json.dumps(
                {
                    "statusLine": {"type": "command", "command": statusline},
                    "hooks": {"Stop": [{"hooks": [{"command": stop}]}]},
                }
            )
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    def test_shims_under_a_foreign_prefix_are_accepted(self, tmp_path, monkeypatch):
        """The regression: the path carries no `claude-interactive-p`, but both
        scripts implement the contract, so pty works and the check must say so."""
        statusline, stop = self._shims(tmp_path)
        assert "claude-interactive-p" not in str(statusline)
        self._settings(tmp_path, str(statusline), str(stop), monkeypatch)

        result = check_pty_hooks()

        assert result.status == "ok"

    def test_a_statusline_that_is_not_a_shim_is_rejected(self, tmp_path, monkeypatch):
        """Somebody's own statusline sitting in statusLine.command means pty's
        sidecar is never written — the failure the check exists to catch."""
        _, stop = self._shims(tmp_path)
        plain = tmp_path / "my-statusline.sh"
        plain.write_text('#!/usr/bin/env bash\necho "just a prompt"\n')
        self._settings(tmp_path, str(plain), str(stop), monkeypatch)

        result = check_pty_hooks()

        assert result.status == "missing"
        assert "statusLine shim" in result.detail
        assert "Stop hook" not in result.detail

    def test_a_stop_hook_that_is_not_a_shim_is_rejected(self, tmp_path, monkeypatch):
        statusline, _ = self._shims(tmp_path)
        other = tmp_path / "unrelated-hook.sh"
        other.write_text("#!/usr/bin/env bash\necho hi\n")
        self._settings(tmp_path, str(statusline), str(other), monkeypatch)

        result = check_pty_hooks()

        assert result.status == "missing"
        assert "Stop hook" in result.detail

    def test_a_command_with_arguments_still_resolves(self, tmp_path, monkeypatch):
        statusline, stop = self._shims(tmp_path)
        self._settings(tmp_path, f"{statusline} --quiet", str(stop), monkeypatch)

        assert check_pty_hooks().status == "ok"

    def test_unreadable_script_falls_back_to_the_install_path(
        self, tmp_path, monkeypatch
    ):
        """No worse than before: a shim that cannot be read but sits on the
        canonical install path is still recognised."""
        _, stop = self._shims(tmp_path)
        ghost = tmp_path / "claude-interactive-p" / "hooks" / "statusline.sh"
        self._settings(tmp_path, str(ghost), str(stop), monkeypatch)

        assert not ghost.exists()
        assert check_pty_hooks().status == "ok"

    def test_missing_settings_file_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))

        result = check_pty_hooks()

        assert result.status == "missing"
        assert "no settings.json" in result.detail
