"""Tests for the pi backend."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from claude_on_the_fly.agent import NUDGE_PROMPT, OllamaLauncher, get_backend
from claude_on_the_fly.backends.pi import (
    PiBackend,
    _workspace_to_pi_hash,
    parse_pi_stream,
)
from claude_on_the_fly.transcript import (
    Turn,
    extract_pi,
    _workspace_to_pi_hash as _transcript_pi_hash,
)


def _ndjson(*messages: dict) -> bytes:
    return b"\n".join(json.dumps(m).encode() for m in messages)


# ---------------------------------------------------------------------------
# _workspace_to_pi_hash
# ---------------------------------------------------------------------------


class TestWorkspaceToPiHash:
    def test_plain_path(self) -> None:
        assert (
            _workspace_to_pi_hash(Path("/Users/me/Workspace"))
            == "--Users-me-Workspace--"
        )

    def test_tmp_resolves_to_private_tmp(self) -> None:
        """On macOS, /tmp is a symlink to /private/tmp."""
        h = _workspace_to_pi_hash(Path("/tmp/test-ws"))
        assert h.startswith("--private-tmp-")
        assert h.endswith("test-ws--")

    def test_deeply_nested_path(self) -> None:
        h = _workspace_to_pi_hash(
            Path("/Users/me/.claude-on-the-fly/workspaces/symphony/proj-1")
        )
        assert h == "--Users-me-.claude-on-the-fly-workspaces-symphony-proj-1--"

    def test_hash_matches_transcript_module(self) -> None:
        """Both modules use the same hash function."""
        ws = Path("/Users/me/Workspace/proj")
        assert _workspace_to_pi_hash(ws) == _transcript_pi_hash(ws)


# ---------------------------------------------------------------------------
# parse_pi_stream
# ---------------------------------------------------------------------------


def _agent_end(
    body: str = "hello",
    model: str = "deepseek-v4-pro:cloud",
    provider: str = "ollama",
    usage: dict | None = None,
    tool_calls: list[dict] | None = None,
) -> dict:
    content: list[dict] = [{"type": "thinking", "thinking": "Let me respond..."}]
    if tool_calls:
        for tc in tool_calls:
            content.append(
                {
                    "type": "toolCall",
                    "id": tc.get("id", "call_x"),
                    "name": tc["name"],
                    "arguments": tc.get("arguments", {}),
                }
            )
    content.append({"type": "text", "text": body})
    default_usage = {
        "input": 100,
        "output": 10,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 110,
    }
    return {
        "type": "agent_end",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "content": content,
                "api": "openai-completions",
                "provider": provider,
                "model": model,
                "usage": usage or default_usage,
                "stopReason": "stop",
            },
        ],
        "willRetry": False,
    }


class TestParsePiStream:
    def test_empty_stdout_returns_defaults(self):
        out = parse_pi_stream(b"")
        assert out["body"] == ""
        assert out["usage"] == {}
        assert out["model"] == ""
        assert out["provider"] == ""
        assert out["error"] is None
        assert out["tool_counts"] == {}

    def test_extracts_body_model_usage(self):
        stream = _ndjson(
            _agent_end(body="hello world", model="gpt-4", provider="openai")
        )
        out = parse_pi_stream(stream)
        assert out["body"] == "hello world"
        assert out["model"] == "gpt-4"
        assert out["provider"] == "openai"
        assert out["usage"]["input"] == 100
        assert out["usage"]["output"] == 10

    def test_extracts_last_text_block_body(self):
        """Only the last assistant message's text block wins."""
        msg = {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "q"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "..."},
                        {"type": "text", "text": "first reply"},
                    ],
                    "model": "m1",
                    "provider": "p1",
                    "usage": {"input": 10, "output": 2},
                },
                {"role": "user", "content": [{"type": "text", "text": "again"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "..."},
                        {"type": "text", "text": "final reply"},
                    ],
                    "model": "m2",
                    "provider": "p2",
                    "usage": {"input": 20, "output": 5},
                },
            ],
        }
        out = parse_pi_stream(json.dumps(msg).encode())
        assert out["body"] == "final reply"
        assert out["model"] == "m2"
        assert out["provider"] == "p2"

    def test_counts_tool_calls_across_all_messages(self):
        stream = _ndjson(
            {
                "type": "agent_end",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": "c1",
                                "name": "read",
                                "arguments": {},
                            },
                            {
                                "type": "toolCall",
                                "id": "c2",
                                "name": "bash",
                                "arguments": {},
                            },
                        ],
                        "stopReason": "toolUse",
                    },
                    {"role": "user", "content": [{"type": "tool_result"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": "c3",
                                "name": "read",
                                "arguments": {},
                            },
                            {"type": "text", "text": "done"},
                        ],
                        "model": "m",
                        "provider": "p",
                        "usage": {"input": 30, "output": 3},
                    },
                ],
            }
        )
        out = parse_pi_stream(stream)
        assert out["tool_counts"] == {"read": 2, "bash": 1}
        assert out["body"] == "done"

    def test_malformed_lines_skipped(self):
        raw = json.dumps(_agent_end(body="ok")).encode() + b"\nnot-json\n"
        out = parse_pi_stream(raw)
        assert out["body"] == "ok"

    def test_will_retry_without_body_sets_error(self):
        msg = {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "..."}],
                    "model": "m",
                    "provider": "p",
                    "usage": {},
                    "stopReason": "error",
                },
            ],
            "willRetry": True,
        }
        out = parse_pi_stream(json.dumps(msg).encode())
        assert out["error"] is not None
        assert out["body"] == ""

    def test_will_retry_with_body_ignores_error(self):
        """If we have a body, willRetry is not an error."""
        msg = {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "..."},
                        {"type": "text", "text": "partial reply"},
                    ],
                    "model": "m",
                    "provider": "p",
                    "usage": {},
                },
            ],
            "willRetry": True,
        }
        out = parse_pi_stream(json.dumps(msg).encode())
        assert out["error"] is None
        assert out["body"] == "partial reply"


# ---------------------------------------------------------------------------
# extract_pi
# ---------------------------------------------------------------------------


def _pi_user(text: str) -> dict:
    return {
        "type": "message",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _pi_assistant_text(text: str) -> dict:
    return {
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": text},
            ],
            "model": "test-model",
            "provider": "test",
            "usage": {},
        },
    }


def _pi_assistant_toolcall(name: str = "read") -> dict:
    return {
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "need tool"},
                {"type": "toolCall", "id": "call_1", "name": name, "arguments": {}},
            ],
            "stopReason": "toolUse",
        },
    }


class TestExtractPi:
    def test_missing_file_returns_none(self, pi_sessions_dir):
        assert extract_pi(Path("/tmp/nowhere"), "uuid-x") is None

    def test_returns_user_and_assistant_text_in_order(self, pi_sessions_dir):
        workspace = Path("/tmp/ws-a")
        session_dir = pi_sessions_dir / _workspace_to_pi_hash(workspace)
        session_dir.mkdir(parents=True)
        (session_dir / "2026-06-06T12-00-00_uuid-a.jsonl").write_bytes(
            _ndjson(
                _pi_user("hi there"),
                _pi_assistant_text("hello"),
                _pi_user("again"),
                _pi_assistant_text("yes"),
            )
        )
        turns = extract_pi(workspace, "uuid-a")
        assert turns == [
            Turn("user", "hi there"),
            Turn("assistant", "hello"),
            Turn("user", "again"),
            Turn("assistant", "yes"),
        ]

    def test_skips_tool_call_messages(self, pi_sessions_dir):
        workspace = Path("/tmp/ws-b")
        session_dir = pi_sessions_dir / _workspace_to_pi_hash(workspace)
        session_dir.mkdir(parents=True)
        (session_dir / "2026-06-06T12-00-00_uuid-b.jsonl").write_bytes(
            _ndjson(
                _pi_user("real question"),
                _pi_assistant_toolcall("read"),
                _pi_assistant_text("final answer"),
            )
        )
        turns = extract_pi(workspace, "uuid-b")
        assert turns == [
            Turn("user", "real question"),
            Turn("assistant", "final answer"),
        ]

    def test_picks_first_text_block_per_assistant_message(self, pi_sessions_dir):
        workspace = Path("/tmp/ws-c")
        session_dir = pi_sessions_dir / _workspace_to_pi_hash(workspace)
        session_dir.mkdir(parents=True)
        msg = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},  # ignored
                ],
                "model": "m",
                "provider": "p",
                "usage": {},
            },
        }
        (session_dir / "2026-06-06T12-00-00_uuid-c.jsonl").write_bytes(_ndjson(msg))
        turns = extract_pi(workspace, "uuid-c")
        assert turns == [Turn("assistant", "first")]

    def test_empty_strings_skipped(self, pi_sessions_dir):
        workspace = Path("/tmp/ws-d")
        session_dir = pi_sessions_dir / _workspace_to_pi_hash(workspace)
        session_dir.mkdir(parents=True)
        (session_dir / "2026-06-06T12-00-00_uuid-d.jsonl").write_bytes(
            _ndjson(
                _pi_user("   "),
                _pi_assistant_text(""),
                _pi_user("real"),
                _pi_assistant_text("answer"),
            )
        )
        turns = extract_pi(workspace, "uuid-d")
        assert turns == [Turn("user", "real"), Turn("assistant", "answer")]

    def test_returns_none_when_no_turns_present(self, pi_sessions_dir):
        workspace = Path("/tmp/ws-e")
        session_dir = pi_sessions_dir / _workspace_to_pi_hash(workspace)
        session_dir.mkdir(parents=True)
        (session_dir / "2026-06-06T12-00-00_uuid-e.jsonl").write_bytes(
            _ndjson(_pi_assistant_toolcall("read"), _pi_assistant_toolcall("bash"))
        )
        turns = extract_pi(workspace, "uuid-e")
        assert turns is None

    def test_malformed_lines_tolerated(self, pi_sessions_dir):
        workspace = Path("/tmp/ws-f")
        session_dir = pi_sessions_dir / _workspace_to_pi_hash(workspace)
        session_dir.mkdir(parents=True)
        (session_dir / "2026-06-06T12-00-00_uuid-f.jsonl").write_bytes(
            json.dumps(_pi_user("ok")).encode()
            + b"\nnot-json\n"
            + json.dumps(_pi_assistant_text("yes")).encode()
            + b"\n"
        )
        turns = extract_pi(workspace, "uuid-f")
        assert turns == [Turn("user", "ok"), Turn("assistant", "yes")]


# ---------------------------------------------------------------------------
# PiBackend.run
# ---------------------------------------------------------------------------


def _success_result(
    body: str = "hello",
    model: str = "deepseek-v4-pro:cloud",
    provider: str = "ollama",
    input_tokens: int = 100,
    output_tokens: int = 10,
    tool_counts: dict | None = None,
) -> dict:
    return {
        "body": body,
        "usage": {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": input_tokens + output_tokens,
        },
        "model": model,
        "provider": provider,
        "error": None,
        "tool_counts": tool_counts or {},
    }


class TestPiBackendRun:
    async def test_first_call_creates_new_session(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            resp = await PiBackend().run(workspace, "session-1", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert "--session-id" in cmd
        idx = cmd.index("--session-id")
        assert cmd[idx + 1] == "session-1"
        assert resp.body == "hello"

    async def test_no_launcher_omits_ollama_prefix(self, tmp_path: Path, monkeypatch):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.delenv("PI_PROVIDER", raising=False)

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await PiBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert cmd[0] == "pi"
        assert "ollama" not in cmd

    async def test_launcher_prepends_ollama_prefix(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await PiBackend(launcher=launcher).run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert cmd[:7] == [
            "ollama",
            "launch",
            "pi",
            "--model",
            "deepseek-v4-flash:cloud",
            "--yes",
            "--",
        ]
        # The pi binary is NOT repeated after `--`; first real arg is `-p`.
        assert cmd[7] == "-p"

    async def test_launcher_drops_pi_model_flag(self, tmp_path: Path, monkeypatch):
        """With a launcher, pi's own --model must be omitted (ollama overrides it)."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("PI_MODEL", "deepseek-v4-pro:cloud")
        launcher = OllamaLauncher(model="qwen3.6:latest")

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await PiBackend(launcher=launcher).run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert "--model" not in cmd[7:]  # after the `--` separator

    async def test_native_with_pi_model_injects_model_flag(
        self, tmp_path: Path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("PI_MODEL", "deepseek-v4-flash:cloud")

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await PiBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert "--model" in cmd
        m_idx = cmd.index("--model")
        assert cmd[m_idx + 1] == "deepseek-v4-flash:cloud"

    async def test_native_without_model_omits_model_flag(
        self, tmp_path: Path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.delenv("PI_MODEL", raising=False)

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await PiBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert "--model" not in cmd

    async def test_provider_flag_injected(self, tmp_path: Path, monkeypatch):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("PI_PROVIDER", "ollama")

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await PiBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert "--provider" in cmd
        p_idx = cmd.index("--provider")
        assert cmd[p_idx + 1] == "ollama"

    async def test_command_includes_mode_json(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await PiBackend().run(workspace, "sess", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert "--mode" in cmd
        m_idx = cmd.index("--mode")
        assert cmd[m_idx + 1] == "json"
        assert "-p" in cmd

    async def test_empty_body_triggers_nudge_retry(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(body="")
        retry = _success_result(body="real answer")

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                side_effect=[first, retry],
            ) as mock,
        ):
            resp = await PiBackend().run(workspace, "sess-x", "hi", "telegram")

        assert mock.await_count == 2
        retry_cmd = mock.call_args_list[1][0][1]
        assert NUDGE_PROMPT in retry_cmd
        assert resp.body == "real answer"

    async def test_non_empty_body_does_not_retry(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(body="all good"),
            ) as mock,
        ):
            resp = await PiBackend().run(workspace, "sess-z", "hi", "telegram")

        assert mock.await_count == 1
        assert resp.body == "all good"

    async def test_whitespace_only_body_triggers_retry(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        first = _success_result(body="   \n  ")
        retry = _success_result(body="real")

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                side_effect=[first, retry],
            ),
        ):
            resp = await PiBackend().run(workspace, "sess-w", "hi", "telegram")

        assert resp.body == "real"

    async def test_tokens_in_sums_input_and_cache_read(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(input_tokens=200),
            ),
        ):
            resp = await PiBackend().run(workspace, "sess", "hi", "telegram")

        # input: 200, cacheRead: 0 (default)
        assert resp.tokens_in == 200
        assert resp.tokens_out == 10

    async def test_response_propagates_tool_counts(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(
                    tool_counts={"read": 3, "bash": 1, "edit": 2}
                ),
            ),
        ):
            resp = await PiBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.tool_counts == {"read": 3, "bash": 1, "edit": 2}
        assert resp.skill_counts == {}

    async def test_response_model_uses_launcher_when_set(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ),
        ):
            resp = await PiBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )

        # Model comes from pi's own output, not launcher
        assert resp.model == "deepseek-v4-pro:cloud"

    async def test_system_prompt_prepended_to_user_message(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.backends.pi._find_pi_session_path",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.pi._run_pi_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await PiBackend().run(
                workspace, "sess", "USER_TEXT_TOKEN", "telegram", "hoss", "dm"
            )

        cmd = mock.call_args[0][1]
        composed = cmd[-1]
        # System prompt is prepended into the user message (no --system-prompt flag)
        assert "USER_TEXT_TOKEN" in composed
        assert "Memory System" in composed or "Format responses" in composed
        assert composed.endswith("USER_TEXT_TOKEN")


# ---------------------------------------------------------------------------
# get_backend routes to PiBackend
# ---------------------------------------------------------------------------


class TestGetBackendPi:
    def test_native_pi(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "pi")
        backend = get_backend()
        assert isinstance(backend, PiBackend)
        assert backend.launcher is None

    def test_pi_ollama_mode(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "pi")
        monkeypatch.setenv("PI_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "deepseek-v4-flash:cloud")
        backend = get_backend()
        assert isinstance(backend, PiBackend)
        assert backend.launcher == OllamaLauncher(model="deepseek-v4-flash:cloud")

    def test_pi_ollama_without_model_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "pi")
        monkeypatch.setenv("PI_MODE", "ollama")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            get_backend()

    def test_unknown_pi_mode_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "pi")
        monkeypatch.setenv("PI_MODE", "voodoo")
        with pytest.raises(ValueError, match="voodoo"):
            get_backend()


class TestPiBackendTakeoverCommand:
    def test_returns_resume_command_when_session_exists(
        self, tmp_path: Path, pi_sessions_dir
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_dir = pi_sessions_dir / _workspace_to_pi_hash(workspace)
        session_dir.mkdir(parents=True)
        (session_dir / "2026-06-06T12-00-00_session-1.jsonl").write_text(
            '{"type":"session"}\n{"type":"message","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n'
        )

        cmd = PiBackend().takeover_command(workspace, "session-1")
        assert cmd == "pi --resume session-1"

    def test_returns_none_when_no_session(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert PiBackend().takeover_command(workspace, "missing-uuid") is None
