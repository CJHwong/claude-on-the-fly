"""Tests for the codex backend."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.agent import NUDGE_PROMPT, OllamaLauncher, get_backend
from claude_on_the_fly.backends import codex as codex_mod
from claude_on_the_fly.backends.codex import (
    CodexBackend,
    _merge_codex_results,
    parse_codex_stream,
)
from claude_on_the_fly.transcript import Turn


def _ndjson(*messages: dict) -> bytes:
    return b"\n".join(json.dumps(m).encode() for m in messages)


def _write_mapping(workspace: Path, session_uuid: str, thread_id: str) -> Path:
    """Create a daemon-owned mapping for backend tests."""
    return codex_mod.codex_state.write_thread_id(workspace, session_uuid, thread_id)


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
# _run_codex_exec
# ---------------------------------------------------------------------------


def _agent_message(text: str) -> dict:
    return {
        "type": "item.completed",
        "item": {"id": "i1", "type": "agent_message", "text": text},
    }


def _turn_completed(**usage: int) -> dict:
    return {"type": "turn.completed", "usage": usage}


def _exec_proc(returncode: int, stdout: bytes, stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


async def _run_exec(proc, tmp_path: Path):
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch.object(codex_mod.agent, "track_agent_process"),
        patch.object(codex_mod.agent, "_kill_process_tree", AsyncMock()),
    ):
        return await codex_mod._run_codex_exec(
            tmp_path, ["codex", "exec"], timeout=None
        )


class TestRunCodexExec:
    async def test_nonzero_exit_after_completed_turn_still_returns_body(
        self, tmp_path: Path, caplog
    ):
        """codex can deadlock at exit with its reply already on stdout; the
        turn is finished, so the reply must survive being killed."""
        proc = _exec_proc(
            -9,
            _ndjson(
                {"type": "thread.started", "thread_id": "t1"},
                _agent_message("PR #804 is merged."),
                _turn_completed(input_tokens=100, output_tokens=5),
            ),
            stderr=b"ERROR codex_core::tools::router: stale noise",
        )
        with caplog.at_level(logging.WARNING):
            out = await _run_exec(proc, tmp_path)
        assert out["body"] == "PR #804 is merged."
        assert out["thread_id"] == "t1"
        # The deadlock is un-root-caused upstream; stderr is the only artifact
        # that can explain a given instance, so it has to reach the log.
        assert "codex_core::tools::router" in caplog.text

    async def test_nonzero_exit_mid_turn_raises_instead_of_shipping_a_fragment(
        self, tmp_path: Path
    ):
        """A turn killed mid-work leaves an intermediate agent_message that
        looks exactly like a final answer. Without turn.completed it is not
        one, and delivering it would be a silently wrong reply."""
        proc = _exec_proc(
            -9,
            _ndjson(
                {"type": "thread.started", "thread_id": "t1"},
                _agent_message("Let me check the tests first, then I'll open the PR"),
            ),
            stderr=b"killed mid-turn",
        )
        with pytest.raises(RuntimeError, match="killed mid-turn"):
            await _run_exec(proc, tmp_path)

    async def test_nonzero_exit_after_completed_turn_without_body_raises(
        self, tmp_path: Path
    ):
        """turn.completed with nothing to say is not a reply worth delivering."""
        proc = _exec_proc(-9, _ndjson(_turn_completed()), stderr=b"empty turn")
        with pytest.raises(RuntimeError, match="empty turn"):
            await _run_exec(proc, tmp_path)

    async def test_nonzero_exit_without_body_raises_stderr(self, tmp_path: Path):
        proc = _exec_proc(1, b"", stderr=b"codex: command not found")
        with pytest.raises(RuntimeError, match="command not found"):
            await _run_exec(proc, tmp_path)

    async def test_nonzero_exit_without_body_or_stderr_raises_exit_code(
        self, tmp_path: Path
    ):
        proc = _exec_proc(-15, b"")
        with pytest.raises(RuntimeError, match="Exit code -15"):
            await _run_exec(proc, tmp_path)

    async def test_turn_failed_raises_even_with_a_body(self, tmp_path: Path):
        """turn.failed is terminal: a partial body must not mask it."""
        proc = _exec_proc(
            0,
            _ndjson(
                _agent_message("partial"),
                {"type": "turn.failed", "error": {"message": "context exhausted"}},
            ),
        )
        with pytest.raises(RuntimeError, match="context exhausted"):
            await _run_exec(proc, tmp_path)

    async def test_turn_failed_wins_over_stderr_on_nonzero_exit(self, tmp_path: Path):
        proc = _exec_proc(
            1,
            _ndjson({"type": "turn.failed", "error": {"message": "rate limited"}}),
            stderr=b"noisy teardown",
        )
        with pytest.raises(RuntimeError, match="rate limited"):
            await _run_exec(proc, tmp_path)

    async def test_clean_exit_returns_parsed(self, tmp_path: Path):
        proc = _exec_proc(0, _ndjson(_agent_message("done")))
        out = await _run_exec(proc, tmp_path)
        assert out["body"] == "done"


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
        # The mapping is daemon-owned and workspace-bound, not agent-writable.
        mapping = codex_mod.codex_state.mapping_path(workspace, "our-session-1")
        record = json.loads(mapping.read_text())
        assert record["thread_id"] == "codex-thread-xyz"
        assert not (workspace / ".codex_sessions").exists()
        assert resp.body == "hello"

    async def test_killed_first_turn_still_delivers_reply_and_persists_thread(
        self, tmp_path: Path
    ):
        """The whole point of the non-zero-exit path, end to end: a turn that
        finished and then died reaches the caller as an ordinary Response, its
        thread survives for the next turn, and its tokens are still counted.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        proc = _exec_proc(
            -9,
            _ndjson(
                {"type": "thread.started", "thread_id": "codex-thread-killed"},
                _agent_message("PR #804 is merged."),
                _turn_completed(input_tokens=100, output_tokens=5),
            ),
            stderr=b"stale teardown noise",
        )
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.object(codex_mod.agent, "track_agent_process"),
            patch.object(codex_mod.agent, "_kill_process_tree", AsyncMock()),
        ):
            resp = await CodexBackend().run(
                workspace, "killed-session", "hi", "telegram"
            )

        assert resp.body == "PR #804 is merged."
        # turn.completed carries the usage, so gating on it also keeps the
        # fallback token count honest instead of billing the turn as free.
        assert resp.tokens_in == 100
        assert resp.tokens_out == 5
        mapping = codex_mod.codex_state.mapping_path(workspace, "killed-session")
        assert json.loads(mapping.read_text())["thread_id"] == "codex-thread-killed"

    async def test_second_call_resumes_persisted_thread(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mapping = _write_mapping(workspace, "our-session-1", "existing-thread")

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
        # Mapping unchanged (we do not overwrite on resume).
        assert json.loads(mapping.read_text())["thread_id"] == "existing-thread"

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
        # The codex binary is NOT repeated after `--`; first real arg is `exec`.
        assert cmd[7] == "exec"
        assert "codex" not in cmd[7:], "redundant codex binary in launcher cmd"

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

    async def test_effort_config_only_under_launcher(self, tmp_path, monkeypatch):
        """OLLAMA_EFFORT must not reach native argv: native inherits the
        operator's own model_reasoning_effort in ~/.codex/config.toml."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("OLLAMA_EFFORT", "high")
        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(workspace, "sess", "hi", "telegram")
        assert "-c" not in mock.call_args[0][1]

    async def test_effort_config_under_launcher(self, tmp_path, monkeypatch):
        """Ollama mode: effort is passed as a TOML-quoted -c override."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("OLLAMA_EFFORT", "high")
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
        assert 'model_reasoning_effort="high"' in cmd

    async def test_effort_omitted_without_setting(self, tmp_path, monkeypatch):
        """Unset OLLAMA_EFFORT → no -c override even under the launcher."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.delenv("OLLAMA_EFFORT", raising=False)
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")
        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )
        assert "-c" not in mock.call_args[0][1]

    async def test_effort_level_not_in_codex_set_skipped(
        self, tmp_path, monkeypatch, caplog
    ):
        """`max` is claude-only; codex must skip it rather than hand it to its
        config parse, which would fail the spawn."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("OLLAMA_EFFORT", "max")
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")
        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )
        assert "-c" not in mock.call_args[0][1]
        assert "ignoring unknown effort 'max'" in caplog.text

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

    async def test_jobs_platform_skips_handoff(self, tmp_path: Path):
        """A fresh scheduler fire must not inherit the prior fire's transcript."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.transcript.prepend_latest_handoff",
            ) as handoff,
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ),
        ):
            await CodexBackend().run(workspace, "sess", "hi", "jobs")
        handoff.assert_not_called()

    async def test_tokens_in_does_not_double_count_cached(self, tmp_path: Path):
        """OpenAI's `cached_input_tokens` is a subset of `input_tokens`, so
        do not sum them like we do for Anthropic's cache_read field."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(input_tokens=200, cached=300),
        ):
            resp = await CodexBackend().run(workspace, "sess", "hi", "telegram")

        # input_tokens already includes the cached portion
        assert resp.tokens_in == 200

    async def test_tokens_in_uses_session_file_delta(self, tmp_path: Path):
        """When session-file totals are available, report this exec's delta,
        not codex stdout's cumulative running total."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "sess-resume", "existing-thread")

        # Simulate: pre-exec total was 12000 in, 100 out.
        # Post-exec total is 26000 in, 250 out. This exec contributed 14000 / 150.
        pre = {
            "input_tokens": 12000,
            "output_tokens": 100,
            "reasoning_output_tokens": 0,
        }
        post = {
            "input_tokens": 26000,
            "output_tokens": 250,
            "reasoning_output_tokens": 0,
        }

        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.extract_codex_cumulative_tokens",
                side_effect=[pre, post],
            ),
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                # stdout reports cumulative (the bug we're fixing) — ignored when
                # session-file delta is available.
                return_value=_success_result(
                    thread_id="existing-thread", input_tokens=26000
                ),
            ),
        ):
            resp = await CodexBackend().run(
                workspace, "sess-resume", "next turn", "telegram"
            )

        assert resp.tokens_in == 14000
        assert resp.tokens_out == 150

    async def test_tokens_in_falls_back_to_stdout_when_no_session_data(
        self, tmp_path: Path
    ):
        """No prior turn (fresh thread) + post-exec session file unreachable:
        fall back to stdout usage. For a fresh thread, stdout = per-turn anyway."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.extract_codex_cumulative_tokens",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(input_tokens=5000, output_tokens=50),
            ),
        ):
            resp = await CodexBackend().run(workspace, "sess-fresh", "hi", "telegram")

        assert resp.tokens_in == 5000
        assert resp.tokens_out == 50 + 5  # output + reasoning default of 5

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

        # cached_input_tokens is a subset of input_tokens for codex (OpenAI
        # semantics), so only input_tokens contributes to tokens_in.
        assert resp.tokens_in == 100 + 200
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
# Cross-backend transcript handoff
# ---------------------------------------------------------------------------


class TestCodexBackendHandoff:
    async def test_fresh_thread_injects_claude_handoff(self, tmp_path: Path):
        """When no codex state exists but claude has prior turns, prepend them."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        prior_turns = [
            Turn("user", "earlier question"),
            Turn("assistant", "earlier answer"),
        ]
        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.find_latest_prior_transcript",
                return_value=(prior_turns, "claude"),
            ),
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await CodexBackend().run(
                workspace, "sess-handoff", "CURRENT_USER_TEXT", "telegram"
            )

        composed = mock.call_args[0][1][-1]
        assert "[Prior conversation via claude" in composed
        assert "earlier question" in composed
        assert "earlier answer" in composed
        # User's current prompt is still there, and follows the handoff.
        assert composed.endswith("CURRENT_USER_TEXT")
        # System prompt and handoff appear in the right order.
        assert composed.index("[Prior conversation via claude") < composed.index(
            "CURRENT_USER_TEXT"
        )

    async def test_existing_thread_skips_handoff(self, tmp_path: Path):
        """Don't re-forward history when we're resuming an existing codex thread.
        Also confirms the system prompt is NOT prepended on resume: codex
        already has it from the first turn's persisted history, and re-sending
        it bloats input tokens by ~4.7KB per turn for nothing."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "sess-resume", "existing-thread")

        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.find_latest_prior_transcript"
            ) as mock_lookup,
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await CodexBackend().run(
                workspace, "sess-resume", "USER_TEXT_ONLY", "telegram"
            )

        # The lookup must not even be called when resuming an existing thread.
        mock_lookup.assert_not_called()
        composed = mock.call_args[0][1][-1]
        assert "[Prior conversation via claude" not in composed
        # System-prompt content (any of the FORMAT_HINTS) must NOT be present
        # on resume — the composed prompt should be exactly the user's text.
        assert composed == "USER_TEXT_ONLY"
        assert "Memory System" not in composed
        assert "Format responses" not in composed

    async def test_no_claude_history_no_handoff(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.find_latest_prior_transcript",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            await CodexBackend().run(workspace, "sess-clean", "msg", "telegram")

        composed = mock.call_args[0][1][-1]
        assert "[Prior conversation" not in composed

    async def test_extractor_exception_falls_through_silently(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.find_latest_prior_transcript",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(),
            ) as mock,
        ):
            resp = await CodexBackend().run(workspace, "sess-broken", "msg", "telegram")

        # Backend must not blow up — user keeps getting a reply.
        assert resp.body == "hello"
        composed = mock.call_args[0][1][-1]
        assert "[Prior conversation" not in composed


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


class TestCodexBackendTakeoverCommand:
    def test_returns_resume_command_when_thread_mapping_exists(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        session_uuid = "deadbeef-1234"
        thread_id = "thread-abc-xyz"
        _write_mapping(workspace, session_uuid, thread_id)

        cmd = CodexBackend().takeover_command(workspace, session_uuid)
        assert cmd == f"codex resume {thread_id}"

    def test_returns_none_when_no_mapping_file(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert CodexBackend().takeover_command(workspace, "missing-uuid") is None

    def test_returns_none_when_mapping_file_empty(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        assert CodexBackend().takeover_command(workspace, "empty-uuid") is None


class TestCodexBackendSessionLogPath:
    def test_always_returns_none_for_now(self, tmp_path: Path) -> None:
        """Codex format isn't wired through the watch formatter — explicitly None."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert CodexBackend().session_log_path(workspace, "any-uuid") is None


class TestCodexSessionLogPath:
    """session_log_path maps our session_uuid -> codex thread id -> rollout."""

    def test_resolves_via_thread_mapping(self, codex_sessions_dir, tmp_path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        _write_mapping(ws, "our-uuid", "threadabc")
        rollout = codex_sessions_dir / "rollout-2026-06-06T10-00-00-threadabc.jsonl"
        rollout.write_text('{"id":"threadabc"}\n')

        assert CodexBackend().session_log_path(ws, "our-uuid") == rollout

    def test_none_without_mapping(self, codex_sessions_dir, tmp_path) -> None:
        assert CodexBackend().session_log_path(tmp_path / "ws", "missing") is None

    def test_none_when_mapping_empty(self, codex_sessions_dir, tmp_path) -> None:
        ws = tmp_path / "ws"
        assert CodexBackend().session_log_path(ws, "our-uuid") is None

    def test_resolves_live_by_cwd_before_mapping_written(
        self, codex_sessions_dir, tmp_path
    ) -> None:
        # First turn still running: no uuid->thread mapping yet, but codex is
        # writing a rollout that records the workspace cwd. Resolve via that so
        # the watch can tail live instead of waiting for the turn to finish.
        ws = tmp_path / "ws"
        day = codex_sessions_dir / "2026" / "06" / "06"
        day.mkdir(parents=True)
        rollout = day / "rollout-2026-06-06T10-00-00-threadlive.jsonl"
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": str(ws)}}) + "\n"
        )
        assert CodexBackend().session_log_path(ws, "uuid-no-mapping") == rollout

    def test_ignores_stale_rollout_for_cwd(self, codex_sessions_dir, tmp_path) -> None:
        # A rollout for this cwd but not recently written is a *past* session,
        # not the live one — don't resurface it as the live target.
        import os
        import time

        ws = tmp_path / "ws"
        day = codex_sessions_dir / "2026" / "06" / "06"
        day.mkdir(parents=True)
        rollout = day / "rollout-old-threadstale.jsonl"
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": str(ws)}}) + "\n"
        )
        old = time.time() - 3600
        os.utime(rollout, (old, old))
        assert CodexBackend().session_log_path(ws, "uuid-no-mapping") is None


# ---------------------------------------------------------------------------
# Custom-prompt expansion (codex exec doesn't expand /name itself)
# ---------------------------------------------------------------------------


class TestExpandCodexPrompt:
    def _prompt(self, tmp_path, name, body):
        prompts = tmp_path / "prompts"
        prompts.mkdir(exist_ok=True)
        (prompts / f"{name}.md").write_text(body)

    def test_non_slash_prompt_unchanged(self):
        from claude_on_the_fly.backends.codex import _expand_codex_prompt

        assert _expand_codex_prompt("just a message") == "just a message"

    def test_unknown_prompt_unchanged(self, tmp_path, monkeypatch):
        from claude_on_the_fly.backends.codex import _expand_codex_prompt

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        assert _expand_codex_prompt("/nope") == "/nope"

    @pytest.mark.parametrize("invocation", ["absolute", "traversal"])
    def test_prompt_name_cannot_escape_prompt_directory(
        self, tmp_path, monkeypatch, invocation
    ):
        from claude_on_the_fly.backends.codex import _expand_codex_prompt

        prompts = tmp_path / "prompts"
        prompts.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("HOST_SECRET")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))

        if invocation == "absolute":
            prompt = "/" + str(outside.with_suffix(""))
        else:
            prompt = "/prompts/../outside"

        assert _expand_codex_prompt(prompt) == prompt

    def test_expands_body_strips_frontmatter_and_named_args(
        self, tmp_path, monkeypatch
    ):
        from claude_on_the_fly.backends.codex import _expand_codex_prompt

        self._prompt(
            tmp_path,
            "draftpr",
            "---\ndescription: draft a PR\n---\nPR titled $PR_TITLE for $ARGUMENTS",
        )
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        out = _expand_codex_prompt('/draftpr FILES="a b" PR_TITLE="Add hero"')
        assert out == 'PR titled Add hero for FILES="a b" PR_TITLE="Add hero"'

    def test_positional_args_and_escape(self, tmp_path, monkeypatch):
        from claude_on_the_fly.backends.codex import _expand_codex_prompt

        self._prompt(tmp_path, "greet", "Hi $1, you owe $$5. Rest: $2")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        assert (
            _expand_codex_prompt("/greet alice bob")
            == "Hi alice, you owe $5. Rest: bob"
        )

    def test_namespaced_invocation(self, tmp_path, monkeypatch):
        from claude_on_the_fly.backends.codex import _expand_codex_prompt

        self._prompt(tmp_path, "x", "BODY")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        assert _expand_codex_prompt("/prompts:x") == "BODY"

    def test_missing_placeholders_become_empty(self, tmp_path, monkeypatch):
        from claude_on_the_fly.backends.codex import _expand_codex_prompt

        self._prompt(tmp_path, "p", "[$3][$NOPE]")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        assert _expand_codex_prompt("/p only") == "[][]"


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestCodexCompact:
    """Codex has no compaction *command* we can send: `thread/compact/start` is
    app-server only, and `/compact` typed as a prompt is acknowledged
    ("Context compacted.") while changing nothing — measured 45,730 → 46,357.
    What works is forcing codex's own pre-turn threshold via `-c`.
    """

    def _wire_thread(self, workspace: Path, session_uuid: str, thread_id: str) -> None:
        _write_mapping(workspace, session_uuid, thread_id)

    async def test_no_thread_yet_is_not_reported_as_unsupported(self, tmp_path):
        outcome = await codex_mod.CodexBackend().compact(tmp_path, "sid")
        assert outcome is not None, "None would claim codex cannot compact at all"
        assert outcome.ok is False
        assert "no session" in outcome.error

    async def test_forces_the_threshold_via_a_per_invocation_override(self, tmp_path):
        """Never writes to the user's ~/.codex/config.toml — the override is
        scoped to this one run."""
        self._wire_thread(tmp_path, "sid", "thread-1")
        with (
            patch.object(
                codex_mod.transcript,
                "extract_codex_prompt_tokens",
                side_effect=[(45_000, 258_400), (18_000, 258_400)],
            ),
            patch.object(
                codex_mod, "_run_codex_exec", new_callable=AsyncMock, return_value={}
            ) as run_exec,
        ):
            outcome = await codex_mod.CodexBackend().compact(tmp_path, "sid")

        argv = run_exec.await_args[0][1]
        assert "-c" in argv
        assert f"model_auto_compact_token_limit={codex_mod.COMPACT_TOKEN_LIMIT}" in argv
        assert argv[-3:-1] == ["resume", "thread-1"]
        assert outcome is not None and outcome.ok is True
        assert (outcome.pre_tokens, outcome.post_tokens) == (45_000, 18_000)

    async def test_never_sends_slash_compact(self, tmp_path):
        """It would be accepted and do nothing, which is the one outcome worse
        than an error."""
        self._wire_thread(tmp_path, "sid", "thread-1")
        with (
            patch.object(
                codex_mod.transcript,
                "extract_codex_prompt_tokens",
                side_effect=[(45_000, 258_400), (18_000, 258_400)],
            ),
            patch.object(
                codex_mod, "_run_codex_exec", new_callable=AsyncMock, return_value={}
            ) as run_exec,
        ):
            await codex_mod.CodexBackend().compact(tmp_path, "sid")

        assert "/compact" not in run_exec.await_args[0][1]

    async def test_a_context_that_did_not_shrink_is_not_success(self, tmp_path):
        """The only evidence available is the token count — codex publishes no
        in-band compaction signal, so trusting the trigger would repeat the exact
        lie `/compact` tells."""
        self._wire_thread(tmp_path, "sid", "thread-1")
        with (
            patch.object(
                codex_mod.transcript,
                "extract_codex_prompt_tokens",
                side_effect=[(45_000, 258_400), (45_100, 258_400)],
            ),
            patch.object(
                codex_mod, "_run_codex_exec", new_callable=AsyncMock, return_value={}
            ),
        ):
            outcome = await codex_mod.CodexBackend().compact(tmp_path, "sid")

        assert outcome is not None and outcome.ok is False
        assert "nothing to compact" in outcome.error

    async def test_unreadable_usage_is_reported_not_guessed(self, tmp_path):
        self._wire_thread(tmp_path, "sid", "thread-1")
        with (
            patch.object(
                codex_mod.transcript, "extract_codex_prompt_tokens", return_value=None
            ),
            patch.object(
                codex_mod, "_run_codex_exec", new_callable=AsyncMock, return_value={}
            ),
        ):
            outcome = await codex_mod.CodexBackend().compact(tmp_path, "sid")

        assert outcome is not None and outcome.ok is False
        assert "token usage" in outcome.error


# ---------------------------------------------------------------------------
# The exec wrapper
# ---------------------------------------------------------------------------


class TestCodexExec:
    """Everything the CLI can do wrong has to become a RuntimeError carrying the
    most specific detail available, because that string is what the user reads."""

    def _proc(self, stdout: bytes, stderr: bytes = b"", rc: int = 0):
        from unittest.mock import MagicMock

        proc = MagicMock()
        proc.returncode = rc
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        return proc

    async def _run(self, proc, **kwargs):
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(codex_mod.agent, "_kill_process_tree", new_callable=AsyncMock),
        ):
            return await codex_mod._run_codex_exec(
                Path("/tmp"), ["codex"], kwargs.get("timeout")
            )

    async def test_a_clean_run_returns_the_parsed_stream(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": "t-1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
        ]
        stdout = "\n".join(json.dumps(e) for e in events).encode()
        parsed = await self._run(self._proc(stdout))
        assert parsed["thread_id"] == "t-1"
        assert parsed["body"] == "hi"

    async def test_a_timeout_names_the_limit(self) -> None:
        from unittest.mock import MagicMock

        proc = MagicMock()
        proc.returncode = None

        async def never(*_args, **_kwargs):
            import asyncio

            await asyncio.Event().wait()

        proc.communicate = never
        with pytest.raises(RuntimeError, match=r"timed out after 0\.01s"):
            await self._run(proc, timeout=0.01)

    async def test_cancellation_reaps_the_process_group(self) -> None:
        """Frontends cancel a live turn to implement $stop, and codex spawns tool
        subprocesses that must die with it rather than orphaning."""
        import asyncio
        from unittest.mock import MagicMock

        started = asyncio.Event()

        async def never_finishes():
            started.set()
            await asyncio.Event().wait()

        proc = MagicMock()
        proc.returncode = None
        proc.communicate = never_finishes

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(
                codex_mod.agent, "_kill_process_tree", new_callable=AsyncMock
            ) as kill,
        ):
            task = asyncio.create_task(
                codex_mod._run_codex_exec(Path("/tmp"), ["codex"], None)
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        kill.assert_awaited_once_with(proc)

    async def test_a_turn_failure_in_the_stream_wins_over_the_exit_code(self) -> None:
        """codex reports the real reason in `turn.failed`; the exit code is just a
        number, so the stream's message is the better error."""
        stdout = json.dumps(
            {"type": "turn.failed", "error": {"message": "model refused"}}
        ).encode()
        with pytest.raises(RuntimeError, match="model refused"):
            await self._run(self._proc(stdout, b"exit 1 noise", rc=1))

    async def test_stderr_is_used_when_the_stream_says_nothing(self) -> None:
        with pytest.raises(RuntimeError, match="command not found"):
            await self._run(self._proc(b"", b"command not found: codex", rc=127))

    async def test_a_bare_exit_code_is_reported_when_nothing_else_is_available(
        self,
    ) -> None:
        with pytest.raises(RuntimeError, match="Exit code 3"):
            await self._run(self._proc(b"", b"", rc=3))

    async def test_a_turn_failure_on_a_zero_exit_still_raises(self) -> None:
        """codex can exit 0 having failed the turn, so the stream has to be checked
        even on a clean exit or the user gets an empty reply and no reason."""
        stdout = json.dumps(
            {"type": "turn.failed", "error": {"message": "context overflow"}}
        ).encode()
        with pytest.raises(RuntimeError, match="context overflow"):
            await self._run(self._proc(stdout, rc=0))


# ---------------------------------------------------------------------------
# Prompt expansion edge cases
# ---------------------------------------------------------------------------


class TestExpandCodexPromptFailures:
    def test_a_symlinked_prompt_pointing_outside_is_not_expanded(
        self, tmp_path, monkeypatch
    ):
        """The name has no slash and the file exists, so only comparing the
        *resolved* parent catches it. A link is how you escape a directory
        without a traversal sequence in the name."""
        from claude_on_the_fly.backends.codex import _expand_codex_prompt

        prompts = tmp_path / "prompts"
        prompts.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("HOST_SECRET")
        (prompts / "sneaky.md").symlink_to(outside)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))

        assert _expand_codex_prompt("/sneaky") == "/sneaky"

    def test_an_unresolvable_prompt_path_is_left_alone(
        self, tmp_path, monkeypatch, caplog
    ):
        """The traversal guard resolves both sides to compare them. If that
        resolution itself fails, the name cannot be proven to stay inside the
        prompts directory, so the text passes through unexpanded."""
        from claude_on_the_fly.backends import codex as codex_module

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))

        def boom(self, *args, **kwargs):
            raise OSError("too many levels of symbolic links")

        monkeypatch.setattr(codex_module.Path, "resolve", boom)
        with caplog.at_level("WARNING"):
            assert codex_module._expand_codex_prompt("/review") == "/review"
        assert "cannot resolve prompt review" in caplog.text

    def test_a_template_that_cannot_be_read_leaves_the_prompt_alone(
        self, tmp_path, monkeypatch, caplog
    ):
        """Better to send the user's literal text than to swallow the turn."""
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        template = prompts / "review.md"
        template.write_text("---\n---\nReview $ARGUMENTS")

        def read_fails(self, *_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", read_fails)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.backends.codex"):
            assert codex_mod._expand_codex_prompt("/review diff") == "/review diff"
        assert "cannot read" in "\n".join(r.getMessage() for r in caplog.records)

    def test_a_bare_slash_is_not_a_command(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        assert codex_mod._expand_codex_prompt("/ something") == "/ something"

    def test_unbalanced_quotes_fall_back_to_a_plain_split(self, tmp_path, monkeypatch):
        """shlex raises on an unterminated quote, and a user typing one mid-sentence
        should not lose the turn over it."""
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "ask.md").write_text("Q: $1")
        assert codex_mod._expand_codex_prompt("/ask what's \"up") == "Q: what's"


# ---------------------------------------------------------------------------
# Prompt listing failures
# ---------------------------------------------------------------------------


class TestCodexListSkillsFailures:
    async def test_an_unreadable_prompts_dir_yields_nothing(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        (tmp_path / "prompts").mkdir()

        def glob_fails(self, _pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "glob", glob_fails)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.backends.codex"):
            assert await CodexBackend().list_skills() == []
        assert "cannot read" in "\n".join(r.getMessage() for r in caplog.records)

    async def test_a_prompt_file_that_cannot_be_read_keeps_its_name(
        self, tmp_path, monkeypatch
    ):
        """The name is what the picker needs; the description is a nicety."""
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "review.md").write_text("---\ndescription: Review it\n---\n")
        real_read = Path.read_text

        def read_fails(self, *args, **kwargs):
            if self.name == "review.md":
                raise OSError("permission denied")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_fails)
        assert await CodexBackend().list_skills() == [("review", "")]


def test_blank_lines_in_the_stream_are_skipped():
    """codex's JSONL has trailing and interleaved blank lines; treating one as a
    parse failure would log a warning per turn."""
    stdout = (
        b'\n{"type": "thread.started", "thread_id": "t-1"}\n\n   \n'
        b'{"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}\n\n'
    )
    parsed = parse_codex_stream(stdout)
    assert parsed["thread_id"] == "t-1"
    assert parsed["body"] == "hi"
    assert parsed["error"] is None


class TestCodexContextReading:
    """The absolutes the auto-compact gate thresholds on. Codex reports them in the
    rollout rather than on stdout, so the reading is a second lookup and can be
    absent."""

    async def test_a_rollout_reading_reaches_the_response(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with (
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(thread_id="t-1"),
            ),
            patch.object(
                codex_mod.transcript,
                "extract_codex_prompt_tokens",
                return_value=(650_000, 1_000_000),
            ),
        ):
            resp = await CodexBackend().run(workspace, "s-1", "hi", "telegram")
        assert resp.context_tokens == 650_000
        assert resp.context_window_size == 1_000_000

    async def test_no_rollout_yet_leaves_the_reading_unset(self, tmp_path: Path):
        """None reads downstream as "no reading" rather than as an empty context, so
        a first turn cannot make a large session look small."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with (
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(thread_id="t-1"),
            ),
            patch.object(
                codex_mod.transcript, "extract_codex_prompt_tokens", return_value=None
            ),
        ):
            resp = await CodexBackend().run(workspace, "s-1", "hi", "telegram")
        assert resp.context_tokens is None
        assert resp.context_window_size is None

    async def test_a_window_of_zero_is_not_a_reading(self, tmp_path: Path):
        """Dividing by it would raise, and reporting 0 would read as a full window."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with (
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(thread_id="t-1"),
            ),
            patch.object(
                codex_mod.transcript,
                "extract_codex_prompt_tokens",
                return_value=(100, 0),
            ),
        ):
            resp = await CodexBackend().run(workspace, "s-1", "hi", "telegram")
        assert resp.context_tokens is None


def test_an_error_item_is_not_counted_as_a_tool():
    """Permissions mode `ask` passes --dangerously-bypass-hook-trust, which codex
    announces as an error item. Counting it produced a phantom tool named "error"
    in the response footer of every gated codex turn."""
    from claude_on_the_fly.backends.codex import parse_codex_stream

    stream = b"\n".join(
        [
            b'{"type":"item.completed","item":{"id":"i1","type":"error",'
            b'"message":"`--dangerously-bypass-hook-trust` is enabled."}}',
            b'{"type":"item.completed","item":{"id":"i2",'
            b'"type":"command_execution","command":"ls"}}',
            b'{"type":"item.completed","item":{"id":"i3","type":"reasoning"}}',
        ]
    )
    assert parse_codex_stream(stream)["tool_counts"] == {"command_execution": 1}
