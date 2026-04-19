"""Tests for claude_on_the_fly.agent module."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.agent import (
    FORMAT_HINTS,
    Response,
    build_system_prompt,
    _exec,
    parse_stream,
    run,
    stats_mode,
)


def _ndjson(*messages: dict) -> bytes:
    return b"\n".join(json.dumps(m).encode() for m in messages)


def _result_line(**overrides) -> dict:
    base = {
        "type": "result",
        "subtype": "success",
        "result": "hello",
        "is_error": False,
    }
    base.update(overrides)
    return base


def _assistant_line(*content_blocks: dict) -> dict:
    return {
        "type": "assistant",
        "message": {"id": "msg_x", "content": list(content_blocks)},
    }


def _tool_use(name: str, **input_fields) -> dict:
    return {
        "type": "tool_use",
        "id": f"toolu_{name}",
        "name": name,
        "input": input_fields,
    }


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------


class TestResponseHasStats:
    def test_true_when_cost_set(self):
        r = Response(body="hi", cost=0.01)
        assert r.has_stats is True

    def test_true_when_model_set(self):
        r = Response(body="hi", model="claude-sonnet")
        assert r.has_stats is True

    def test_true_when_both_set(self):
        r = Response(body="hi", cost=0.5, model="claude-sonnet")
        assert r.has_stats is True

    def test_false_when_neither(self):
        r = Response(body="hi")
        assert r.has_stats is False

    def test_false_when_zero_cost_empty_model(self):
        r = Response(body="hi", cost=0, model="")
        assert r.has_stats is False


class TestResponseFormatStats:
    def test_cost_only(self):
        r = Response(body="hi", cost=0.0123)
        assert r.format_stats() == "$0.0123"

    def test_duration_only(self):
        r = Response(body="hi", duration=3.456)
        assert r.format_stats() == "3.5s"

    def test_tokens_only_input(self):
        r = Response(body="hi", tokens_in=100)
        assert r.format_stats() == "↑100 ↓0"

    def test_tokens_only_output(self):
        r = Response(body="hi", tokens_out=200)
        assert r.format_stats() == "↑0 ↓200"

    def test_model_only(self):
        r = Response(body="hi", model="opus")
        assert r.format_stats() == "opus"

    def test_all_fields(self):
        r = Response(
            body="hi",
            cost=0.05,
            duration=12.34,
            tokens_in=500,
            tokens_out=300,
            model="sonnet",
        )
        assert r.format_stats() == "$0.0500 | 12.3s | ↑500 ↓300 | sonnet"

    def test_no_fields(self):
        r = Response(body="hi")
        assert r.format_stats() == ""

    def test_cost_and_model(self):
        r = Response(body="hi", cost=0.001, model="haiku")
        assert r.format_stats() == "$0.0010 | haiku"

    def test_zero_tokens_excluded(self):
        """tokens_in=0 and tokens_out=0 should not produce a token part."""
        r = Response(body="hi", tokens_in=0, tokens_out=0)
        assert r.format_stats() == ""

    def test_tool_counts_not_included_in_stats(self):
        """format_stats stays single-line; tools render via format_tools."""
        r = Response(
            body="hi",
            cost=0.01,
            model="sonnet",
            tool_counts={"Read": 5},
        )
        assert r.format_stats() == "$0.0100 | sonnet"


class TestResponseHasTools:
    def test_true_when_tool_counts_populated(self):
        assert Response(body="hi", tool_counts={"Read": 1}).has_tools is True

    def test_false_when_empty(self):
        assert Response(body="hi").has_tools is False

    def test_false_for_empty_dict(self):
        assert Response(body="hi", tool_counts={}).has_tools is False


class TestStatsMode:
    def test_default_is_summary(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_STATS_MODE", raising=False)
        assert stats_mode("telegram") == "summary"

    def test_reads_platform_specific_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_STATS_MODE", "detailed")
        monkeypatch.setenv("TELEGRAM_STATS_MODE", "off")
        assert stats_mode("slack") == "detailed"
        assert stats_mode("telegram") == "off"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("GMAIL_STATS_MODE", "DETAILED")
        assert stats_mode("gmail") == "detailed"

    def test_invalid_value_falls_back_to_summary(self, monkeypatch):
        monkeypatch.setenv("SLACK_STATS_MODE", "bogus")
        assert stats_mode("slack") == "summary"

    def test_all_three_modes_accepted(self, monkeypatch):
        for mode in ("off", "summary", "detailed"):
            monkeypatch.setenv("TELEGRAM_STATS_MODE", mode)
            assert stats_mode("telegram") == mode


class TestResponseFormatTools:
    def test_empty_tool_counts_returns_empty(self):
        assert Response(body="hi").format_tools() == ""

    def test_single_tool(self):
        r = Response(body="hi", tool_counts={"Read": 3})
        assert r.format_tools() == "🔧 3 (Read×3)"

    def test_shows_total_and_full_breakdown(self):
        r = Response(
            body="hi",
            tool_counts={"Read": 12, "Bash": 8, "Grep": 6, "Edit": 3, "Write": 2},
        )
        assert r.format_tools() == "🔧 31 (Read×12 Bash×8 Grep×6 Edit×3 Write×2)"

    def test_fewer_than_three_tools(self):
        r = Response(body="hi", tool_counts={"Read": 2, "Bash": 1})
        assert r.format_tools() == "🔧 3 (Read×2 Bash×1)"

    def test_tie_broken_alphabetical(self):
        r = Response(
            body="hi", tool_counts={"Write": 2, "Bash": 2, "Read": 2, "Edit": 2}
        )
        assert r.format_tools() == "🔧 8 (Bash×2 Edit×2 Read×2 Write×2)"

    def test_skill_counts_ignored_in_new_format(self):
        """Skill sub-breakdown dropped for compactness; Skill count still shown."""
        r = Response(
            body="hi",
            tool_counts={"Read": 5, "Skill": 2},
            skill_counts={"cq": 1, "simplify": 1},
        )
        assert r.format_tools() == "🔧 7 (Read×5 Skill×2)"


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_telegram_platform(self):
        result = build_system_prompt("telegram", "hoss", "dm")
        assert FORMAT_HINTS["telegram"] in result
        assert "hoss" in result
        assert "dm" in result

    def test_slack_platform(self):
        result = build_system_prompt("slack", "alice", "#general")
        assert FORMAT_HINTS["slack"] in result
        assert "alice" in result
        assert "#general" in result

    def test_gmail_platform(self):
        result = build_system_prompt("gmail", "bob", "thread")
        assert FORMAT_HINTS["gmail"] in result

    def test_unknown_platform_falls_back_to_telegram(self):
        result = build_system_prompt("discord", "charlie", "dm")
        assert FORMAT_HINTS["telegram"] in result
        # Slack and gmail hints should NOT be present
        assert FORMAT_HINTS["slack"] not in result
        assert FORMAT_HINTS["gmail"] not in result

    def test_all_template_variables_substituted(self):
        result = build_system_prompt("telegram", "hoss", "channel:dev")
        # No leftover {placeholders}
        assert "{format_hint}" not in result
        assert "{user_name}" not in result
        assert "{channel_context}" not in result
        assert "{memory_root}" not in result
        assert "{knowledge_dir}" not in result


# ---------------------------------------------------------------------------
# _exec
# ---------------------------------------------------------------------------


class _AsyncLineIter:
    """Async iterator over newline-terminated byte lines, mimicking StreamReader."""

    def __init__(self, data: bytes) -> None:
        self._lines: deque[bytes] = (
            deque(line + b"\n" for line in data.split(b"\n") if line)
            if data
            else deque()
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.popleft()


def _make_proc(returncode: int, stdout: bytes, stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = _AsyncLineIter(stdout)
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr)
    proc.wait = AsyncMock(return_value=returncode)
    return proc


class TestExec:
    async def test_success_returns_parsed_stream(self):
        stream = _ndjson(
            {"type": "system", "subtype": "init"},
            _result_line(result="hello"),
        )
        proc = _make_proc(0, stream)

        with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await _exec(Path("/tmp"), ["claude", "-p", "hi"])

        assert result["result"] == "hello"
        assert result["is_error"] is False
        assert result["tool_counts"] == {}
        assert result["skill_counts"] == {}
        mock_exec.assert_awaited_once()

    async def test_success_aggregates_tool_counts(self):
        stream = _ndjson(
            _assistant_line(_tool_use("Read"), _tool_use("Bash")),
            _assistant_line(_tool_use("Read")),
            _result_line(),
        )
        proc = _make_proc(0, stream)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await _exec(Path("/tmp"), ["claude", "-p", "hi"])

        assert result["tool_counts"] == {"Read": 2, "Bash": 1}

    async def test_nonzero_exit_raises_with_stderr(self):
        proc = _make_proc(1, b"", stderr=b"something broke")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="something broke"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_nonzero_exit_empty_stderr_uses_fallback(self):
        proc = _make_proc(42, b"", stderr=b"")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="Exit code 42"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_is_error_true_raises(self):
        stream = _ndjson(
            _result_line(is_error=True, result="bad stuff", subtype="tool_error")
        )
        proc = _make_proc(0, stream)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="bad stuff"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_error_subtype_raises(self):
        stream = _ndjson(
            _result_line(is_error=False, subtype="error_max_turns", result="too many")
        )
        proc = _make_proc(0, stream)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="too many"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_is_error_missing_result_defaults(self):
        # Result line with is_error but no result field
        stream = _ndjson({"type": "result", "is_error": True})
        proc = _make_proc(0, stream)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="Unknown error"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_nonzero_exit_with_stream_result_extracts_result(self):
        stream = _ndjson(
            _result_line(
                is_error=True,
                result="API Error: Could not process image",
                subtype="success",
            )
        )
        proc = _make_proc(1, stream, stderr=b"")

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(
                RuntimeError, match="API Error: Could not process image"
            ):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"])


# ---------------------------------------------------------------------------
# parse_stream
# ---------------------------------------------------------------------------


class TestParseStream:
    def test_empty_stdout_returns_empty_dict(self):
        assert parse_stream(b"") == {}

    def test_only_system_lines_no_result_returns_empty(self):
        stream = _ndjson({"type": "system", "subtype": "init"})
        assert parse_stream(stream) == {}

    def test_result_line_passthrough_with_empty_counts(self):
        stream = _ndjson(_result_line(result="done", total_cost_usd=0.1))
        out = parse_stream(stream)
        assert out["result"] == "done"
        assert out["total_cost_usd"] == 0.1
        assert out["tool_counts"] == {}
        assert out["skill_counts"] == {}

    def test_tool_counts_across_multiple_assistant_messages(self):
        stream = _ndjson(
            _assistant_line(_tool_use("Read"), _tool_use("Read"), _tool_use("Bash")),
            {"type": "user", "message": {"content": []}},
            _assistant_line(_tool_use("Read")),
            _result_line(),
        )
        out = parse_stream(stream)
        assert out["tool_counts"] == {"Read": 3, "Bash": 1}

    def test_skill_tool_populates_skill_counts(self):
        stream = _ndjson(
            _assistant_line(
                _tool_use("Skill", skill="cq"),
                _tool_use("Skill", skill="simplify"),
                _tool_use("Skill", skill="cq"),
            ),
            _result_line(),
        )
        out = parse_stream(stream)
        assert out["tool_counts"] == {"Skill": 3}
        assert out["skill_counts"] == {"cq": 2, "simplify": 1}

    def test_skill_tool_without_skill_field_counted_only_as_tool(self):
        stream = _ndjson(
            _assistant_line(_tool_use("Skill")),  # no skill in input
            _result_line(),
        )
        out = parse_stream(stream)
        assert out["tool_counts"] == {"Skill": 1}
        assert out["skill_counts"] == {}

    def test_text_blocks_ignored(self):
        stream = _ndjson(
            _assistant_line(
                {"type": "text", "text": "thinking..."},
                _tool_use("Read"),
            ),
            _result_line(),
        )
        out = parse_stream(stream)
        assert out["tool_counts"] == {"Read": 1}

    def test_malformed_line_is_skipped(self):
        stream = (
            json.dumps(_assistant_line(_tool_use("Read"))).encode()
            + b"\nnot-json-garbage\n"
            + json.dumps(_result_line(result="ok")).encode()
        )
        out = parse_stream(stream)
        assert out["result"] == "ok"
        assert out["tool_counts"] == {"Read": 1}

    def test_blank_lines_skipped(self):
        stream = b"\n\n" + json.dumps(_result_line(result="ok")).encode() + b"\n\n"
        assert parse_stream(stream)["result"] == "ok"

    def test_tool_use_without_name_falls_back_to_unknown(self):
        stream = _ndjson(
            _assistant_line({"type": "tool_use", "id": "t", "input": {}}),
            _result_line(),
        )
        out = parse_stream(stream)
        assert out["tool_counts"] == {"unknown": 1}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _cli_output(
    result: str = "response text",
    cost: float = 0.05,
    duration_ms: int = 3000,
    input_tokens: int = 100,
    cache_read: int = 50,
    output_tokens: int = 200,
    model: str = "claude-sonnet-4-20250514",
) -> dict:
    return {
        "result": result,
        "total_cost_usd": cost,
        "duration_ms": duration_ms,
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output_tokens,
        },
        "modelUsage": {model: {"input_tokens": input_tokens}} if model else {},
    }


class TestRun:
    async def test_happy_path_resume(self):
        output = _cli_output()

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ):
            resp = await run(Path("/tmp"), "sess-1", "hello", "telegram", "hoss")

        assert resp.body == "response text"
        assert resp.cost == 0.05
        assert resp.duration == 3.0
        assert resp.tokens_in == 150  # 100 + 50 cache_read
        assert resp.tokens_out == 200
        assert resp.model == "claude-sonnet-4-20250514"

    async def test_session_not_found_falls_back_to_new(self):
        output = _cli_output(result="new session reply")

        async def side_effect(workspace, cmd):
            if "--resume" in cmd:
                raise RuntimeError("No conversation found with id sess-1")
            return output

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            side_effect=side_effect,
        ) as mock:
            resp = await run(Path("/tmp"), "sess-1", "hello", "slack", "alice")

        assert resp.body == "new session reply"
        # Called twice: resume attempt + new session
        assert mock.await_count == 2

    async def test_other_runtime_error_reraised(self):
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            side_effect=RuntimeError("something completely different"),
        ):
            with pytest.raises(RuntimeError, match="something completely different"):
                await run(Path("/tmp"), "sess-1", "hello", "telegram")

    async def test_duration_converts_ms_to_seconds(self):
        output = _cli_output(duration_ms=12500)

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.duration == 12.5

    async def test_token_calculation_includes_cache_read(self):
        output = _cli_output(input_tokens=400, cache_read=600, output_tokens=50)

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.tokens_in == 1000
        assert resp.tokens_out == 50

    async def test_model_extracts_first_key(self):
        output = _cli_output(model="claude-opus-4-20250514")

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.model == "claude-opus-4-20250514"

    async def test_empty_model_usage(self):
        output = _cli_output()
        output["modelUsage"] = {}

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.model == ""

    async def test_missing_fields_default_gracefully(self):
        """CLI output with minimal fields should not blow up."""
        minimal_output = {"result": "bare response"}

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=minimal_output,
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "gmail")

        assert resp.body == "bare response"
        assert resp.cost == 0
        assert resp.duration == 0.0
        assert resp.tokens_in == 0
        assert resp.tokens_out == 0
        assert resp.model == ""

    async def test_missing_result_defaults_to_no_response(self):
        output = {"total_cost_usd": 0.01}

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.body == "No response"

    async def test_tool_and_skill_counts_propagate(self):
        output = _cli_output()
        output["tool_counts"] = {"Read": 2, "Skill": 1}
        output["skill_counts"] = {"cq": 1}

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.tool_counts == {"Read": 2, "Skill": 1}
        assert resp.skill_counts == {"cq": 1}

    async def test_tool_counts_default_empty_when_missing(self):
        output = _cli_output()

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.tool_counts == {}
        assert resp.skill_counts == {}

    async def test_cmd_uses_stream_json_verbose(self):
        output = _cli_output()
        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ) as mock:
            await run(Path("/tmp"), "sess-1", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert "stream-json" in cmd
        assert "--verbose" in cmd
        assert "json" not in [
            cmd[i + 1] for i, v in enumerate(cmd[:-1]) if v == "--output-format"
        ]

    async def test_resume_cmd_contains_session_uuid(self):
        output = _cli_output()

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ) as mock:
            await run(Path("/tmp"), "my-uuid", "hi", "telegram", "hoss", "channel:dev")

        cmd = mock.call_args[0][1]
        assert "--resume" in cmd
        assert "my-uuid" in cmd
        assert "hi" in cmd
