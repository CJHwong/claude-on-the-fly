"""Tests for claude_on_the_fly.agent module."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.agent import (
    FORMAT_HINTS,
    NUDGE_PROMPT,
    ClaudeUnavailableError,
    OllamaLauncher,
    Response,
    _classify,
    build_system_prompt,
    ensure_persona,
    _exec,
    _merge_cli_output,
    get_backend,
    parse_stream,
    run,
    stats_mode,
)
from claude_on_the_fly.backends.claude import ClaudeBackend
from claude_on_the_fly.transcript import Turn


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

    async def test_usage_limit_raises_unavailable(self):
        stream = _ndjson(
            _result_line(result="You've hit your org's monthly usage limit")
        )
        proc = _make_proc(1, stream)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(ClaudeUnavailableError, match="usage limit"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_usage_allocation_disabled_raises_unavailable(self):
        stream = _ndjson(
            _result_line(result="Your usage allocation has been disabled by your admin")
        )
        proc = _make_proc(1, stream)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(ClaudeUnavailableError, match="allocation"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_timeout_kills_proc_and_raises(self):
        # Stdout that never ends — will block the consumer.
        class _NeverEnds:
            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(10)
                raise StopAsyncIteration  # pragma: no cover

        proc = MagicMock()
        proc.returncode = None
        proc.stdout = _NeverEnds()
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.kill = MagicMock(side_effect=lambda: setattr(proc, "returncode", -9))
        proc.wait = AsyncMock(return_value=-9)

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="timed out after 0.1s"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"], timeout=0.1)

        proc.kill.assert_called_once()
        proc.wait.assert_awaited()

    async def test_whitespace_only_lines_skipped(self) -> None:
        """Lines that strip to empty bytes are skipped without incrementing line_count."""
        stream = _ndjson(_result_line(result="ok")) + b"\n    \n"
        proc = _make_proc(0, stream)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await _exec(Path("/tmp"), ["claude", "-p", "hi"])
        assert result["result"] == "ok"
        assert result["is_error"] is False

    async def test_malformed_json_line_skipped(self) -> None:
        """Non-JSON lines are logged and skipped; valid lines still processed."""
        # Each message gets a trailing \n so _AsyncLineIter splits correctly
        raw = (
            _ndjson(_assistant_line(_tool_use("Read"))) + b"\n"
            b"not-json\n" + _ndjson(_result_line(result="final")) + b"\n"
        )
        proc = _make_proc(0, raw)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await _exec(Path("/tmp"), ["claude", "-p", "hi"])
        assert result["result"] == "final"
        assert result["tool_counts"] == {"Read": 1}

    async def test_stderr_read_exception_handled_gracefully(self) -> None:
        """When stderr.read raises, the exception is swallowed and stdout still works."""
        stream = _ndjson(_result_line(result="good"))
        proc = _make_proc(0, stream)
        proc.stderr.read = AsyncMock(side_effect=Exception("stderr pipe broken"))
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await _exec(Path("/tmp"), ["claude", "-p", "hi"])
        assert result["result"] == "good"

    async def test_timeout_kill_raises_process_lookup_error(self) -> None:
        """kill() raises ProcessLookupError — swallowed, timeout still propagates."""
        proc = _never_ending_proc()
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="timed out after 0.1s"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"], timeout=0.1)
        proc.kill.assert_called_once()

    async def test_timeout_kill_raises_generic_exception(self) -> None:
        """kill() raises a generic Exception — logged, timeout still propagates."""
        proc = _never_ending_proc()
        proc.kill = MagicMock(side_effect=Exception("OS kill failed"))
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="timed out after 0.1s"):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"], timeout=0.1)
        proc.kill.assert_called_once()


def _never_ending_proc() -> MagicMock:
    class _NeverEnds:
        def __aiter__(self) -> _NeverEnds:
            return self

        async def __anext__(self) -> bytes:
            await asyncio.sleep(10)
            raise StopAsyncIteration  # pragma: no cover

    proc = MagicMock()
    proc.returncode = None
    proc.stdout = _NeverEnds()
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=-9)
    return proc


class TestClassify:
    def test_usage_limit_lowercased(self):
        assert isinstance(
            _classify("You've hit your org's monthly Usage Limit"),
            ClaudeUnavailableError,
        )

    def test_usage_allocation_disabled(self):
        assert isinstance(
            _classify("Your Usage Allocation has been disabled"),
            ClaudeUnavailableError,
        )

    def test_unrelated_error_stays_runtime(self):
        err = _classify("API Error: Could not process image")
        assert isinstance(err, RuntimeError)
        assert not isinstance(err, ClaudeUnavailableError)

    def test_empty_message(self):
        err = _classify("")
        assert isinstance(err, RuntimeError)
        assert not isinstance(err, ClaudeUnavailableError)


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
# _merge_cli_output
# ---------------------------------------------------------------------------


class TestMergeCliOutput:
    def test_second_body_wins(self):
        a = {"result": "", "total_cost_usd": 0.01}
        b = {"result": "done", "total_cost_usd": 0.02}
        assert _merge_cli_output(a, b)["result"] == "done"

    def test_cost_and_duration_summed(self):
        a = {"total_cost_usd": 0.10, "duration_ms": 1500}
        b = {"total_cost_usd": 0.25, "duration_ms": 2500}
        merged = _merge_cli_output(a, b)
        assert merged["total_cost_usd"] == pytest.approx(0.35)
        assert merged["duration_ms"] == 4000

    def test_usage_fields_summed(self):
        a = {
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 20,
                "output_tokens": 5,
            }
        }
        b = {
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 200,
                "output_tokens": 50,
            }
        }
        merged = _merge_cli_output(a, b)
        assert merged["usage"] == {
            "input_tokens": 110,
            "cache_read_input_tokens": 220,
            "output_tokens": 55,
        }

    def test_missing_usage_treated_as_zero(self):
        merged = _merge_cli_output({}, {"result": "ok"})
        assert merged["usage"] == {
            "input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        }

    def test_tool_and_skill_counts_merged_by_sum(self):
        a = {"tool_counts": {"Read": 2}, "skill_counts": {"cq": 1}}
        b = {"tool_counts": {"Read": 3, "Edit": 1}, "skill_counts": {"cq": 2}}
        merged = _merge_cli_output(a, b)
        assert merged["tool_counts"] == {"Read": 5, "Edit": 1}
        assert merged["skill_counts"] == {"cq": 3}

    def test_model_usage_dicts_merged(self):
        a = {"modelUsage": {"sonnet": {"input_tokens": 10}}}
        b = {"modelUsage": {"opus": {"input_tokens": 20}}}
        merged = _merge_cli_output(a, b)
        assert set(merged["modelUsage"]) == {"sonnet", "opus"}


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

        async def side_effect(workspace, cmd, **_kwargs):
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

    async def test_missing_result_triggers_retry_then_defaults(self):
        """Missing result key → retry → retry also empty → 'No response'."""
        first = {"total_cost_usd": 0.01}
        retry = {"result": ""}

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ) as mock:
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.body == "No response"
        assert mock.await_count == 2
        # Second call is the nudge.
        assert NUDGE_PROMPT in mock.call_args_list[1][0][1]

    async def test_empty_result_triggers_retry(self):
        """Empty-string result fires a retry; retry's body is returned."""
        first = _cli_output(result="", cost=0.01, duration_ms=1000)
        retry = _cli_output(result="actual reply", cost=0.02, duration_ms=2000)

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ) as mock:
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.body == "actual reply"
        assert mock.await_count == 2
        retry_cmd = mock.call_args_list[1][0][1]
        assert "--resume" in retry_cmd
        assert "sess-1" in retry_cmd
        assert NUDGE_PROMPT in retry_cmd

    async def test_whitespace_result_triggers_retry(self):
        first = _cli_output(result="   \n  ")
        retry = _cli_output(result="real answer")

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.body == "real answer"

    async def test_retry_accumulates_cost_duration_tokens(self):
        first = _cli_output(
            result="",
            cost=0.01,
            duration_ms=1000,
            input_tokens=100,
            cache_read=50,
            output_tokens=10,
        )
        retry = _cli_output(
            result="final",
            cost=0.02,
            duration_ms=2500,
            input_tokens=200,
            cache_read=30,
            output_tokens=80,
        )

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.cost == pytest.approx(0.03)
        assert resp.duration == pytest.approx(3.5)
        assert resp.tokens_in == 380  # (100+50) + (200+30)
        assert resp.tokens_out == 90

    async def test_retry_merges_tool_counts(self):
        first = _cli_output(result="")
        first["tool_counts"] = {"Read": 2, "Bash": 1}
        first["skill_counts"] = {"cq": 1}
        retry = _cli_output(result="done")
        retry["tool_counts"] = {"Read": 1, "Edit": 3}
        retry["skill_counts"] = {"simplify": 1, "cq": 1}

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            side_effect=[first, retry],
        ):
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.tool_counts == {"Read": 3, "Bash": 1, "Edit": 3}
        assert resp.skill_counts == {"cq": 2, "simplify": 1}

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

    async def test_no_retry_when_body_non_empty(self):
        output = _cli_output(result="real reply")

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert mock.await_count == 1

    async def test_unavailable_short_circuits_fallback(self):
        """When --resume raises ClaudeUnavailableError, do NOT try --session-id fallback."""
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("monthly usage limit"),
        ) as mock:
            with pytest.raises(ClaudeUnavailableError, match="usage limit"):
                await run(Path("/tmp"), "sess-1", "hi", "telegram")
        assert mock.await_count == 1

    async def test_timeout_threaded_to_exec(self):
        output = _cli_output()
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await run(Path("/tmp"), "sess-1", "hi", "telegram", timeout=42.0)

        # All _exec calls should receive timeout=42.0 as kwarg.
        assert mock.call_args.kwargs["timeout"] == 42.0

    async def test_default_timeout_applied(self):
        from claude_on_the_fly.agent import DEFAULT_TIMEOUT

        output = _cli_output()
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert mock.call_args.kwargs["timeout"] == DEFAULT_TIMEOUT

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


# ---------------------------------------------------------------------------
# ensure_persona
# ---------------------------------------------------------------------------


class TestEnsurePersona:
    def test_noop_when_source_missing(self, tmp_path: Path) -> None:
        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            workspace = tmp_path / "ws"
            workspace.mkdir()
            ensure_persona(workspace)
            assert not (workspace / "CLAUDE.md").exists()

    def test_creates_symlink(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace)

        target = workspace / "CLAUDE.md"
        assert target.is_symlink()
        assert target.resolve() == source.resolve()

    def test_replaces_existing_file_with_symlink(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        existing = workspace / "CLAUDE.md"
        existing.write_text("old content")

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace)

        assert existing.is_symlink()
        assert existing.resolve() == source.resolve()

    def test_replaces_wrong_symlink(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        wrong_target = tmp_path / "wrong.md"
        wrong_target.write_text("wrong")

        workspace = tmp_path / "ws"
        workspace.mkdir()
        link = workspace / "CLAUDE.md"
        link.symlink_to(wrong_target)

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace)

        assert link.is_symlink()
        assert link.resolve() == source.resolve()

    def test_noop_when_symlink_already_correct(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        link = workspace / "CLAUDE.md"
        link.symlink_to(source)

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace)

        # Still the same symlink, not recreated
        assert link.is_symlink()
        assert link.resolve() == source.resolve()

    def test_also_creates_agents_md_for_codex(self, tmp_path: Path) -> None:
        """codex reads AGENTS.md, not CLAUDE.md — ensure both are linked."""
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace)

        agents = workspace / "AGENTS.md"
        assert agents.is_symlink()
        assert agents.resolve() == source.resolve()

    def test_agents_md_replaces_existing_file(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        existing = workspace / "AGENTS.md"
        existing.write_text("stale codex instructions")

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace)

        assert existing.is_symlink()
        assert existing.resolve() == source.resolve()

    def test_both_links_idempotent(self, tmp_path: Path) -> None:
        source = tmp_path / "CLAUDE.md"
        source.write_text("persona")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace)
            ensure_persona(workspace)  # second call must not raise

        for filename in ("CLAUDE.md", "AGENTS.md"):
            link = workspace / filename
            assert link.is_symlink()
            assert link.resolve() == source.resolve()


# ---------------------------------------------------------------------------
# OllamaLauncher
# ---------------------------------------------------------------------------


class TestOllamaLauncher:
    def test_prefix_for_claude(self):
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")
        assert launcher.prefix("claude") == [
            "ollama",
            "launch",
            "claude",
            "--model",
            "deepseek-v4-flash:cloud",
            "--yes",
            "--",
        ]

    def test_prefix_parametrizes_agent_name(self):
        """Other agents (codex, gemini, ...) will reuse the same launcher."""
        launcher = OllamaLauncher(model="qwen3.6:latest")
        assert launcher.prefix("codex")[:3] == ["ollama", "launch", "codex"]

    def test_frozen(self):
        from dataclasses import FrozenInstanceError

        launcher = OllamaLauncher(model="x")
        with pytest.raises(FrozenInstanceError):
            launcher.model = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# get_backend factory
# ---------------------------------------------------------------------------


class TestGetBackend:
    def test_default_returns_claude_native(self, clear_backend_env):
        backend = get_backend()
        assert isinstance(backend, ClaudeBackend)
        assert backend.launcher is None

    def test_claude_native_explicit(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "claude")
        monkeypatch.setenv("CLAUDE_MODE", "native")
        backend = get_backend()
        assert isinstance(backend, ClaudeBackend)
        assert backend.launcher is None

    def test_claude_ollama_mode(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "deepseek-v4-flash:cloud")
        backend = get_backend()
        assert isinstance(backend, ClaudeBackend)
        assert backend.launcher == OllamaLauncher(model="deepseek-v4-flash:cloud")

    def test_ollama_without_model_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            get_backend()

    def test_ollama_blank_model_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "   ")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            get_backend()

    def test_unknown_backend_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "gemini")
        with pytest.raises(ValueError, match="gemini"):
            get_backend()

    def test_unknown_claude_mode_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "magic")
        with pytest.raises(ValueError, match="magic"):
            get_backend()


# ---------------------------------------------------------------------------
# ClaudeBackend launcher injection
# ---------------------------------------------------------------------------


class TestClaudeBackendLauncher:
    async def test_no_launcher_includes_model_flag(self):
        output = _cli_output()
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await ClaudeBackend().run(Path("/tmp"), "sess-1", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert cmd[0] == "claude"
        assert "--model" in cmd

    async def test_launcher_prepends_prefix(self):
        output = _cli_output()
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await ClaudeBackend(launcher=launcher).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )

        cmd = mock.call_args[0][1]
        assert cmd[:7] == [
            "ollama",
            "launch",
            "claude",
            "--model",
            "deepseek-v4-flash:cloud",
            "--yes",
            "--",
        ]
        # The claude binary is NOT repeated after `--` — ollama launch already
        # invokes it. The first real arg is the -p flag.
        assert cmd[7] == "-p"
        assert "claude" not in cmd[7:], "redundant claude binary in launcher cmd"

    async def test_launcher_drops_claude_model_flag(self):
        """Launcher decides the model; claude's --model is omitted to avoid dead args."""
        output = _cli_output()
        launcher = OllamaLauncher(model="qwen3.6:latest")
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await ClaudeBackend(launcher=launcher).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )

        cmd = mock.call_args[0][1]
        # The only --model in the command should be the launcher prefix's at index 3.
        model_indices = [i for i, v in enumerate(cmd) if v == "--model"]
        assert model_indices == [3]

    async def test_native_mode_uses_cli_total_cost_usd(self):
        """Without a launcher, cost comes straight from claude's billing field."""
        output = _cli_output(cost=0.05)
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ):
            resp = await ClaudeBackend().run(Path("/tmp"), "sess-1", "hi", "telegram")
        assert resp.cost == 0.05

    async def test_launcher_mode_ignores_cli_total_cost_usd(self):
        """Ollama mode: CLI's Anthropic-priced cost is bogus; pricing.cost_for wins."""
        output = _cli_output(
            cost=0.99,  # nonsense Anthropic-priced value from CLI
            input_tokens=100,
            cache_read=50,
            output_tokens=200,
            model="deepseek-v4-flash:cloud",
        )
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")
        with (
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=output,
            ),
            patch(
                "claude_on_the_fly.backends.claude.pricing.cost_for",
                return_value=0.0042,
            ) as mock_pricing,
        ):
            resp = await ClaudeBackend(launcher=launcher).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )
        assert resp.cost == 0.0042
        mock_pricing.assert_called_once_with("deepseek-v4-flash:cloud", 150, 200)

    async def test_launcher_mode_unknown_model_yields_zero(self):
        """Local models (e.g. gpt-oss:20b) aren't in OpenRouter — cost is $0."""
        output = _cli_output(cost=0.50, model="gpt-oss:20b")
        launcher = OllamaLauncher(model="gpt-oss:20b")
        with (
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=output,
            ),
            patch(
                "claude_on_the_fly.backends.claude.pricing.cost_for",
                return_value=None,
            ),
        ):
            resp = await ClaudeBackend(launcher=launcher).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )
        assert resp.cost == 0

    async def test_launcher_does_not_repeat_claude_binary(self):
        """Regression: ollama launch claude already invokes the binary; a
        second "claude" after `--` becomes argv[1] which -p parses as the prompt."""
        output = _cli_output()
        launcher = OllamaLauncher(model="qwen3.6:latest")
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await ClaudeBackend(launcher=launcher).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )

        cmd = mock.call_args[0][1]
        # "claude" appears exactly once: inside the launcher prefix.
        assert cmd.count("claude") == 1
        assert cmd[2] == "claude"

    async def test_get_backend_factory_drives_run(self, clear_backend_env, monkeypatch):
        """agent.run() routes through get_backend() and honors ollama mode."""
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3.6:latest")
        output = _cli_output()
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await run(Path("/tmp"), "sess-1", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert cmd[:3] == ["ollama", "launch", "claude"]
        assert "qwen3.6:latest" in cmd


# ---------------------------------------------------------------------------
# ClaudeBackend cross-backend transcript handoff
# ---------------------------------------------------------------------------


class TestClaudeBackendHandoff:
    async def test_fallback_path_injects_codex_handoff(self):
        output = _cli_output(result="new session reply")

        async def side_effect(workspace, cmd, **_kwargs):
            if "--resume" in cmd:
                raise RuntimeError("No conversation found with id sess-1")
            return output

        prior_turns = [
            Turn("user", "prior codex msg"),
            Turn("assistant", "prior codex reply"),
        ]
        with (
            patch(
                "claude_on_the_fly.backends.claude.transcript.extract_codex",
                return_value=prior_turns,
            ),
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                side_effect=side_effect,
            ) as mock,
        ):
            await run(Path("/tmp"), "sess-1", "CURRENT_TEXT", "telegram")

        # Second call is the --session-id fallback; its prompt arg should contain
        # both the handoff preamble and the user's actual text.
        fallback_cmd = mock.call_args_list[1][0][1]
        prompt_arg = fallback_cmd[-1]
        assert "[Prior conversation via codex" in prompt_arg
        assert "prior codex msg" in prompt_arg
        assert "prior codex reply" in prompt_arg
        assert prompt_arg.endswith("CURRENT_TEXT")

    async def test_fallback_with_no_codex_history_just_uses_prompt(self):
        output = _cli_output(result="new session reply")

        async def side_effect(workspace, cmd, **_kwargs):
            if "--resume" in cmd:
                raise RuntimeError("No conversation found with id sess-2")
            return output

        with (
            patch(
                "claude_on_the_fly.backends.claude.transcript.extract_codex",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                side_effect=side_effect,
            ) as mock,
        ):
            await run(Path("/tmp"), "sess-2", "JUST_THIS", "telegram")

        fallback_cmd = mock.call_args_list[1][0][1]
        assert "[Prior conversation" not in fallback_cmd[-1]
        assert fallback_cmd[-1] == "JUST_THIS"

    async def test_extractor_exception_falls_through_silently(self):
        output = _cli_output(result="new session reply")

        async def side_effect(workspace, cmd, **_kwargs):
            if "--resume" in cmd:
                raise RuntimeError("No conversation found with id sess-3")
            return output

        with (
            patch(
                "claude_on_the_fly.backends.claude.transcript.extract_codex",
                side_effect=RuntimeError("read failed"),
            ),
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                side_effect=side_effect,
            ) as mock,
        ):
            resp = await run(Path("/tmp"), "sess-3", "TEXT", "telegram")

        # Daemon must keep serving the user even when transcript extraction breaks.
        assert resp.body == "new session reply"
        assert mock.call_args_list[1][0][1][-1] == "TEXT"

    async def test_resume_success_skips_extractor(self):
        """No fallback fires → handoff path is never consulted."""
        output = _cli_output()

        with (
            patch(
                "claude_on_the_fly.backends.claude.transcript.extract_codex"
            ) as mock_extract,
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=output,
            ),
        ):
            await run(Path("/tmp"), "sess-4", "hi", "telegram")

        mock_extract.assert_not_called()


class TestClaudeBackendTakeoverCommand:
    def test_returns_resume_command_when_session_jsonl_exists(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_uuid = "deadbeef-1234"

        # Mirror claude's projects/<hash>/<uuid>.jsonl layout in a fake home.
        from claude_on_the_fly.transcript import _workspace_to_claude_hash

        projects_dir = tmp_path / ".claude" / "projects"
        session_dir = projects_dir / _workspace_to_claude_hash(workspace)
        session_dir.mkdir(parents=True)
        (session_dir / f"{session_uuid}.jsonl").write_text("{}\n")

        with patch(
            "claude_on_the_fly.backends.claude.CLAUDE_PROJECTS_DIR", projects_dir
        ):
            cmd = ClaudeBackend().takeover_command(workspace, session_uuid)

        assert cmd == f"claude --resume {session_uuid}"

    def test_returns_none_when_no_session_jsonl(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)

        with patch(
            "claude_on_the_fly.backends.claude.CLAUDE_PROJECTS_DIR", projects_dir
        ):
            cmd = ClaudeBackend().takeover_command(workspace, "missing-uuid")

        assert cmd is None


class TestClaudeBackendSessionLogPath:
    def test_returns_path_when_jsonl_exists(self, tmp_path: Path) -> None:
        from claude_on_the_fly.transcript import _workspace_to_claude_hash

        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_uuid = "live-uuid"
        projects_dir = tmp_path / ".claude" / "projects"
        session_dir = projects_dir / _workspace_to_claude_hash(workspace)
        session_dir.mkdir(parents=True)
        expected = session_dir / f"{session_uuid}.jsonl"
        expected.write_text("")

        with patch(
            "claude_on_the_fly.backends.claude.CLAUDE_PROJECTS_DIR", projects_dir
        ):
            path = ClaudeBackend().session_log_path(workspace, session_uuid)

        assert path == expected

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)

        with patch(
            "claude_on_the_fly.backends.claude.CLAUDE_PROJECTS_DIR", projects_dir
        ):
            path = ClaudeBackend().session_log_path(workspace, "absent")

        assert path is None
