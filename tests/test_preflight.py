"""Tests for preflight checks."""

from __future__ import annotations

import json
import logging
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.preflight import (
    check_backend,
    check_claude_cli,
    check_codex_cli,
    check_ollama_mode,
    check_slack,
    check_telegram,
    require_env,
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
        with pytest.raises(SystemExit, match=r"check failed.*exit 2"):
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
    """`check_backend` now resolves every profile in `agent.profiles` as well as
    the global config, so each case needs its own DATA_DIR: a `config.yaml` left
    by another test would add profiles and extra CLI checks to these counts."""

    @pytest.fixture(autouse=True)
    def _own_config(self, operator_settings):
        return operator_settings

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
        # The profile resolves before anything dispatches, and ollama mode does
        # not resolve without a model.
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3.6:latest")
        check_backend()
        mock_check.assert_called_once()

    @patch("claude_on_the_fly.preflight.check_pty_mode")
    def test_pty_mode_dispatches(self, mock_check, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "pty")
        check_backend()
        mock_check.assert_called_once()

    def test_unknown_backend_exits(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "gemini")
        with pytest.raises(SystemExit, match="Unknown AGENT_BACKEND"):
            check_backend()

    def test_unknown_mode_exits(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "magic")
        with pytest.raises(SystemExit, match="Unknown CLAUDE_MODE"):
            check_backend()

    @patch("claude_on_the_fly.preflight.check_codex_cli")
    def test_codex_native_dispatches(self, mock_check, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        check_backend()
        mock_check.assert_called_once()

    @patch("claude_on_the_fly.preflight.check_ollama_mode")
    def test_codex_ollama_dispatches(self, mock_check, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3.6:latest")
        check_backend()
        mock_check.assert_called_once_with("codex")

    @patch("claude_on_the_fly.preflight.check_codex_cli")
    def test_codex_pty_checks_the_same_binary(
        self, mock_check, clear_backend_env, monkeypatch
    ):
        """tmux is not required: without a pane the turn degrades to `codex exec`
        rather than failing, so this must not refuse to start."""
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODE", "pty")
        check_backend()
        mock_check.assert_called_once()

    def test_codex_unknown_mode_exits(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODE", "magic")
        with pytest.raises(SystemExit, match="Unknown CODEX_MODE"):
            check_backend()


# ---------------------------------------------------------------------------
# check_codex_cli
# ---------------------------------------------------------------------------


class TestCheckCodexCli:
    @patch("claude_on_the_fly.preflight.shutil.which", return_value=None)
    def test_exits_if_not_found(self, _mock_which):
        with pytest.raises(SystemExit, match="codex CLI not found"):
            check_codex_cli()

    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/codex")
    def test_succeeds_when_present(self, _mock_which):
        check_codex_cli()  # should not raise


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
        with pytest.raises(SystemExit, match=r"ollama list failed.*boom"):
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

    @patch(
        "claude_on_the_fly.preflight.shutil.which",
        side_effect=lambda name: None if name == "codex" else "/usr/bin/" + name,
    )
    def test_exits_when_codex_missing_in_codex_mode(
        self, _mock_which, clear_backend_env, monkeypatch
    ):
        monkeypatch.setenv("OLLAMA_MODEL", "x")
        with pytest.raises(SystemExit, match="codex CLI not found"):
            check_ollama_mode("codex")

    def test_codex_mode_message_references_codex_env_var(
        self, clear_backend_env, monkeypatch
    ):
        # No OLLAMA_MODEL -> error message names CODEX_MODE for codex agent
        with (
            patch(
                "claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/x"
            ),
            pytest.raises(SystemExit, match="CODEX_MODE=ollama"),
        ):
            check_ollama_mode("codex")


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
            SystemExit, match=r"Invalid Telegram bot token.*Unauthorized"
        ):
            await check_telegram("bad-token")

    @pytest.mark.asyncio
    @patch("claude_on_the_fly.preflight.httpx.AsyncClient")
    async def test_token_is_not_emitted_by_httpx_request_logging(
        self, mock_client_cls, caplog
    ):
        client = _make_async_client({"ok": True, "result": {"username": "testbot"}})

        async def logged_get(*args, **kwargs):
            logging.getLogger("httpx").info(
                "HTTP Request: GET https://api.telegram.org/botsecret-token/getMe"
            )
            return client.get.return_value

        client.get.side_effect = logged_get
        mock_client_cls.return_value = client
        httpx_logger = logging.getLogger("httpx")
        previous_level = httpx_logger.level
        httpx_logger.setLevel(logging.NOTSET)
        try:
            with caplog.at_level(logging.INFO, logger="httpx"):
                await check_telegram("secret-token")
        finally:
            httpx_logger.setLevel(previous_level)

        assert "secret-token" not in "\n".join(r.getMessage() for r in caplog.records)


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
    async def test_returns_user_id_for_bot_token(self, mock_client_cls):
        mock_client_cls.return_value = _make_slack_client(
            {"ok": True, "user_id": "UBOT", "user": "claude", "team": "myteam"}
        )
        user_id = await check_slack("xapp-valid", "xoxb-valid")
        assert user_id == "UBOT"

    @pytest.mark.asyncio
    @patch("claude_on_the_fly.preflight.httpx.AsyncClient")
    async def test_exits_on_invalid_user_token(self, mock_client_cls):
        mock_client_cls.return_value = _make_slack_client(
            {"ok": False, "error": "invalid_auth"}
        )
        with pytest.raises(SystemExit, match=r"Invalid Slack user token.*invalid_auth"):
            await check_slack("xapp-valid", "bad-token")

    @pytest.mark.asyncio
    @patch("claude_on_the_fly.preflight.httpx.AsyncClient")
    async def test_exits_on_invalid_bot_token(self, mock_client_cls):
        mock_client_cls.return_value = _make_slack_client(
            {"ok": False, "error": "invalid_auth"}
        )
        with pytest.raises(SystemExit, match=r"Invalid Slack bot token.*invalid_auth"):
            await check_slack("xapp-valid", "xoxb-bad")

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
# run_telegram
# ---------------------------------------------------------------------------


class TestRunTelegram:
    @pytest.fixture(autouse=True)
    def _reset_backend_env(self, clear_backend_env):
        """Force native backend dispatch so a dev's `CLAUDE_MODE=pty` doesn't
        route check_backend() through check_pty_mode() and skip the patched
        check_claude_cli."""

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
    @pytest.fixture(autouse=True)
    def _reset_backend_env(self, clear_backend_env):
        """Same reason as TestRunTelegram — keep `CLAUDE_MODE=pty` from
        the dev's shell from rerouting backend dispatch in run_slack()."""

    @pytest.fixture(autouse=True)
    def _reset_slack_env(self, monkeypatch):
        """Each test sets only the SLACK_* vars it asserts on, so on a dev's
        machine the rest resolve from the real workspace — and the mismatch
        prints their live token and sender ids into the assertion diff."""
        for var in (
            "SLACK_APP_TOKEN",
            "SLACK_TOKEN",
            "SLACK_ALLOWED_SENDER_IDS",
            "SLACK_BLOCKED_SENDER_IDS",
            "SLACK_SILENT_SENDER_IDS",
            "SLACK_SLASH_COMMAND",
            # Deprecated aliases, still honored by run_slack.
            "SLACK_USER_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_ALLOWED_USER_IDS",
            "SLACK_BLOCKED_USER_IDS",
            "SLACK_ALLOWED_BOT_IDS",
        ):
            monkeypatch.delenv(var, raising=False)

    @patch("claude_on_the_fly.preflight.asyncio.run", return_value="U123")
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_returns_the_tokens_and_the_resolved_user_id(
        self, _mock_claude, _mock_arun, monkeypatch
    ):
        """The sender lists are deliberately not returned any more -- the frontend
        reads them per message so an edit needs no restart. Their resolution is
        covered in test_slack's TestSenderLists, at the place that now owns it."""
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-abc")
        assert run_slack() == ("xapp-abc", "xoxp-abc", "U123")

    @patch("claude_on_the_fly.preflight.asyncio.run", return_value="U123")
    @patch("claude_on_the_fly.preflight.check_claude_cli")
    def test_a_deprecated_alias_is_warned_about(
        self, _mock_claude, _mock_arun, monkeypatch, caplog
    ):
        """Still honored, so the only signal an operator gets is this line."""
        monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-abc")
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-abc")
        monkeypatch.setenv("SLACK_ALLOWED_USER_IDS", "U1")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.preflight"):
            run_slack()
        assert "SLACK_ALLOWED_USER_IDS is deprecated" in caplog.text


class TestCloudOllamaModels:
    # Both CLIs stubbed present: this class is about the model-availability rule, and
    # relying on the dev machine having claude and ollama installed made these pass
    # locally and fail on a CI runner that has neither.
    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/x")
    def test_a_cloud_model_absent_from_ollama_list_is_accepted(
        self, _mock_which, caplog
    ):
        """`:cloud` models are API-only and never appear in `ollama list`, so the
        availability check has to skip them or a valid config cannot start."""
        with (
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="NAME\nqwen3:8b  x\n"),
            ),
            patch.dict(
                "os.environ",
                {"OLLAMA_MODEL": "deepseek-v4-flash:cloud", "AGENT_BACKEND": "claude"},
                clear=False,
            ),
            caplog.at_level("INFO", logger="claude_on_the_fly.preflight"),
        ):
            check_ollama_mode("claude")
        assert "relying on remote API" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    @patch("claude_on_the_fly.preflight.shutil.which", return_value="/usr/bin/x")
    def test_a_local_model_absent_from_ollama_list_still_exits(self, _mock_which):
        with (
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="NAME\nqwen3:8b  x\n"),
            ),
            patch.dict(
                "os.environ",
                {"OLLAMA_MODEL": "not-pulled", "AGENT_BACKEND": "claude"},
                clear=False,
            ),
            pytest.raises(SystemExit, match="ollama pull not-pulled"),
        ):
            check_ollama_mode("claude")


class TestCheckPtyMode:
    def _ok(self, name):
        from claude_on_the_fly.checks import CheckResult

        return CheckResult(name=name, status="ok", detail="fine")

    def _warn(self, name, hint=""):
        from claude_on_the_fly.checks import CheckResult

        return CheckResult(name=name, status="warn", detail="incomplete", fix_hint=hint)

    def test_an_absent_pty_binary_is_installed_first(self, caplog):
        from claude_on_the_fly import preflight as preflight_mod

        with (
            patch("claude_on_the_fly.pty_install.is_pty_installed", return_value=False),
            patch(
                "claude_on_the_fly.pty_install.ensure_pty_installed",
                return_value=MagicMock(installed=True, message="installed 1.2.3"),
            ),
            patch(
                "claude_on_the_fly.checks.check_pty_setup",
                return_value=[self._ok("claude-pty")],
            ),
            caplog.at_level("INFO", logger="claude_on_the_fly.preflight"),
        ):
            preflight_mod.check_pty_mode()
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "installed 1.2.3" in logged
        assert "pty mode: ok" in logged

    def test_a_failed_install_stops_startup(self):
        from claude_on_the_fly import preflight as preflight_mod

        with (
            patch("claude_on_the_fly.pty_install.is_pty_installed", return_value=False),
            patch(
                "claude_on_the_fly.pty_install.ensure_pty_installed",
                return_value=MagicMock(installed=False, message="no network"),
            ),
            pytest.raises(SystemExit, match="no network"),
        ):
            preflight_mod.check_pty_mode()

    def test_a_warning_is_surfaced_without_stopping_startup(self, caplog):
        from claude_on_the_fly import preflight as preflight_mod

        with (
            patch("claude_on_the_fly.pty_install.is_pty_installed", return_value=True),
            patch(
                "claude_on_the_fly.checks.check_pty_setup",
                return_value=[self._warn("jq")],
            ),
            caplog.at_level("WARNING", logger="claude_on_the_fly.preflight"),
        ):
            preflight_mod.check_pty_mode()
        assert "incomplete" in "\n".join(r.getMessage() for r in caplog.records)


class TestStalePtyHookRefresh:
    def _warn_hooks(self, hint=""):
        from claude_on_the_fly.checks import CheckResult

        return CheckResult(
            name="claude-pty hooks", status="warn", detail="incomplete", fix_hint=hint
        )

    def _ok_hooks(self):
        from claude_on_the_fly.checks import CheckResult

        return CheckResult(name="claude-pty hooks", status="ok", detail="wired")

    def test_nothing_stale_means_no_refresh(self):
        from claude_on_the_fly import preflight as preflight_mod

        results = [self._ok_hooks()]
        assert preflight_mod._refresh_stale_pty_hooks(results) is results

    def test_the_refresh_can_be_turned_off(self, caplog):
        """statusLine is the operator's own line, so an install that rewrites it is
        something they must be able to decline."""
        from claude_on_the_fly import preflight as preflight_mod

        with (
            patch(
                "claude_on_the_fly.pty_install.auto_refresh_enabled", return_value=False
            ),
            caplog.at_level("WARNING", logger="claude_on_the_fly.preflight"),
        ):
            out = preflight_mod._refresh_stale_pty_hooks(
                [self._warn_hooks("update claude-pty")]
            )
        assert out[0].status == "warn"
        assert "disables the refresh" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    def test_a_successful_refresh_rechecks_and_reports(self, caplog):
        from claude_on_the_fly import preflight as preflight_mod

        with (
            patch(
                "claude_on_the_fly.pty_install.auto_refresh_enabled", return_value=True
            ),
            patch(
                "claude_on_the_fly.pty_install.refresh_hooks",
                return_value=(True, "hooks re-spliced"),
            ),
            patch(
                "claude_on_the_fly.checks.check_pty_setup",
                return_value=[self._ok_hooks()],
            ),
            caplog.at_level("INFO", logger="claude_on_the_fly.preflight"),
        ):
            out = preflight_mod._refresh_stale_pty_hooks([self._warn_hooks()])
        assert out[0].status == "ok"
        assert "hooks re-spliced" in "\n".join(r.getMessage() for r in caplog.records)

    def test_a_failed_refresh_says_so(self, caplog):
        from claude_on_the_fly import preflight as preflight_mod

        with (
            patch(
                "claude_on_the_fly.pty_install.auto_refresh_enabled", return_value=True
            ),
            patch(
                "claude_on_the_fly.pty_install.refresh_hooks",
                return_value=(False, "permission denied"),
            ),
            patch(
                "claude_on_the_fly.checks.check_pty_setup",
                return_value=[self._warn_hooks()],
            ),
            caplog.at_level("WARNING", logger="claude_on_the_fly.preflight"),
        ):
            preflight_mod._refresh_stale_pty_hooks([self._warn_hooks()])
        assert "hook refresh failed" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    def test_a_clean_install_that_still_lacks_the_hook_is_named_as_such(self, caplog):
        """Otherwise the same warning reads as a failed write rather than as a
        published claude-pty that predates the hook."""
        from claude_on_the_fly import preflight as preflight_mod

        with (
            patch(
                "claude_on_the_fly.pty_install.auto_refresh_enabled", return_value=True
            ),
            patch(
                "claude_on_the_fly.pty_install.refresh_hooks",
                return_value=(True, "installed"),
            ),
            patch(
                "claude_on_the_fly.checks.check_pty_setup",
                return_value=[self._warn_hooks()],
            ),
            caplog.at_level("WARNING", logger="claude_on_the_fly.preflight"),
        ):
            preflight_mod._refresh_stale_pty_hooks([self._warn_hooks()])
        assert "predates it" in "\n".join(r.getMessage() for r in caplog.records)
