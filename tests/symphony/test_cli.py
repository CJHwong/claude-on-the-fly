"""Tests for symphony cli: _setup_logging, _run, main."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_on_the_fly.symphony.cli import (
    DEFAULT_CONFIG,
    _cmd_takeover,
    _cmd_watch,
    _normalize_argv,
    _run,
    _setup_logging,
    main,
)


# ---------------------------------------------------------------------------
# _setup_logging
# ---------------------------------------------------------------------------


def test_setup_logging_creates_log_dir_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / ".claude-on-the-fly"
    monkeypatch.setattr("claude_on_the_fly.symphony.cli.DATA_DIR", data_dir)

    root = logging.getLogger()
    before = set(root.handlers)

    _setup_logging()

    after = set(root.handlers)
    new_handlers = after - before

    # Remove added handlers so we don't pollute other tests
    for h in new_handlers:
        h.close()
        root.removeHandler(h)

    log_dir = data_dir / "logs"
    assert log_dir.is_dir()
    log_files = list(log_dir.glob("symphony.log*"))
    assert len(log_files) > 0


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------


async def test_run_calls_orchestrator_run_loop() -> None:
    with patch("claude_on_the_fly.symphony.cli.orchestrator.run_loop") as mock_run_loop:
        mock_run_loop.return_value = None
        config_path = Path("/tmp/symphony.yaml")
        await _run(config_path)
        mock_run_loop.assert_awaited_once()
        call_args = mock_run_loop.call_args
        assert call_args.args[0] == config_path
        assert isinstance(call_args.args[1], asyncio.Event)


async def test_run_signal_handler_not_implemented() -> None:
    """NotImplementedError from add_signal_handler (Windows) is ignored."""

    loop = asyncio.get_running_loop()
    with (
        patch.object(loop, "add_signal_handler", side_effect=NotImplementedError),
        patch("claude_on_the_fly.symphony.cli.orchestrator.run_loop") as mock_run_loop,
        patch.object(asyncio, "get_running_loop", return_value=loop),
        patch.object(loop, "add_signal_handler", side_effect=NotImplementedError),
    ):
        # Re-patch on the function level - _run calls get_running_loop() and add_signal_handler
        with patch(
            "claude_on_the_fly.symphony.cli.asyncio.get_running_loop", return_value=loop
        ):
            mock_run_loop.return_value = None
            await _run(Path("/tmp/s.yaml"))
            mock_run_loop.assert_awaited_once()


def test_main_keyboard_interrupt() -> None:
    with (
        patch("claude_on_the_fly.symphony.cli.load_dotenv"),
        patch.object(sys, "argv", ["claude-symphony"]),
        patch("claude_on_the_fly.symphony.cli._setup_logging"),
        patch(
            "claude_on_the_fly.symphony.cli.asyncio.run", side_effect=KeyboardInterrupt
        ),
    ):
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        with patch(
            "claude_on_the_fly.symphony.cli.Path.expanduser", return_value=mock_path
        ):
            result = main()
            assert result == 0


# ---------------------------------------------------------------------------
# main: arg parsing
# ---------------------------------------------------------------------------


def test_main_default_config_path() -> None:
    with (
        patch("claude_on_the_fly.symphony.cli.load_dotenv"),
        patch.object(sys, "argv", ["claude-symphony"]),
        patch.object(Path, "exists", return_value=True),
        patch("claude_on_the_fly.symphony.cli._setup_logging"),
        patch("claude_on_the_fly.symphony.cli.asyncio.run"),
    ):
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        with patch(
            "claude_on_the_fly.symphony.cli.Path.expanduser", return_value=mock_path
        ):
            main()

        mock_path.exists.assert_called_once()


def test_main_config_not_found_exits_2() -> None:
    with (
        patch("claude_on_the_fly.symphony.cli.load_dotenv"),
        patch.object(sys, "argv", ["claude-symphony", "/nonexistent/config.yaml"]),
        patch("claude_on_the_fly.symphony.cli._setup_logging") as mock_logging,
    ):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch(
            "claude_on_the_fly.symphony.cli.Path.expanduser", return_value=mock_path
        ):
            result = main()
            assert result == 2
            mock_logging.assert_not_called()


def test_main_success_path() -> None:
    with (
        patch("claude_on_the_fly.symphony.cli.load_dotenv"),
        patch.object(sys, "argv", ["claude-symphony"]),
        patch("claude_on_the_fly.symphony.cli._setup_logging") as mock_logging,
        patch("claude_on_the_fly.symphony.cli.asyncio.run") as mock_asyncio_run,
    ):
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        with patch(
            "claude_on_the_fly.symphony.cli.Path.expanduser", return_value=mock_path
        ):
            result = main()

            assert result == 0
            mock_logging.assert_called_once()
            mock_asyncio_run.assert_called_once()


# ---------------------------------------------------------------------------
# arg parsing edge cases
# ---------------------------------------------------------------------------


def test_argparse_help() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="claude-symphony")
    parser.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG))
    with pytest.raises(SystemExit) as exc, patch("sys.stderr"):
        parser.parse_args(["--help"])
    assert exc.value.code == 0


def test_argparse_custom_config() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="claude-symphony")
    parser.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG))
    args = parser.parse_args(["/custom/path.yaml"])
    assert args.config == "/custom/path.yaml"


# ---------------------------------------------------------------------------
# _normalize_argv: legacy [config] form rewrites to ["run", config]
# ---------------------------------------------------------------------------


class TestNormalizeArgv:
    def test_empty_becomes_run(self) -> None:
        assert _normalize_argv([]) == ["run"]

    def test_bare_config_path_prepends_run(self) -> None:
        assert _normalize_argv(["/tmp/cfg.yaml"]) == ["run", "/tmp/cfg.yaml"]

    def test_run_subcommand_passthrough(self) -> None:
        assert _normalize_argv(["run", "/tmp/cfg.yaml"]) == ["run", "/tmp/cfg.yaml"]

    def test_takeover_subcommand_passthrough(self) -> None:
        assert _normalize_argv(["takeover", "PROJ-1"]) == ["takeover", "PROJ-1"]

    def test_help_flag_passthrough(self) -> None:
        assert _normalize_argv(["--help"]) == ["--help"]


# ---------------------------------------------------------------------------
# takeover subcommand
# ---------------------------------------------------------------------------


def test_cmd_takeover_prints_resume_one_liner(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    ticket = "PROJ-42"
    fake_workspace = tmp_path / "ws"
    fake_workspace.mkdir()
    fake_uuid = "uuid-abc"

    backend = MagicMock()
    backend.takeover_command.return_value = f"claude --resume {fake_uuid}"

    with (
        patch(
            "claude_on_the_fly.symphony.cli.ensure_workspace",
            return_value=fake_workspace,
        ),
        patch(
            "claude_on_the_fly.symphony.cli.session_uuid_for",
            return_value=fake_uuid,
        ),
        patch("claude_on_the_fly.symphony.cli.get_backend", return_value=backend),
    ):
        rc = _cmd_takeover(ticket)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"cd {fake_workspace}" in out
    assert f"claude --resume {fake_uuid}" in out
    assert "claude-tui stop symphony" in out
    assert "claude-tui resume" in out


def test_cmd_takeover_no_session_yet_exits_1(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    backend = MagicMock()
    backend.takeover_command.return_value = None

    with (
        patch(
            "claude_on_the_fly.symphony.cli.ensure_workspace",
            return_value=tmp_path / "ws",
        ),
        patch(
            "claude_on_the_fly.symphony.cli.session_uuid_for", return_value="missing"
        ),
        patch("claude_on_the_fly.symphony.cli.get_backend", return_value=backend),
    ):
        rc = _cmd_takeover("PROJ-1")

    assert rc == 1
    err = capsys.readouterr().err
    assert "no session yet" in err


def test_main_takeover_dispatches_to_cmd_takeover() -> None:
    with (
        patch("claude_on_the_fly.symphony.cli.load_dotenv"),
        patch.object(sys, "argv", ["claude-symphony", "takeover", "PROJ-9"]),
        patch(
            "claude_on_the_fly.symphony.cli._cmd_takeover", return_value=0
        ) as mock_cmd,
    ):
        rc = main()

    assert rc == 0
    mock_cmd.assert_called_once_with("PROJ-9")


# ---------------------------------------------------------------------------
# watch subcommand
# ---------------------------------------------------------------------------


def test_cmd_watch_no_session_exits_1(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    backend = MagicMock()
    backend.session_log_path.return_value = None

    with (
        patch(
            "claude_on_the_fly.symphony.cli.ensure_workspace",
            return_value=tmp_path / "ws",
        ),
        patch(
            "claude_on_the_fly.symphony.cli.session_uuid_for", return_value="missing"
        ),
        patch("claude_on_the_fly.symphony.cli.get_backend", return_value=backend),
    ):
        rc = _cmd_watch("PROJ-1")

    assert rc == 1
    err = capsys.readouterr().err
    assert "no session log" in err


def test_cmd_watch_tails_and_formats(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake_log = tmp_path / "session.jsonl"
    fake_log.write_text("")

    backend = MagicMock()
    backend.session_log_path.return_value = fake_log

    # tail() yields one event, then we simulate the user hitting Ctrl-C so
    # _cmd_watch exits cleanly without hanging.
    fake_events = iter(
        [
            {
                "type": "assistant",
                "timestamp": "2026-05-21T04:53:36.000Z",
                "message": {"content": [{"type": "text", "text": "Hello"}]},
            },
        ]
    )

    def fake_tail(path, **kwargs):
        yield next(fake_events)
        raise KeyboardInterrupt

    with (
        patch(
            "claude_on_the_fly.symphony.cli.ensure_workspace",
            return_value=tmp_path / "ws",
        ),
        patch("claude_on_the_fly.symphony.cli.session_uuid_for", return_value="u"),
        patch("claude_on_the_fly.symphony.cli.get_backend", return_value=backend),
        patch("claude_on_the_fly.symphony.cli.watch.tail", side_effect=fake_tail),
    ):
        rc = _cmd_watch("PROJ-1")

    assert rc == 0
    out = capsys.readouterr().out
    # Rich's Console strips markup tags when stdout isn't a TTY (capsys), so
    # we assert on the visible content rather than the markup syntax.
    assert "watching" in out
    assert "ASSISTANT" in out
    assert "Hello" in out
    assert "stopped." in out


def test_main_watch_dispatches() -> None:
    with (
        patch("claude_on_the_fly.symphony.cli.load_dotenv"),
        patch.object(sys, "argv", ["claude-symphony", "watch", "PROJ-9"]),
        patch("claude_on_the_fly.symphony.cli._cmd_watch", return_value=0) as mock_cmd,
    ):
        rc = main()

    assert rc == 0
    mock_cmd.assert_called_once_with("PROJ-9")
