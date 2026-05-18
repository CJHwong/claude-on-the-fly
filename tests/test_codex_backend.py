"""Tests for the codex backend."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from claude_on_the_fly.agent import NUDGE_PROMPT, OllamaLauncher, get_backend
from claude_on_the_fly.backends.codex import (
    CodexBackend,
    _merge_codex_results,
    parse_codex_stream,
)


def _ndjson(*messages: dict) -> bytes:
    return b"\n".join(json.dumps(m).encode() for m in messages)


# ---------------------------------------------------------------------------
# parse_codex_stream
# ---------------------------------------------------------------------------


class TestParseCodexStream:
    def test_empty_stdout_returns_defaults(self):
        out = parse_codex_stream(b"")
        assert out["thread_id"] is None
        assert out["body"] == ""
        assert out["usage"] == {}
        assert out["error"] is None
        assert out["tool_counts"] == {}

    def test_happy_path_captures_thread_body_usage(self):
        stream = _ndjson(
            {"type": "thread.started", "thread_id": "thread-abc"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"id": "i0", "type": "reasoning", "text": "thinking..."},
            },
            {
                "type": "item.completed",
                "item": {"id": "i1", "type": "agent_message", "text": "pong"},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 3,
                },
            },
        )
        out = parse_codex_stream(stream)
        assert out["thread_id"] == "thread-abc"
        assert out["body"] == "pong"
        assert out["usage"]["input_tokens"] == 100
        assert out["usage"]["reasoning_output_tokens"] == 3
        assert out["error"] is None
        # reasoning is not a tool
        assert out["tool_counts"] == {}

    def test_command_execution_counted_as_tool(self):
        stream = _ndjson(
            {"type": "thread.started", "thread_id": "t1"},
            {
                "type": "item.completed",
                "item": {
                    "id": "i1",
                    "type": "command_execution",
                    "command": "ls",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "i2",
                    "type": "command_execution",
                    "command": "pwd",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {"id": "i3", "type": "agent_message", "text": "done"},
            },
        )
        out = parse_codex_stream(stream)
        assert out["tool_counts"] == {"command_execution": 2}
        assert out["body"] == "done"

    def test_mixed_tool_types_each_counted_separately(self):
        stream = _ndjson(
            {
                "type": "item.completed",
                "item": {"id": "a", "type": "command_execution"},
            },
            {
                "type": "item.completed",
                "item": {"id": "b", "type": "file_change"},
            },
            {
                "type": "item.completed",
                "item": {"id": "c", "type": "command_execution"},
            },
            {
                "type": "item.completed",
                "item": {"id": "d", "type": "reasoning", "text": "..."},
            },
            {
                "type": "item.completed",
                "item": {"id": "e", "type": "agent_message", "text": "ok"},
            },
        )
        out = parse_codex_stream(stream)
        assert out["tool_counts"] == {"command_execution": 2, "file_change": 1}

    def test_turn_failed_sets_error(self):
        stream = _ndjson(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.failed", "error": {"message": "boom"}},
        )
        out = parse_codex_stream(stream)
        assert out["error"] == "boom"

    def test_reconnect_error_event_ignored(self):
        """Non-terminal `error` events (websocket retries) don't poison the result."""
        stream = _ndjson(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "error", "message": "Reconnecting... 1/5"},
            {
                "type": "item.completed",
                "item": {"id": "i1", "type": "agent_message", "text": "ok"},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 10}},
        )
        out = parse_codex_stream(stream)
        assert out["body"] == "ok"
        assert out["error"] is None

    def test_malformed_lines_skipped(self):
        stream = (
            json.dumps({"type": "thread.started", "thread_id": "t1"}).encode()
            + b"\nnot-json\n"
            + json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "i", "type": "agent_message", "text": "ok"},
                }
            ).encode()
        )
        out = parse_codex_stream(stream)
        assert out["thread_id"] == "t1"
        assert out["body"] == "ok"

    def test_empty_agent_message_does_not_clobber_existing_body(self):
        stream = _ndjson(
            {
                "type": "item.completed",
                "item": {"id": "i1", "type": "agent_message", "text": "real"},
            },
            {
                "type": "item.completed",
                "item": {"id": "i2", "type": "agent_message", "text": ""},
            },
        )
        out = parse_codex_stream(stream)
        assert out["body"] == "real"


# ---------------------------------------------------------------------------
# CodexBackend.run
# ---------------------------------------------------------------------------


def _success_result(
    thread_id: str | None = "thread-1",
    body: str = "hello",
    input_tokens: int = 100,
    cached: int = 20,
    output_tokens: int = 10,
    reasoning_tokens: int = 5,
    tool_counts: dict | None = None,
) -> dict:
    return {
        "thread_id": thread_id,
        "body": body,
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_tokens,
        },
        "error": None,
        "tool_counts": tool_counts or {},
    }


class TestCodexBackendRun:
    async def test_first_call_starts_fresh_thread_and_persists(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(thread_id="codex-thread-xyz"),
        ) as mock:
            resp = await CodexBackend().run(
                workspace, "our-session-1", "hi", "telegram"
            )

        cmd = mock.call_args[0][1]
        # No `resume` subcommand on first call.
        assert "resume" not in cmd
        # Session file written with the codex thread_id.
        session_file = workspace / ".codex_sessions" / "our-session-1"
        assert session_file.exists()
        assert session_file.read_text() == "codex-thread-xyz"
        assert resp.body == "hello"

    async def test_second_call_resumes_persisted_thread(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sessions_dir = workspace / ".codex_sessions"
        sessions_dir.mkdir()
        (sessions_dir / "our-session-1").write_text("existing-thread")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(thread_id="should-be-ignored"),
        ) as mock:
            await CodexBackend().run(
                workspace, "our-session-1", "follow-up", "telegram"
            )

        cmd = mock.call_args[0][1]
        # Resume subcommand present with the persisted thread id.
        assert "resume" in cmd
        idx = cmd.index("resume")
        assert cmd[idx + 1] == "existing-thread"
        # Session file unchanged (we do not overwrite on resume).
        assert (sessions_dir / "our-session-1").read_text() == "existing-thread"

    async def test_no_launcher_omits_ollama_prefix(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "ollama" not in cmd

    async def test_launcher_prepends_ollama_prefix(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )

        cmd = mock.call_args[0][1]
        assert cmd[:7] == [
            "ollama",
            "launch",
            "codex",
            "--model",
            "deepseek-v4-flash:cloud",
            "--yes",
            "--",
        ]
        assert cmd[7] == "codex"
        assert cmd[8] == "exec"

    async def test_launcher_drops_codex_model_flag(self, tmp_path: Path, monkeypatch):
        """With a launcher, codex's own -m must be omitted (ollama overrides it)."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("CODEX_MODEL", "o3")  # would normally inject -m o3
        launcher = OllamaLauncher(model="qwen3.6:latest")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )

        cmd = mock.call_args[0][1]
        assert "-m" not in cmd
        assert "o3" not in cmd

    async def test_native_with_codex_model_injects_m_flag(
        self, tmp_path: Path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("CODEX_MODEL", "gpt-4.1")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "gpt-4.1"

    async def test_command_includes_yolo_and_skip_git(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert "--yolo" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "--json" in cmd

    async def test_system_prompt_prepended_to_user_prompt(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(
                workspace, "sess", "USER_TEXT_TOKEN", "telegram", "hoss", "dm"
            )

        cmd = mock.call_args[0][1]
        # Composed prompt is the last argv element.
        composed = cmd[-1]
        assert "USER_TEXT_TOKEN" in composed
        assert not composed.startswith("\n")
        assert composed.endswith("USER_TEXT_TOKEN")

    async def test_response_sums_input_and_cached_tokens(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(input_tokens=200, cached=300),
        ):
            resp = await CodexBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.tokens_in == 500

    async def test_response_sums_output_and_reasoning_tokens(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(output_tokens=40, reasoning_tokens=60),
        ):
            resp = await CodexBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.tokens_out == 100

    async def test_response_model_uses_launcher_when_set(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ):
            resp = await CodexBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )

        assert resp.model == "deepseek-v4-flash:cloud"

    async def test_response_cost_is_zero(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ):
            resp = await CodexBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.cost == 0

    async def test_response_propagates_tool_counts(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(
                tool_counts={"command_execution": 3, "file_change": 1}
            ),
        ):
            resp = await CodexBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.tool_counts == {"command_execution": 3, "file_change": 1}
        assert resp.skill_counts == {}


# ---------------------------------------------------------------------------
# _merge_codex_results + nudge retry
# ---------------------------------------------------------------------------


class TestMergeCodexResults:
    def test_body_from_second(self):
        first = _success_result(body="")
        second = _success_result(body="final answer")
        merged = _merge_codex_results(first, second)
        assert merged["body"] == "final answer"

    def test_usage_summed(self):
        first = _success_result(
            input_tokens=100, cached=50, output_tokens=10, reasoning_tokens=5
        )
        second = _success_result(
            input_tokens=200, cached=30, output_tokens=80, reasoning_tokens=20
        )
        merged = _merge_codex_results(first, second)
        assert merged["usage"]["input_tokens"] == 300
        assert merged["usage"]["cached_input_tokens"] == 80
        assert merged["usage"]["output_tokens"] == 90
        assert merged["usage"]["reasoning_output_tokens"] == 25

    def test_tool_counts_merged(self):
        first = _success_result(tool_counts={"command_execution": 2})
        second = _success_result(tool_counts={"command_execution": 1, "file_change": 3})
        merged = _merge_codex_results(first, second)
        assert merged["tool_counts"] == {"command_execution": 3, "file_change": 3}

    def test_thread_id_preserved_from_first(self):
        first = _success_result(thread_id="orig")
        second = _success_result(thread_id="ignored")
        merged = _merge_codex_results(first, second)
        assert merged["thread_id"] == "orig"


class TestCodexBackendNudgeRetry:
    async def test_empty_body_triggers_nudge_retry(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(thread_id="t1", body="")
        retry = _success_result(thread_id="t1", body="real answer")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ) as mock:
            resp = await CodexBackend().run(workspace, "sess-x", "hi", "telegram")

        assert mock.await_count == 2
        # The retry must be a `resume` with the nudge prompt.
        retry_cmd = mock.call_args_list[1][0][1]
        assert "resume" in retry_cmd
        idx = retry_cmd.index("resume")
        assert retry_cmd[idx + 1] == "t1"
        assert NUDGE_PROMPT in retry_cmd
        assert resp.body == "real answer"

    async def test_retry_accumulates_tokens_and_tools(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(
            thread_id="t1",
            body="",
            input_tokens=100,
            cached=20,
            output_tokens=5,
            reasoning_tokens=3,
            tool_counts={"command_execution": 1},
        )
        retry = _success_result(
            thread_id="t1",
            body="done",
            input_tokens=200,
            cached=10,
            output_tokens=15,
            reasoning_tokens=4,
            tool_counts={"command_execution": 2, "file_change": 1},
        )

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ):
            resp = await CodexBackend().run(workspace, "sess-y", "hi", "telegram")

        assert resp.tokens_in == 100 + 20 + 200 + 10
        assert resp.tokens_out == 5 + 3 + 15 + 4
        assert resp.tool_counts == {"command_execution": 3, "file_change": 1}

    async def test_non_empty_body_does_not_retry(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(body="all good")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=first,
        ) as mock:
            resp = await CodexBackend().run(workspace, "sess-z", "hi", "telegram")

        assert mock.await_count == 1
        assert resp.body == "all good"

    async def test_empty_first_with_no_thread_id_returns_no_response(
        self, tmp_path: Path
    ):
        """If we can't recover a thread_id, we can't resume — bail with default."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(thread_id=None, body="")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=first,
        ) as mock:
            resp = await CodexBackend().run(workspace, "sess-q", "hi", "telegram")

        assert mock.await_count == 1  # no retry attempted
        assert resp.body == "No response"

    async def test_whitespace_only_body_triggers_retry(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(thread_id="t1", body="   \n  ")
        retry = _success_result(thread_id="t1", body="real")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ) as mock:
            resp = await CodexBackend().run(workspace, "sess-w", "hi", "telegram")

        assert mock.await_count == 2
        assert resp.body == "real"

    async def test_retry_also_empty_returns_no_response(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(thread_id="t1", body="")
        retry = _success_result(thread_id="t1", body="")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ):
            resp = await CodexBackend().run(workspace, "sess-v", "hi", "telegram")

        assert resp.body == "No response"


# ---------------------------------------------------------------------------
# get_backend routes to CodexBackend
# ---------------------------------------------------------------------------


class TestGetBackendCodex:
    def test_native_codex(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        backend = get_backend()
        assert isinstance(backend, CodexBackend)
        assert backend.launcher is None

    def test_codex_ollama_mode(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "deepseek-v4-flash:cloud")
        backend = get_backend()
        assert isinstance(backend, CodexBackend)
        assert backend.launcher == OllamaLauncher(model="deepseek-v4-flash:cloud")

    def test_codex_ollama_without_model_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODE", "ollama")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            get_backend()

    def test_unknown_codex_mode_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODE", "voodoo")
        with pytest.raises(ValueError, match="voodoo"):
            get_backend()
