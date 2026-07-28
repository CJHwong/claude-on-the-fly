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

    def test_job_command_absent_means_the_default_is_live(self):
        # On by default: absent is not "off", it is `$job`. The row appears so
        # the operator can see which trigger their install answers to.
        results = check_slack({"SLACK_APP_TOKEN": "xapp-1", "SLACK_TOKEN": "xoxb-1"})
        assert all_ok(results)
        row = next(r for r in results if r.name == "SLACK_JOB_COMMAND")
        assert row.status == "ok"
        assert "$job" in row.detail

    def test_blank_job_command_is_the_opt_out(self):
        results = check_slack(
            {
                "SLACK_APP_TOKEN": "xapp-1",
                "SLACK_TOKEN": "xoxb-1",
                "SLACK_JOB_COMMAND": "",
            }
        )
        assert all_ok(results)
        assert not any(r.name == "SLACK_JOB_COMMAND" for r in results)

    def test_job_command_valid(self):
        results = check_slack(
            {
                "SLACK_APP_TOKEN": "xapp-1",
                "SLACK_TOKEN": "xoxb-1",
                "SLACK_JOB_COMMAND": "$job",
            }
        )
        assert all_ok(results)

    @pytest.mark.parametrize(
        "value",
        [
            # Which rule each row exercises; `_job_command_error` says why.
            # A leading alphanumeric is NOT here: it works as written, so it
            # warns rather than blocks — see test_job_command_advisory_only.
            "$my job",  # whitespace — fires, but only on that exact spacing
            "/bg",  # leading '/' — Slack routes it as a slash command
            "$a<b",  # '<>&' — Slack escapes these in message text
            "$stop",  # collides with a turn-control prefix
            "$continue",  # collides with a turn-control prefix
        ],
    )
    def test_job_command_invalid(self, value):
        results = check_slack(
            {
                "SLACK_APP_TOKEN": "xapp-1",
                "SLACK_TOKEN": "xoxb-1",
                "SLACK_JOB_COMMAND": value,
            }
        )
        fail = first_failure(results)
        assert fail is not None
        assert fail.name == "SLACK_JOB_COMMAND"
        assert fail.status == "invalid"

    def test_job_command_advisory_only(self):
        """A leading word character fires exactly as written — the validator's
        own comment says so. Reporting it as invalid made supervisor.spawn
        refuse to start Slack at all over a naming preference, which is the
        collateral damage the opt-in gate exists to prevent."""
        env = {
            "SLACK_APP_TOKEN": "xapp-1",
            "SLACK_TOKEN": "xoxb-1",
            "SLACK_JOB_COMMAND": "job",
        }
        results = check_slack(env)

        note = next(r for r in results if r.name == "SLACK_JOB_COMMAND")
        assert note.status == "warn"
        assert "swallows" in note.detail
        # Surfaced in doctor, but Slack still starts.
        assert first_failure(results) is None
        assert all_ok(results)

    def test_turn_control_prefixes_match_the_slack_constants(self):
        # `_job_command_error` copies these as literals (see its comment). The
        # duplication is only safe if drift fails somewhere, so it fails here:
        # rename either constant and the validator would silently stop
        # rejecting the colliding trigger.
        from claude_on_the_fly import slack

        assert slack.STOP_COMMAND == "$stop"
        assert slack.CONTINUE_COMMAND == "$continue"

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

    def test_reports_slack_producer_off(self):
        # `claude-jobs doctor` runs check_jobs, never check_slack, so this line
        # is the only place a worker learns Slack cannot reach it. Advisory:
        # an enqueue-only install is legitimate (cron, a git hook), so the
        # worker must still start — but a worker nothing can reach is a silent
        # no-op and the operator should be told which of the two they have.
        results = check_jobs({"SLACK_TOKEN": "xoxb-123", "SLACK_JOB_COMMAND": ""})
        note = next(r for r in results if r.name == "SLACK_JOB_COMMAND")
        assert note.status == "warn"
        assert "disabled" in note.detail
        assert note.fix_hint is not None
        # Warned, never blocked: doctor must not exit 1 and spawn must not fail.
        assert all_ok(results)
        assert first_failure(results) is None

    def test_reports_slack_producer_on(self):
        results = check_jobs({"SLACK_TOKEN": "xoxb-123", "SLACK_JOB_COMMAND": "$job"})
        note = next(r for r in results if r.name == "SLACK_JOB_COMMAND")
        assert note.status == "ok"
        assert "$job" in note.detail

    def test_producer_note_does_not_contradict_the_slack_group(self):
        # Both groups render on one doctor screen, so the two rows of this name
        # must agree on severity as well as wording — a "producer on" beside
        # check_slack's "invalid" would leave the operator guessing.
        env = {"SLACK_TOKEN": "xoxb-123", "SLACK_JOB_COMMAND": "$a<b"}
        note = next(r for r in check_jobs(env) if r.name == "SLACK_JOB_COMMAND")
        assert note.status == "invalid"
        assert "misconfigured" in note.detail
        # The reason, rendered for a human — not the internal (status, reason)
        # pair, which `claude-jobs doctor` would print verbatim.
        assert "Slack escapes them" in note.detail
        assert "(" not in note.detail.split("misconfigured:")[1]
        assert (
            first_failure(check_slack({**env, "SLACK_APP_TOKEN": "xapp-1"})) is not None
        )

    def test_producer_note_survives_the_override_paths(self):
        # The token check has three branches; the note has to appear on all of
        # them, or it vanishes for exactly the installs that configured a worker.
        for env in (
            {"SLACK_TOKEN": "xoxb-1"},
            {"JOBS_SLACK_TOKEN": "xoxb-j"},
            {"JOBS_SLACK_TOKEN": "nope"},
        ):
            assert any(r.name == "SLACK_JOB_COMMAND" for r in check_jobs(env))

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

    def test_job_command_belongs_to_the_slack_daemon(self):
        # env_editor maps a changed var back to the daemons that declare it. The
        # slack daemon is the process that binds the trigger, so it is the one to
        # restart; declaring it under jobs would restart the wrong one.
        assert "SLACK_JOB_COMMAND" in FRONTEND_ENV_VARS["slack"]
        assert "SLACK_JOB_COMMAND" not in FRONTEND_ENV_VARS["jobs"]

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


class TestJobsPreflightGaps:
    """The three ways a jobs setup fails silently, each caught before a daemon
    starts rather than after it dies."""

    def test_unknown_queue_kind_is_blocking(self):
        """make_queue() raises on an unknown kind, and the Slack frontend calls
        it while building the producer — so a typo here kills *Slack*. Caught in
        preflight it is one line instead of a daemon that won't start."""
        env = {"SLACK_TOKEN": "xoxb-1", "JOBS_QUEUE_KIND": "redis"}
        results = check_jobs(env)

        row = next(r for r in results if r.name == "JOBS_QUEUE_KIND")
        assert row.status == "invalid"
        assert "redis" in row.detail
        assert "file" in (row.fix_hint or "")
        assert not all_ok(results)

    def test_known_queue_kind_passes(self):
        env = {"SLACK_TOKEN": "xoxb-1", "JOBS_QUEUE_KIND": "file"}
        row = next(r for r in check_jobs(env) if r.name == "JOBS_QUEUE_KIND")
        assert row.status == "ok"

    def test_unset_queue_kind_is_the_default(self):
        row = next(
            r
            for r in check_jobs({"SLACK_TOKEN": "xoxb-1"})
            if r.name == "JOBS_QUEUE_KIND"
        )
        assert row.status == "ok"
        assert "default" in row.detail

    def test_slack_preflight_catches_the_queue_kind_that_would_kill_it(self):
        """The frontend only builds a queue when the trigger is set, so that is
        exactly when a bad kind can take it down."""
        env = {
            "SLACK_APP_TOKEN": "xapp-1",
            "SLACK_TOKEN": "xoxb-1",
            "SLACK_JOB_COMMAND": "$job",
            "JOBS_QUEUE_KIND": "nope",
        }
        assert not all_ok(check_slack(env))

    def test_slack_ignores_the_queue_kind_when_jobs_are_off(self):
        """No trigger means no queue is constructed, so a jobs-side typo must
        not stop Slack — the collateral damage the opt-in gate exists for."""
        env = {
            "SLACK_APP_TOKEN": "xapp-1",
            "SLACK_TOKEN": "xoxb-1",
            "SLACK_JOB_COMMAND": "",
            "JOBS_QUEUE_KIND": "nope",
        }
        assert all_ok(check_slack(env))
        assert not any(r.name == "JOBS_QUEUE_KIND" for r in check_slack(env))

    def test_warns_when_the_trigger_is_on_but_no_worker_runs(self, monkeypatch):
        """`$job` acks 'I'll reply here when it's done'. With no worker that
        promise is never kept and nothing in the thread says so."""
        monkeypatch.setattr("claude_on_the_fly.heartbeat.live_pid", lambda f: None)
        env = {
            "SLACK_APP_TOKEN": "xapp-1",
            "SLACK_TOKEN": "xoxb-1",
            "SLACK_JOB_COMMAND": "$job",
        }
        results = check_slack(env)

        row = next(r for r in results if r.name == "jobs worker")
        assert row.status == "warn"
        assert "nothing drains" in row.detail
        # Advisory: the worker may be started after the frontend, and Slack must
        # not refuse to run because a different daemon is down.
        assert all_ok(results)

    def test_no_warning_when_the_worker_is_running(self, monkeypatch):
        monkeypatch.setattr("claude_on_the_fly.heartbeat.live_pid", lambda f: 4242)
        env = {
            "SLACK_APP_TOKEN": "xapp-1",
            "SLACK_TOKEN": "xoxb-1",
            "SLACK_JOB_COMMAND": "$job",
        }
        row = next(r for r in check_slack(env) if r.name == "jobs worker")
        assert row.status == "ok"
        assert "4242" in row.detail

    def test_no_worker_row_at_all_when_jobs_are_off(self):
        env = {
            "SLACK_APP_TOKEN": "xapp-1",
            "SLACK_TOKEN": "xoxb-1",
            "SLACK_JOB_COMMAND": "",
        }
        assert not any(r.name == "jobs worker" for r in check_slack(env))


class TestIsBlocking:
    def test_warn_is_advisory(self):
        from claude_on_the_fly.checks import CheckResult, is_blocking

        assert not is_blocking(CheckResult(name="x", status="warn", detail=""))
        assert not is_blocking(CheckResult(name="x", status="ok", detail=""))
        assert is_blocking(CheckResult(name="x", status="missing", detail=""))
        assert is_blocking(CheckResult(name="x", status="invalid", detail=""))


def test_job_command_default_matches_the_slack_constant():
    """checks.py copies the default as a literal so importing it never drags
    slack_bolt into a preflight run. The duplication is only safe if drift
    fails somewhere, so it fails here."""
    from claude_on_the_fly import slack
    from claude_on_the_fly.checks import DEFAULT_JOB_COMMAND

    assert DEFAULT_JOB_COMMAND == slack.DEFAULT_JOB_COMMAND


def test_effective_job_command_matches_the_frontend_resolution(monkeypatch):
    """The two resolvers must agree on absent-vs-blank, or preflight reasons
    about a trigger the daemon will not actually use."""
    from claude_on_the_fly import slack
    from claude_on_the_fly.checks import effective_job_command

    for raw in (None, "", "!bg"):
        env = {} if raw is None else {"SLACK_JOB_COMMAND": raw}
        monkeypatch.delenv("SLACK_JOB_COMMAND", raising=False)
        if raw is not None:
            monkeypatch.setenv("SLACK_JOB_COMMAND", raw)
        assert effective_job_command(env) == slack._resolve_job_command()
