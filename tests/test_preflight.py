"""Tests for preflight checks."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.preflight import (
    check_backend,
    check_claude_cli,
    check_gws_cli,
    check_ollama_mode,
    check_slack,
    check_telegram,
    require_env,
    run_gmail,
    run_slack,
    run_telegram,
)


# ---------------------------------------------------------------------------
# require_env
# ---------------------------------------------------------------------------


class TestRequireEnv:
    def test_returns_value(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR_XYZ", "hello")
        assert require_env("TEST_VAR_XYZ") == "hello"

    def test_missing_var_exits(self, monkeypatch):
        monkeypatch.delenv("NEVER_SET_THIS_VAR", raising=False)
        with pytest.raises(SystemExit, match="Missing required environment variable"):
            require_env("NEVER_SET_THIS_VAR")

    def test_empty_string_exits(self, monkeypatch):
        monkeypatch.setenv("EMPTY_VAR", "")
        with pytest.raises(SystemExit, match="Missing required environment variable"):
            require_env("EMPTY_VAR")


# ---------------------------------------------------------------------------
# check_claude_cli
# ---------------------------------------------------------------------------


class TestCheckClaudeCli:
    @patch("claude_on_the_fly.preflight.shutil.which", return_value=None)
    def test_exits_if_not_found(self, _mock_which):
        with pytest.raises(SystemExit, match="claude CLI not found"):
            check_claude_cli()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/claude")
    def test_exits_with_auth_message(self, _mock_which, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Authentication required"
        )
        with pytest.raises(SystemExit, match="not authenticated"):
            check_claude_cli()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/claude")
    def test_exits_generic_on_nonzero(self, _mock_which, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="something broke"
        )
        with pytest.raises(SystemExit, match="check failed.*exit 2"):
            check_claude_cli()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/claude")
    def test_succeeds_on_zero(self, _mock_which, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{}", stderr=""
        )
        check_claude_cli()  # should not raise

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/claude")
    def test_downgrades_claude_unavailable_to_warning(
        self, _mock_which, mock_run, caplog
    ):
        from claude_on_the_fly.agent import ClaudeUnavailableError

        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="usage limit exceeded",
        )
        with patch(
            "claude_on_the_fly.agent._classify",
            return_value=ClaudeUnavailableError("overloaded"),
        ):
            check_claude_cli()  # should warn, not raise

        assert "claude CLI unavailable" in caplog.text


# ---------------------------------------------------------------------------
# _extract_cli_result
# ---------------------------------------------------------------------------


def test_extract_cli_result_ndjson_fallback():
    from claude_on_the_fly.preflight import _extract_cli_result

    # Non-JSON first line, but valid NDJSON lines follow
    stdout = "garbage\n" + json.dumps({"result": "hello from ndjson"})
    assert _extract_cli_result(stdout) == "hello from ndjson"


def test_extract_cli_result_skips_non_result_lines():
    from claude_on_the_fly.preflight import _extract_cli_result

    stdout = "\n".join(
        [
            "not json at all",
            json.dumps({"no_result": True}),
            json.dumps({"result": "found it"}),
        ]
    )
    assert _extract_cli_result(stdout) == "found it"


def test_extract_cli_result_empty_input():
    from claude_on_the_fly.preflight import _extract_cli_result

    assert _extract_cli_result("") == ""
    assert _extract_cli_result("   ") == ""


def test_extract_cli_result_single_json_blob():
    from claude_on_the_fly.preflight import _extract_cli_result

    assert _extract_cli_result(json.dumps({"result": "direct"})) == "direct"


def test_extract_cli_result_no_result_anywhere():
    from claude_on_the_fly.preflight import _extract_cli_result

    assert _extract_cli_result("no result here") == ""


def test_extract_cli_result_blank_lines_skipped():
    from claude_on_the_fly.preflight import _extract_cli_result

    # Blank line in NDJSON scan: last line has no "result", blank, then first has "result"
    stdout = json.dumps({"result": "first"}) + "\n\n" + json.dumps({"x": 1})
    assert _extract_cli_result(stdout) == "first"


# ---------------------------------------------------------------------------
# check_backend dispatcher
# ---------------------------------------------------------------------------


class TestCheckBackend:
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_default_dispatches_native(self, mock_check, clear_backend_env):
        check_backend()
        mock_check.assert_called_once()

    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_explicit_native_dispatches(
        self, mock_check, clear_backend_env, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_MODE", "native")
        check_backend()
        mock_check.assert_called_once()

    @patch("claude_on_the_fly.preflight.check_ollama_mode")
    def test_ollama_mode_dispatches(self, mock_check, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        check_backend()
        mock_check.assert_called_once()

    def test_unknown_backend_exits(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        with pytest.raises(SystemExit, match="Unknown AGENT_BACKEND"):
            check_backend()

    def test_unknown_mode_exits(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "magic")
        with pytest.raises(SystemExit, match="Unknown CLAUDE_MODE"):
            check_backend()


# ---------------------------------------------------------------------------
# check_ollama_mode
# ---------------------------------------------------------------------------


class TestCheckOllamaMode:
    @patch(
        "claude_on_the_fly.preflight.shutil.which",
        side_effect=lambda name: None if name == "claude" else "/usr/bin/" + name,
    )
    def test_exits_when_claude_missing(
        self, _mock_which, clear_backend_env, monkeypatch
    ):
        monkeypatch.setenv("OLLAMA_MODEL", "x")
        with pytest.raises(SystemExit, match="claude CLI not found"):
            check_ollama_mode()

    @patch(
        "claude_on_the_fly.preflight.shutil.which",
        side_effect=lambda name: None if name == "ollama" else "/usr/bin/" + name,
    )
    def test_exits_when_ollama_missing(
        self, _mock_which, clear_backend_env, monkeypatch
    ):
        monkeypatch.setenv("OLLAMA_MODEL", "x")
        with pytest.raises(SystemExit, match="ollama CLI not found"):
            check_ollama_mode()

    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/x")
    def test_exits_when_model_missing(self, _mock_which, clear_backend_env):
        with pytest.raises(SystemExit, match="OLLAMA_MODEL"):
            check_ollama_mode()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/x")
    def test_exits_when_list_fails(
        self, _mock_which, mock_run, clear_backend_env, monkeypatch
    ):
        monkeypatch.setenv("OLLAMA_MODEL", "x")
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom"
        )
        with pytest.raises(SystemExit, match="ollama list failed.*boom"):
            check_ollama_mode()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/x")
    def test_exits_when_model_not_in_list(
        self, _mock_which, mock_run, clear_backend_env, monkeypatch
    ):
        monkeypatch.setenv("OLLAMA_MODEL", "missing-model")
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="NAME    ID    SIZE    MODIFIED\nother:latest    abc    1GB    1d\n",
            stderr="",
        )
        with pytest.raises(SystemExit, match="missing-model"):
            check_ollama_mode()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/x")
    def test_succeeds_when_model_present(
        self, _mock_which, mock_run, clear_backend_env, monkeypatch
    ):
        monkeypatch.setenv("OLLAMA_MODEL", "deepseek-v4-flash:cloud")
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "NAME    ID    SIZE    MODIFIED\n"
                "deepseek-v4-flash:cloud    abc    -    2d\n"
            ),
            stderr="",
        )
        check_ollama_mode()  # should not raise


# ---------------------------------------------------------------------------
# check_telegram
# ---------------------------------------------------------------------------


def _make_async_client(response_json):
    """Build a mock httpx.AsyncClient that returns response_json for any request."""
    mock_response = MagicMock()
    mock_response.json.return_value = response_json

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestCheckTelegram:
    @pytest.mark.asyncio
    @patch("claude_on_the_fly.preflight.httpx.AsyncClient")
    async def test_succeeds_with_valid_token(self, mock_client_cls):
        mock_client_cls.return_value = _make_async_client(
            {"ok": True, "result": {"username": "testbot"}}
        )
        await check_telegram("fake-token")  # should not raise

    @pytest.mark.asyncio
    @patch("claude_on_the_fly.preflight.httpx.AsyncClient")
    async def test_exits_on_invalid_token(self, mock_client_cls):
        mock_client_cls.return_value = _make_async_client(
            {"ok": False, "description": "Unauthorized"}
        )
        with pytest.raises(
            SystemExit, match="Invalid Telegram bot token.*Unauthorized"
        ):
            await check_telegram("bad-token")


# ---------------------------------------------------------------------------
# check_slack
# ---------------------------------------------------------------------------


def _make_slack_client(post_response_json):
    """Build a mock httpx.AsyncClient that returns post_response_json for POST."""
    mock_response = MagicMock()
    mock_response.json.return_value = post_response_json

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestCheckSlack:
    @pytest.mark.asyncio
    @patch("claude_on_the_fly.preflight.httpx.AsyncClient")
    async def test_returns_user_id_from_auth_test(self, mock_client_cls):
        mock_client_cls.return_value = _make_slack_client(
            {"ok": True, "user_id": "U123", "user": "hoss", "team": "myteam"}
        )
        user_id = await check_slack("xapp-valid", "xoxp-valid")
        assert user_id == "U123"

    @pytest.mark.asyncio
    @patch("claude_on_the_fly.preflight.httpx.AsyncClient")
    async def test_exits_on_invalid_user_token(self, mock_client_cls):
        mock_client_cls.return_value = _make_slack_client(
            {"ok": False, "error": "invalid_auth"}
        )
        with pytest.raises(SystemExit, match="Invalid Slack user token.*invalid_auth"):
            await check_slack("xapp-valid", "bad-token")

    @pytest.mark.asyncio
    @patch("claude_on_the_fly.preflight.httpx.AsyncClient")
    async def test_exits_on_missing_user_id(self, mock_client_cls):
        mock_client_cls.return_value = _make_slack_client(
            {"ok": True, "user": "hoss", "team": "myteam"}
        )
        with pytest.raises(SystemExit, match="no user_id"):
            await check_slack("xapp-valid", "xoxp-valid")

    @pytest.mark.asyncio
    @patch("claude_on_the_fly.preflight.httpx.AsyncClient")
    async def test_exits_on_bad_app_token_format(self, mock_client_cls):
        mock_client_cls.return_value = _make_slack_client(
            {"ok": True, "user_id": "U123", "user": "hoss", "team": "t"}
        )
        with pytest.raises(SystemExit, match="must start with 'xapp-'"):
            await check_slack("not-xapp", "xoxp-valid")


# ---------------------------------------------------------------------------
# check_gws_cli
# ---------------------------------------------------------------------------


class TestCheckGwsCli:
    @patch("claude_on_the_fly.preflight.shutil.which", return_value=None)
    def test_exits_if_not_found(self, _mock_which):
        with pytest.raises(SystemExit, match="gws CLI not found"):
            check_gws_cli()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/gws")
    def test_exits_on_nonzero_returncode(self, _mock_which, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="auth failed"
        )
        with pytest.raises(SystemExit, match="gws auth check failed"):
            check_gws_cli()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/gws")
    def test_exits_on_invalid_token(self, _mock_which, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"token_valid": False, "enabled_apis": []}),
            stderr="",
        )
        with pytest.raises(SystemExit, match="gws token invalid"):
            check_gws_cli()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/gws")
    def test_exits_when_gmail_api_not_enabled(self, _mock_which, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"token_valid": True, "enabled_apis": ["calendar.googleapis.com"]}
            ),
            stderr="",
        )
        with pytest.raises(SystemExit, match="Gmail API not enabled"):
            check_gws_cli()

    @patch("claude_on_the_fly.preflight.subprocess.run")
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/gws")
    def test_succeeds_with_valid_status(self, _mock_which, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "token_valid": True,
                    "enabled_apis": ["gmail.googleapis.com"],
                    "user": "hoss@gofreight.com",
                }
            ),
            stderr="",
        )
        check_gws_cli()  # should not raise


# ---------------------------------------------------------------------------
# run_telegram
# ---------------------------------------------------------------------------


class TestRunTelegram:
    @patch("claude_on_the_fly.preflight.asyncio.run")
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_returns_token_and_user_id(self, _mock_claude, _mock_arun, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "42")
        token, uid = run_telegram()
        assert token == "tok123"
        assert uid == 42

    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_exits_on_non_integer_user_id(self, _mock_claude, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "not-a-number")
        with pytest.raises(SystemExit, match="must be an integer"):
            run_telegram()

    @patch("claude_on_the_fly.preflight.asyncio.run")
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_calls_check_claude_and_telegram(self, mock_claude, mock_arun, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "1")
        run_telegram()
        mock_claude.assert_called_once()
        mock_arun.assert_called_once()


# ---------------------------------------------------------------------------
# run_slack
# ---------------------------------------------------------------------------


class TestRunSlack:
    @patch("claude_on_the_fly.preflight.asyncio.run", return_value="U123")
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_returns_tokens_and_ids(self, _mock_claude, _mock_arun, monkeypatch):
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-abc")
        monkeypatch.setenv("SLACK_ALLOWED_USER_IDS", "U1, U2 ,U3")
        app, user, uid, allowed = run_slack()
        assert app == "xapp-abc"
        assert user == "xoxp-abc"
        assert uid == "U123"
        assert allowed == {"U1", "U2", "U3"}

    @patch("claude_on_the_fly.preflight.asyncio.run", return_value="U123")
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_empty_allowed_ids_yields_empty_set(
        self, _mock_claude, _mock_arun, monkeypatch
    ):
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-abc")
        monkeypatch.setenv("SLACK_ALLOWED_USER_IDS", "")
        _, _, _, allowed = run_slack()
        assert allowed == set()

    @patch("claude_on_the_fly.preflight.asyncio.run", return_value="U123")
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_missing_allowed_ids_yields_empty_set(
        self, _mock_claude, _mock_arun, monkeypatch
    ):
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-abc")
        monkeypatch.delenv("SLACK_ALLOWED_USER_IDS", raising=False)
        _, _, _, allowed = run_slack()
        assert allowed == set()


# ---------------------------------------------------------------------------
# run_gmail
# ---------------------------------------------------------------------------


class TestRunGmail:
    @patch("claude_on_the_fly.preflight.check_gws_cli")
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_returns_project_and_senders(self, _mock_claude, _mock_gws, monkeypatch):
        monkeypatch.setenv("GMAIL_GCP_PROJECT", "my-proj")
        monkeypatch.setenv("GMAIL_ALLOWED_SENDERS", "a@b.com, c@d.com")
        project, senders = run_gmail()
        assert project == "my-proj"
        assert senders == {"a@b.com", "c@d.com"}

    @patch("claude_on_the_fly.preflight.check_gws_cli")
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_exits_if_senders_empty_after_parse(
        self, _mock_claude, _mock_gws, monkeypatch
    ):
        monkeypatch.setenv("GMAIL_GCP_PROJECT", "my-proj")
        monkeypatch.setenv("GMAIL_ALLOWED_SENDERS", " , , ")
        with pytest.raises(SystemExit, match="at least one email"):
            run_gmail()
