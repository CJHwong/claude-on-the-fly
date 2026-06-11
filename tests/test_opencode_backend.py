"""Tests for the opencode backend."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from claude_on_the_fly.agent import NUDGE_PROMPT, OllamaLauncher, get_backend
from claude_on_the_fly.backends.opencode import (
    OpencodeBackend,
    _merge_opencode_results,
    parse_opencode_stream,
)
from claude_on_the_fly.transcript import Turn


def _ndjson(*messages: dict) -> bytes:
    return b"\n".join(json.dumps(m).encode() for m in messages)


def _text(text: str, session: str = "ses_1") -> dict:
    return {
        "type": "text",
        "sessionID": session,
        "part": {"type": "text", "text": text},
    }


def _tool(name: str, status: str = "completed", session: str = "ses_1") -> dict:
    return {
        "type": "tool_use",
        "sessionID": session,
        "part": {"type": "tool", "tool": name, "state": {"status": status}},
    }


def _step_finish(
    cost: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning: int = 0,
    cache_read: int = 0,
    session: str = "ses_1",
) -> dict:
    return {
        "type": "step_finish",
        "sessionID": session,
        "part": {
            "type": "step-finish",
            "reason": "stop",
            "cost": cost,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "reasoning": reasoning,
                "cache": {"read": cache_read, "write": 0},
            },
        },
    }


# ---------------------------------------------------------------------------
# parse_opencode_stream
# ---------------------------------------------------------------------------


class TestParseOpencodeStream:
    def test_empty_stdout_returns_defaults(self):
        out = parse_opencode_stream(b"")
        assert out["session_id"] is None
        assert out["body"] == ""
        assert out["cost"] == 0
        assert out["tokens_in"] == 0
        assert out["tokens_out"] == 0
        assert out["error"] is None
        assert out["tool_counts"] == {}

    def test_happy_path_captures_session_body_cost_tokens(self):
        stream = _ndjson(
            {
                "type": "step_start",
                "sessionID": "ses_abc",
                "part": {"type": "step-start"},
            },
            _text("probe ok.", session="ses_abc"),
            _step_finish(
                cost=0.0148705, input_tokens=3, output_tokens=6, session="ses_abc"
            ),
        )
        out = parse_opencode_stream(stream)
        assert out["session_id"] == "ses_abc"
        assert out["body"] == "probe ok."
        assert out["cost"] == pytest.approx(0.0148705)
        assert out["tokens_in"] == 3
        assert out["tokens_out"] == 6
        assert out["error"] is None
        assert out["tool_counts"] == {}

    def test_cache_read_added_to_tokens_in(self):
        stream = _ndjson(_step_finish(input_tokens=10, cache_read=90))
        out = parse_opencode_stream(stream)
        assert out["tokens_in"] == 100

    def test_reasoning_added_to_tokens_out(self):
        stream = _ndjson(_step_finish(output_tokens=40, reasoning=60))
        out = parse_opencode_stream(stream)
        assert out["tokens_out"] == 100

    def test_cost_and_tokens_summed_across_steps(self):
        stream = _ndjson(
            _step_finish(cost=0.01, input_tokens=100, output_tokens=10),
            _step_finish(cost=0.02, input_tokens=200, output_tokens=20),
        )
        out = parse_opencode_stream(stream)
        assert out["cost"] == pytest.approx(0.03)
        assert out["tokens_in"] == 300
        assert out["tokens_out"] == 30

    def test_tool_use_counted_on_completion(self):
        stream = _ndjson(
            _tool("bash"),
            _tool("edit"),
            _tool("bash"),
            _text("done"),
        )
        out = parse_opencode_stream(stream)
        assert out["tool_counts"] == {"bash": 2, "edit": 1}
        assert out["body"] == "done"

    def test_tool_use_in_progress_not_counted(self):
        stream = _ndjson(
            _tool("bash", status="running"),
            _tool("bash", status="completed"),
        )
        out = parse_opencode_stream(stream)
        # The running state is skipped; only the completed one counts.
        assert out["tool_counts"] == {"bash": 1}

    def test_last_non_empty_text_wins(self):
        stream = _ndjson(_text("first"), _text("second"), _text("   "))
        out = parse_opencode_stream(stream)
        assert out["body"] == "second"

    def test_error_event_sets_error(self):
        stream = _ndjson(
            {
                "type": "error",
                "sessionID": "ses_x",
                "error": {
                    "name": "UnknownError",
                    "data": {"message": "boom", "ref": "err_1"},
                },
            }
        )
        out = parse_opencode_stream(stream)
        assert out["error"] == "boom"
        assert out["session_id"] == "ses_x"

    def test_error_falls_back_to_name(self):
        stream = _ndjson({"type": "error", "error": {"name": "WeirdError"}})
        out = parse_opencode_stream(stream)
        assert out["error"] == "WeirdError"

    def test_malformed_lines_skipped(self):
        stream = (
            json.dumps(_text("ok")).encode()
            + b"\nnot-json\n"
            + json.dumps(_step_finish(cost=0.01)).encode()
        )
        out = parse_opencode_stream(stream)
        assert out["body"] == "ok"
        assert out["cost"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# OpencodeBackend.run
# ---------------------------------------------------------------------------


def _success_result(
    session_id: str | None = "ses_1",
    body: str = "hello",
    cost: float = 0.01,
    tokens_in: int = 100,
    tokens_out: int = 10,
    tool_counts: dict | None = None,
) -> dict:
    return {
        "session_id": session_id,
        "body": body,
        "cost": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "error": None,
        "tool_counts": tool_counts or {},
    }


class TestOpencodeBackendRun:
    async def test_first_call_starts_fresh_and_persists_mapping(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(session_id="ses_new"),
        ) as mock:
            resp = await OpencodeBackend().run(
                workspace, "our-session-1", "hi", "telegram"
            )

        cmd = mock.call_args[0][1]
        assert "-s" not in cmd  # no resume on first call
        mapping = workspace / ".opencode_sessions" / "our-session-1"
        assert mapping.exists()
        assert mapping.read_text() == "ses_new"
        assert resp.body == "hello"

    async def test_second_call_resumes_persisted_session(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sessions_dir = workspace / ".opencode_sessions"
        sessions_dir.mkdir()
        (sessions_dir / "our-session-1").write_text("ses_existing")

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(session_id="ses_existing"),
        ) as mock:
            await OpencodeBackend().run(
                workspace, "our-session-1", "follow-up", "telegram"
            )

        cmd = mock.call_args[0][1]
        assert "-s" in cmd
        idx = cmd.index("-s")
        assert cmd[idx + 1] == "ses_existing"

    async def test_no_launcher_omits_ollama_prefix(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await OpencodeBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert "ollama" not in cmd

    async def test_launcher_prepends_ollama_prefix(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await OpencodeBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )

        cmd = mock.call_args[0][1]
        assert cmd[:7] == [
            "ollama",
            "launch",
            "opencode",
            "--model",
            "deepseek-v4-flash:cloud",
            "--yes",
            "--",
        ]
        # The opencode binary is NOT repeated after `--`; first real arg is `run`.
        assert cmd[7] == "run"
        assert "opencode" not in cmd[7:], "redundant opencode binary in launcher cmd"

    async def test_launcher_drops_model_flag(self, tmp_path: Path, monkeypatch):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("OPENCODE_MODEL", "github-copilot/claude-haiku-4.5")
        launcher = OllamaLauncher(model="qwen3.6:latest")

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await OpencodeBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )

        cmd = mock.call_args[0][1]
        assert "-m" not in cmd

    async def test_native_with_model_injects_m_flag(self, tmp_path: Path, monkeypatch):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("OPENCODE_MODEL", "github-copilot/claude-haiku-4.5")

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await OpencodeBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "github-copilot/claude-haiku-4.5"

    async def test_command_includes_json_and_skip_permissions(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await OpencodeBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "json"
        assert "--dangerously-skip-permissions" in cmd

    async def test_system_prompt_prepended_on_first_turn(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await OpencodeBackend().run(
                workspace, "sess", "USER_TEXT_TOKEN", "telegram", "hoss", "dm"
            )

        composed = mock.call_args[0][1][-1]
        assert "USER_TEXT_TOKEN" in composed
        assert composed.endswith("USER_TEXT_TOKEN")
        assert not composed.startswith("\n")

    async def test_cost_is_native(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(cost=0.0148705),
        ):
            resp = await OpencodeBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.cost == pytest.approx(0.0148705)

    async def test_tokens_propagated(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(tokens_in=123, tokens_out=45),
        ):
            resp = await OpencodeBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.tokens_in == 123
        assert resp.tokens_out == 45

    async def test_model_uses_launcher_when_set(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ):
            resp = await OpencodeBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )

        assert resp.model == "deepseek-v4-flash:cloud"

    async def test_model_uses_env_in_native_mode(self, tmp_path: Path, monkeypatch):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("OPENCODE_MODEL", "github-copilot/claude-opus-4.5")

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ):
            resp = await OpencodeBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.model == "github-copilot/claude-opus-4.5"

    async def test_propagates_tool_counts_and_empty_skills(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(tool_counts={"bash": 3, "edit": 1}),
        ):
            resp = await OpencodeBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.tool_counts == {"bash": 3, "edit": 1}
        assert resp.skill_counts == {}


# ---------------------------------------------------------------------------
# _merge_opencode_results + nudge retry
# ---------------------------------------------------------------------------


class TestMergeOpencodeResults:
    def test_body_from_second(self):
        merged = _merge_opencode_results(
            _success_result(body=""), _success_result(body="final")
        )
        assert merged["body"] == "final"

    def test_cost_and_tokens_summed(self):
        merged = _merge_opencode_results(
            _success_result(cost=0.01, tokens_in=100, tokens_out=10),
            _success_result(cost=0.02, tokens_in=200, tokens_out=20),
        )
        assert merged["cost"] == pytest.approx(0.03)
        assert merged["tokens_in"] == 300
        assert merged["tokens_out"] == 30

    def test_tool_counts_merged(self):
        merged = _merge_opencode_results(
            _success_result(tool_counts={"bash": 2}),
            _success_result(tool_counts={"bash": 1, "edit": 3}),
        )
        assert merged["tool_counts"] == {"bash": 3, "edit": 3}

    def test_session_id_preserved_from_first(self):
        merged = _merge_opencode_results(
            _success_result(session_id="ses_orig"),
            _success_result(session_id="ses_ignored"),
        )
        assert merged["session_id"] == "ses_orig"


class TestOpencodeBackendNudgeRetry:
    async def test_empty_body_triggers_nudge_retry(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(session_id="ses_1", body="")
        retry = _success_result(session_id="ses_1", body="real answer")

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ) as mock:
            resp = await OpencodeBackend().run(workspace, "sess-x", "hi", "telegram")

        assert mock.await_count == 2
        retry_cmd = mock.call_args_list[1][0][1]
        assert "-s" in retry_cmd
        idx = retry_cmd.index("-s")
        assert retry_cmd[idx + 1] == "ses_1"
        assert NUDGE_PROMPT in retry_cmd
        assert resp.body == "real answer"

    async def test_non_empty_body_does_not_retry(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(body="all good"),
        ) as mock:
            resp = await OpencodeBackend().run(workspace, "sess-z", "hi", "telegram")

        assert mock.await_count == 1
        assert resp.body == "all good"

    async def test_empty_first_with_no_session_id_returns_no_response(
        self, tmp_path: Path
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            return_value=_success_result(session_id=None, body=""),
        ) as mock:
            resp = await OpencodeBackend().run(workspace, "sess-q", "hi", "telegram")

        assert mock.await_count == 1  # no retry without a session to resume
        assert resp.body == "No response"

    async def test_retry_accumulates_cost_and_tokens(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(
            session_id="ses_1", body="", cost=0.01, tokens_in=100, tokens_out=5
        )
        retry = _success_result(
            session_id="ses_1", body="done", cost=0.02, tokens_in=200, tokens_out=15
        )

        with patch(
            "claude_on_the_fly.backends.opencode._run_opencode_exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ):
            resp = await OpencodeBackend().run(workspace, "sess-y", "hi", "telegram")

        assert resp.cost == pytest.approx(0.03)
        assert resp.tokens_in == 300
        assert resp.tokens_out == 20


# ---------------------------------------------------------------------------
# Cross-backend transcript handoff
# ---------------------------------------------------------------------------


class TestOpencodeBackendHandoff:
    async def test_fresh_session_injects_claude_handoff(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        prior_turns = [
            Turn("user", "earlier question"),
            Turn("assistant", "earlier answer"),
        ]

        with (
            patch(
                "claude_on_the_fly.backends.opencode.transcript.find_latest_prior_transcript",
                return_value=(prior_turns, "claude"),
            ),
            patch(
                "claude_on_the_fly.backends.opencode._run_opencode_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await OpencodeBackend().run(
                workspace, "sess-handoff", "CURRENT_USER_TEXT", "telegram"
            )

        composed = mock.call_args[0][1][-1]
        assert "[Prior conversation via claude" in composed
        assert "earlier question" in composed
        assert composed.endswith("CURRENT_USER_TEXT")

    async def test_existing_session_skips_handoff_and_system_prompt(
        self, tmp_path: Path
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sessions_dir = workspace / ".opencode_sessions"
        sessions_dir.mkdir()
        (sessions_dir / "sess-resume").write_text("ses_existing")

        with (
            patch(
                "claude_on_the_fly.backends.opencode.transcript.find_latest_prior_transcript"
            ) as mock_lookup,
            patch(
                "claude_on_the_fly.backends.opencode._run_opencode_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await OpencodeBackend().run(
                workspace, "sess-resume", "USER_TEXT_ONLY", "telegram"
            )

        mock_lookup.assert_not_called()
        composed = mock.call_args[0][1][-1]
        assert composed == "USER_TEXT_ONLY"
        assert "Format responses" not in composed


# ---------------------------------------------------------------------------
# get_backend routes to OpencodeBackend
# ---------------------------------------------------------------------------


class TestGetBackendOpencode:
    def test_native_opencode(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "opencode")
        backend = get_backend()
        assert isinstance(backend, OpencodeBackend)
        assert backend.launcher is None

    def test_opencode_ollama_mode(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "opencode")
        monkeypatch.setenv("OPENCODE_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "deepseek-v4-flash:cloud")
        backend = get_backend()
        assert isinstance(backend, OpencodeBackend)
        assert backend.launcher == OllamaLauncher(model="deepseek-v4-flash:cloud")

    def test_opencode_ollama_without_model_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "opencode")
        monkeypatch.setenv("OPENCODE_MODE", "ollama")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            get_backend()

    def test_unknown_opencode_mode_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "opencode")
        monkeypatch.setenv("OPENCODE_MODE", "voodoo")
        with pytest.raises(ValueError, match="voodoo"):
            get_backend()


class TestOpencodeBackendTakeoverCommand:
    def test_returns_resume_command_when_mapping_exists(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        sessions_dir = workspace / ".opencode_sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "deadbeef").write_text("ses_abc\n")

        cmd = OpencodeBackend().takeover_command(workspace, "deadbeef")
        assert cmd == "opencode -s ses_abc"

    def test_returns_none_when_no_mapping(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert OpencodeBackend().takeover_command(workspace, "missing") is None

    def test_returns_none_when_mapping_empty(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        sessions_dir = workspace / ".opencode_sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "empty").write_text("")
        assert OpencodeBackend().takeover_command(workspace, "empty") is None


class TestOpencodeBackendSessionLogPath:
    def test_always_returns_none(self, tmp_path: Path) -> None:
        """opencode has no single tailable log — explicitly None."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert OpencodeBackend().session_log_path(workspace, "any-uuid") is None
