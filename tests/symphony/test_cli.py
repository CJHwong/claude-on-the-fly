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
