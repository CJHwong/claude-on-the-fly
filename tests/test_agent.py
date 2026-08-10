"""Tests for claude_on_the_fly.agent module."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import claude_on_the_fly.agent as agent_mod
from claude_on_the_fly.agent import (
    ATTACHMENT_PLATFORMS,
    FORMAT_HINTS,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    NO_HANDOFF_PLATFORMS,
    NUDGE_PROMPT,
    OUTBOX_ARCHIVE,
    OUTBOX_DIRNAME,
    ClaudeUnavailableError,
    Compaction,
    InterimRelay,
    OllamaLauncher,
    Response,
    _classify,
    _exec,
    _merge_cli_output,
    archive_outbox,
    build_system_prompt,
    collect_outbox,
    current_backend_key,
    ensure_persona,
    get_backend,
    parse_stream,
    persona_for,
    read_attachment,
    reset_progress_sink,
    run,
    set_progress_sink,
    stats_mode,
    write_attachment,
)
from claude_on_the_fly.backends import codex as codex_mod
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
# Compaction
# ---------------------------------------------------------------------------


class TestDataDirFrom:
    def test_default_is_home_dot_claude_on_the_fly(self):
        assert agent_mod.data_dir_from({}) == Path.home() / ".claude-on-the-fly"

    def test_env_var_points_the_data_dir(self):
        assert agent_mod.data_dir_from({"COTF_DATA_DIR": "/srv/cotf-a"}) == Path(
            "/srv/cotf-a"
        )

    def test_empty_env_var_falls_back_like_an_absent_one(self):
        assert agent_mod.data_dir_from({"COTF_DATA_DIR": ""}) == (
            Path.home() / ".claude-on-the-fly"
        )

    def test_module_constant_was_resolved_from_the_environment(self):
        """The daemon-wide constant is the function applied to os.environ, so a
        launch with COTF_DATA_DIR set moves every DATA_DIR-derived path."""
        assert agent_mod.data_dir_from(os.environ) == agent_mod.DATA_DIR


class TestCompaction:
    def test_saved_tokens_is_the_difference(self):
        c = Compaction(ok=True, pre_tokens=48939, post_tokens=5162)
        assert c.saved_tokens == 48939 - 5162

    def test_saved_tokens_never_goes_negative(self):
        """A compaction that grew the conversation is nonsense, not a credit."""
        assert Compaction(ok=True, pre_tokens=100, post_tokens=500).saved_tokens == 0

    def test_summary_reports_both_sides_and_the_wait(self):
        c = Compaction(ok=True, pre_tokens=48939, post_tokens=5162, duration=10.8)
        assert c.summary() == (
            "Compacted the conversation: 48,939 → 5,162 tokens in 11s."
        )

    def test_summary_without_numbers_still_says_it_happened(self):
        """The transcript boundary is the only source of the numbers; a
        successful compaction whose boundary couldn't be read is still a success."""
        assert Compaction(ok=True).summary() == "Compacted the conversation."

    def test_summary_prefers_the_clis_own_refusal(self):
        c = Compaction(ok=False, error="Not enough messages to compact.")
        assert c.summary() == "Couldn't compact: Not enough messages to compact."

    def test_summary_falls_back_when_there_is_no_reason(self):
        assert Compaction(ok=False).summary() == "Nothing to compact."


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
        monkeypatch.setenv("TELEGRAM_STATS_MODE", "DETAILED")
        assert stats_mode("telegram") == "detailed"

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


class TestJobsPlatformRegistration:
    """Acceptance #15: the "jobs" platform is wired into agent.py correctly."""

    def test_jobs_is_no_handoff(self):
        # Each job is an independent one-shot in a fresh session — no transcript
        # handoff, same as "schedule".
        assert "jobs" in NO_HANDOFF_PLATFORMS

    def test_jobs_has_format_hint(self):
        assert "jobs" in FORMAT_HINTS

    def test_jobs_is_not_attachment_platform(self):
        # Nothing collects the outbox on the jobs path and Result is text-only,
        # so adding "jobs" here would make the agent promise files it can't send.
        assert "jobs" not in ATTACHMENT_PLATFORMS


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

    def test_unknown_platform_falls_back_to_telegram(self):
        result = build_system_prompt("discord", "charlie", "dm")
        assert FORMAT_HINTS["telegram"] in result
        # The slack hint should NOT be present
        assert FORMAT_HINTS["slack"] not in result

    def test_all_template_variables_substituted(self):
        result = build_system_prompt("telegram", "hoss", "channel:dev")
        # No leftover {placeholders}
        assert "{format_hint}" not in result
        assert "{user_name}" not in result
        assert "{channel_context}" not in result
        assert "{memory_root}" not in result
        assert "{knowledge_dir}" not in result
        assert "{outbox_instruction}" not in result

    def test_outbox_names_absolute_dir_for_attachment_platforms(self, tmp_path: Path):
        for platform in ("slack", "telegram"):
            result = build_system_prompt(platform, "hoss", "dm", tmp_path)
            assert str(tmp_path / OUTBOX_DIRNAME) in result
            assert "You CAN send files" in result
            assert "{outbox_dir}" not in result  # placeholder fully substituted

    def test_outbox_instruction_absent_for_non_attachment_platforms(
        self, tmp_path: Path
    ):
        for platform in ("cron", "jobs", "discord"):
            result = build_system_prompt(platform, "hoss", "dm", tmp_path)
            assert "You CAN send files" not in result

    def test_outbox_instruction_absent_without_workspace(self):
        # Can't name the absolute dir with no workspace, so don't inject it.
        result = build_system_prompt("telegram", "hoss", "dm")
        assert "You CAN send files" not in result


# ---------------------------------------------------------------------------
# collect_outbox / archive_outbox
# ---------------------------------------------------------------------------


class TestCollectOutbox:
    def test_no_outbox_dir_returns_empty(self, tmp_path: Path):
        assert collect_outbox(tmp_path) == []

    def test_collects_regular_files_sorted_by_name(self, tmp_path: Path):
        outbox = tmp_path / OUTBOX_DIRNAME
        outbox.mkdir()
        (outbox / "b.txt").write_text("b")
        (outbox / "a.txt").write_text("a")
        result = collect_outbox(tmp_path)
        assert [p.name for p in result] == ["a.txt", "b.txt"]

    def test_skips_dotfiles_subdirs_and_archive(self, tmp_path: Path):
        outbox = tmp_path / OUTBOX_DIRNAME
        outbox.mkdir()
        (outbox / "keep.txt").write_text("x")
        (outbox / ".hidden").write_text("x")
        (outbox / "sub").mkdir()
        (outbox / OUTBOX_ARCHIVE).mkdir()
        (outbox / OUTBOX_ARCHIVE / "old.txt").write_text("x")
        result = collect_outbox(tmp_path)
        assert [p.name for p in result] == ["keep.txt"]

    def test_skips_oversize_file(self, tmp_path: Path):
        outbox = tmp_path / OUTBOX_DIRNAME
        outbox.mkdir()
        (outbox / "small.txt").write_text("x")
        (outbox / "big.bin").write_bytes(b"0" * (MAX_ATTACHMENT_BYTES + 1))
        result = collect_outbox(tmp_path)
        assert [p.name for p in result] == ["small.txt"]

    def test_enforces_count_cap(self, tmp_path: Path):
        outbox = tmp_path / OUTBOX_DIRNAME
        outbox.mkdir()
        for i in range(MAX_ATTACHMENTS + 3):
            (outbox / f"f{i:02d}.txt").write_text("x")
        result = collect_outbox(tmp_path)
        assert len(result) == MAX_ATTACHMENTS

    def test_rejects_symlinks_at_collection_and_read(self, tmp_path: Path):
        outbox = tmp_path / OUTBOX_DIRNAME
        outbox.mkdir()
        target = tmp_path / "outside.txt"
        target.write_text("not an attachment")
        link = outbox / "report.txt"
        link.symlink_to(target)

        assert collect_outbox(tmp_path) == []
        with pytest.raises(OSError):
            read_attachment(link)

    def test_download_replaces_a_symlink_not_its_target(self, tmp_path: Path):
        target = tmp_path / "outside.txt"
        target.write_text("original")
        destination = tmp_path / "workspace.txt"
        destination.symlink_to(target)

        write_attachment(destination, b"new attachment")

        assert target.read_text() == "original"
        assert destination.read_bytes() == b"new attachment"
        assert not destination.is_symlink()


class TestArchiveOutbox:
    def test_empty_list_is_noop(self, tmp_path: Path):
        archive_outbox(tmp_path, [])
        assert not (tmp_path / OUTBOX_DIRNAME / OUTBOX_ARCHIVE).exists()

    def test_moves_files_into_archive_and_empties_outbox(self, tmp_path: Path):
        outbox = tmp_path / OUTBOX_DIRNAME
        outbox.mkdir()
        f = outbox / "report.csv"
        f.write_text("data")
        archive_outbox(tmp_path, [f])
        assert not f.exists()
        archived = list((outbox / OUTBOX_ARCHIVE).rglob("report.csv"))
        assert len(archived) == 1
        assert archived[0].read_text() == "data"
        # A fresh scan finds nothing left to re-send.
        assert collect_outbox(tmp_path) == []


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


class _AsyncChunkReader:
    """Stand-in for `StreamReader.read`: the payload once, then EOF forever.

    A double that answers every `read()` with the same bytes describes a stream
    that never ends, which is not a thing a pipe does. It only looked correct
    while the collector called `read` exactly once.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __call__(self, _limit: int = -1) -> bytes:
        payload, self._payload = self._payload, b""
        return payload


class _ChunkedStreamReader(asyncio.StreamReader):
    """A stream reader that exposes predetermined, awkward byte boundaries."""

    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__()
        self._chunks = deque(chunks)

    async def read(self, _limit: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.popleft()


def _make_proc(returncode: int, stdout: bytes, stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = _AsyncLineIter(stdout)
    proc.stderr = MagicMock()
    proc.stderr.read = _AsyncChunkReader(stderr)
    proc.wait = AsyncMock(return_value=returncode)
    return proc


def _codex_item_event(event_type: str, item_type: str, text: str | None = None) -> dict:
    item = {"type": item_type}
    if text is not None:
        item["text"] = text
    return {"type": event_type, "item": item}


def _make_codex_chunked_proc(stdout: bytes):
    split_points = (7, 31, 67, 103, len(stdout))
    chunks = [
        stdout[start:end]
        for start, end in zip((0, *split_points[:-1]), split_points, strict=True)
    ]
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = _ChunkedStreamReader(chunks)
    proc.stderr = _ChunkedStreamReader([b""])
    proc.wait = AsyncMock(return_value=0)
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

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match="something broke"),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_nonzero_exit_empty_stderr_uses_fallback(self):
        proc = _make_proc(42, b"", stderr=b"")

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match="Exit code 42"),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_is_error_true_raises(self):
        stream = _ndjson(
            _result_line(is_error=True, result="bad stuff", subtype="tool_error")
        )
        proc = _make_proc(0, stream)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match="bad stuff"),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_error_subtype_raises(self):
        stream = _ndjson(
            _result_line(is_error=False, subtype="error_max_turns", result="too many")
        )
        proc = _make_proc(0, stream)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match="too many"),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_is_error_missing_result_defaults(self):
        # Result line with is_error but no result field
        stream = _ndjson({"type": "result", "is_error": True})
        proc = _make_proc(0, stream)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match="Unknown error"),
        ):
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

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(RuntimeError, match="API Error: Could not process image"),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_usage_limit_raises_unavailable(self):
        stream = _ndjson(
            _result_line(result="You've hit your org's monthly usage limit")
        )
        proc = _make_proc(1, stream)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(ClaudeUnavailableError, match="usage limit"),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_usage_allocation_disabled_raises_unavailable(self):
        stream = _ndjson(
            _result_line(result="Your usage allocation has been disabled by your admin")
        )
        proc = _make_proc(1, stream)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(ClaudeUnavailableError, match="allocation"),
        ):
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
        proc.stderr.read = _AsyncChunkReader(b"")
        proc.kill = MagicMock(side_effect=lambda: setattr(proc, "returncode", -9))
        proc.wait = AsyncMock(return_value=-9)

        # `proc` is a MagicMock, so `proc.pid` resolves through __index__ to 1 and
        # the reaper would hand `killpg` a real process group. Whether that is
        # refused (and the fallback below runs) or succeeds is then up to the host:
        # unprivileged it raises EPERM, but as root in a container pgid 1 is our
        # own group and the suite SIGKILLs itself. Patch the syscall so the
        # fallback this test is about is reached deterministically, and nothing
        # real is ever signalled.
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(agent_mod.os, "killpg", side_effect=PermissionError),
            pytest.raises(RuntimeError, match=re.escape("timed out after 0.1s")),
        ):
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
        # See test_timeout_kills_proc_and_raises: killpg is patched so the mock's
        # pid cannot reach a live process group.
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(agent_mod.os, "killpg", side_effect=PermissionError),
            pytest.raises(RuntimeError, match=re.escape("timed out after 0.1s")),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"], timeout=0.1)
        proc.kill.assert_called_once()

    async def test_timeout_reaps_process_tree_and_raises(self) -> None:
        """On timeout the CLI is reaped via the process-tree kill and the
        timeout still propagates as RuntimeError."""
        proc = _never_ending_proc()
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch(
                "claude_on_the_fly.agent._kill_process_tree", new_callable=AsyncMock
            ) as mock_kill,
            pytest.raises(RuntimeError, match=re.escape("timed out after 0.1s")),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"], timeout=0.1)
        mock_kill.assert_awaited_once_with(proc)

    async def test_progress_sink_receives_mid_turn_text(self):
        """Passing `timeout=` is deliberate: it proves the ContextVar survives
        `asyncio.wait_for(_consume(proc), ...)`, which the `sandbox.session_env`
        precedent does not establish (agent_env() is read before the wrapper)."""
        emitted: list[str] = []
        stream = _ndjson(
            _assistant_line({"type": "text", "text": "working"}),
            _assistant_line(_tool_use("Bash")),
            _result_line(result="done"),
        )
        proc = _make_proc(0, stream)

        token = set_progress_sink(emitted.append)
        try:
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = await _exec(Path("/tmp"), ["claude", "-p", "hi"], timeout=5)
        finally:
            reset_progress_sink(token)

        assert emitted == ["working"]
        assert result["result"] == "done"

    async def test_a_raising_sink_does_not_kill_the_turn(self, caplog):
        def boom(text: str) -> None:
            raise RuntimeError("the sink is broken")

        stream = _ndjson(
            _assistant_line({"type": "text", "text": "working"}),
            _assistant_line(_tool_use("Bash")),
            _result_line(result="done"),
        )
        proc = _make_proc(0, stream)

        token = set_progress_sink(boom)
        try:
            with (
                patch("asyncio.create_subprocess_exec", return_value=proc),
                caplog.at_level("ERROR", logger="claude_on_the_fly.agent"),
            ):
                result = await _exec(Path("/tmp"), ["claude", "-p", "hi"])
        finally:
            reset_progress_sink(token)

        assert result["result"] == "done"
        assert "progress relay failed" in caplog.text


class TestCodexInterimProgress:
    def test_observer_ignores_blank_and_malformed_lines(self, caplog):
        observer = codex_mod._CodexStreamObserver(
            codex_mod._StreamWatch(), lambda _text: None
        )

        with caplog.at_level("ERROR", logger=codex_mod.__name__):
            observer.feed(b"\nnot-json\n[]\n")
            observer.finish()

        assert "progress relay failed" in caplog.text

    async def test_forwards_narration_across_chunk_boundaries_without_final_duplication(
        self,
    ):
        stream = b"\n".join(
            json.dumps(event).encode()
            for event in (
                {"type": "thread.started", "thread_id": "thread-1"},
                _codex_item_event("item.completed", "agent_message", "first step"),
                _codex_item_event("item.started", "command_execution"),
                _codex_item_event("item.completed", "command_execution"),
                _codex_item_event("item.completed", "agent_message", "final answer"),
                {"type": "turn.completed", "usage": {}},
            )
        )
        proc = _make_codex_chunked_proc(stream)
        emitted: list[str] = []
        token = set_progress_sink(emitted.append)
        try:
            captured, _stderr = await codex_mod._collect_codex_output(proc)
        finally:
            reset_progress_sink(token)

        assert emitted == ["first step"]
        assert codex_mod.parse_codex_stream(captured)["body"] == "final answer"

    async def test_a_broken_progress_sink_does_not_break_codex_output(self, caplog):
        stream = b"\n".join(
            json.dumps(event).encode()
            for event in (
                _codex_item_event("item.completed", "agent_message", "working"),
                _codex_item_event("item.started", "command_execution"),
                _codex_item_event("item.completed", "agent_message", "done"),
                {"type": "turn.completed", "usage": {}},
            )
        )
        proc = _make_codex_chunked_proc(stream)

        def fail(_text: str) -> None:
            raise RuntimeError("progress sink failed")

        token = set_progress_sink(fail)
        try:
            with caplog.at_level("ERROR", logger=codex_mod.__name__):
                captured, _stderr = await codex_mod._collect_codex_output(proc)
        finally:
            reset_progress_sink(token)

        assert codex_mod.parse_codex_stream(captured)["body"] == "done"
        assert "progress relay failed" in caplog.text


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
    proc.stderr.read = _AsyncChunkReader(b"")
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
# Mid-turn progress relay
# ---------------------------------------------------------------------------


class TestInterimRelay:
    """Written against the MEASURED stream shape: one content block per assistant
    message, so "pending across messages" is the normal path, not the fallback."""

    def _relay(self) -> tuple[InterimRelay, list[str]]:
        emitted: list[str] = []
        return InterimRelay(emitted.append), emitted

    def test_the_measured_two_tool_sequence_forwards_both_narrations(self):
        relay, emitted = self._relay()
        for msg in (
            _assistant_line({"type": "thinking", "thinking": "let me see"}),
            _assistant_line({"type": "text", "text": "Step one now."}),
            _assistant_line(_tool_use("Bash")),
            _assistant_line({"type": "thinking", "thinking": "and now the second"}),
            _assistant_line({"type": "text", "text": "Step two now."}),
            _assistant_line(_tool_use("Bash")),
            _assistant_line({"type": "text", "text": "final answer"}),
        ):
            relay.feed(msg)

        assert emitted == ["Step one now.", "Step two now."]

    def test_text_alone_waits_for_the_next_tool_call(self):
        relay, emitted = self._relay()
        relay.feed(_assistant_line({"type": "text", "text": "looking"}))
        assert emitted == []

        relay.feed(_assistant_line(_tool_use("Read")))
        assert emitted == ["looking"]

    def test_the_last_block_is_the_answer_and_is_not_forwarded(self):
        relay, emitted = self._relay()
        relay.feed(_assistant_line({"type": "text", "text": "narration"}))
        relay.feed(_assistant_line(_tool_use("Bash")))
        relay.feed(_assistant_line({"type": "text", "text": "the answer"}))

        assert emitted == ["narration"]

    def test_text_after_a_tool_use_in_the_same_message_is_held(self):
        """The one multi-block case, and the one that pins the in-loop flush:
        a post-loop flush would release "after" before it was proven narration."""
        relay, emitted = self._relay()
        relay.feed(
            _assistant_line(
                {"type": "text", "text": "before"},
                _tool_use("Bash"),
                {"type": "text", "text": "after"},
            )
        )
        assert emitted == ["before"]

        relay.feed(_assistant_line(_tool_use("Read")))
        assert emitted == ["before", "after"]

    def test_thinking_blocks_are_not_forwarded(self):
        relay, emitted = self._relay()
        relay.feed(_assistant_line({"type": "thinking", "thinking": "private"}))
        relay.feed(_assistant_line(_tool_use("Bash")))

        assert emitted == []

    def test_subagent_lines_are_ignored(self):
        relay, emitted = self._relay()
        relay.feed(
            {
                **_assistant_line({"type": "text", "text": "sub"}),
                "parent_tool_use_id": "toolu_1",
            }
        )
        relay.feed(_assistant_line(_tool_use("Bash")))

        assert emitted == []

    def test_a_null_parent_tool_use_id_is_still_forwarded(self):
        """The measured default-stream shape: the key is present, valued None."""
        relay, emitted = self._relay()
        relay.feed(
            {
                **_assistant_line({"type": "text", "text": "main"}),
                "parent_tool_use_id": None,
            }
        )
        relay.feed(_assistant_line(_tool_use("Bash")))

        assert emitted == ["main"]

    def test_blank_text_is_skipped(self):
        relay, emitted = self._relay()
        relay.feed(_assistant_line({"type": "text", "text": "   "}))
        relay.feed(_assistant_line(_tool_use("Bash")))

        assert emitted == []

    def test_non_assistant_lines_are_ignored(self):
        relay, emitted = self._relay()
        relay.feed(_result_line())
        relay.feed({"type": "user", "message": {"content": [{"type": "tool_result"}]}})
        relay.feed({"type": "system", "subtype": "hook_started"})
        relay.feed({"type": "system", "subtype": "init"})

        assert emitted == []

    def test_several_text_messages_keep_their_order(self):
        relay, emitted = self._relay()
        for word in ("first", "second", "third"):
            relay.feed(_assistant_line({"type": "text", "text": word}))
        relay.feed(_assistant_line(_tool_use("Bash")))

        assert emitted == ["first", "second", "third"]


class TestProgressSink:
    def test_sink_is_off_by_default(self):
        assert agent_mod._PROGRESS_SINK.get() is None

    def test_set_and_reset_round_trip(self):
        emitted: list[str] = []
        sink = emitted.append
        token = set_progress_sink(sink)
        try:
            assert agent_mod._PROGRESS_SINK.get() is sink
        finally:
            reset_progress_sink(token)
        assert agent_mod._PROGRESS_SINK.get() is None


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

    def test_ordinary_turn_reports_no_compaction(self):
        stream = _ndjson(_result_line(result="done"))
        assert parse_stream(stream)["compact"] == {}

    def test_compaction_status_events_are_captured(self):
        """The only in-band way to tell a compaction from a dead turn: both end
        with `subtype: "success"` and an empty `result`."""
        stream = _ndjson(
            {"type": "system", "subtype": "status", "status": "compacting"},
            {
                "type": "system",
                "subtype": "status",
                "status": None,
                "compact_result": "success",
            },
            _result_line(result=""),
        )
        out = parse_stream(stream)
        assert out["compact"] == {"started": True, "result": "success"}

    def test_failed_compaction_carries_the_clis_own_reason(self):
        stream = _ndjson(
            {"type": "system", "subtype": "status", "status": "compacting"},
            {
                "type": "system",
                "subtype": "status",
                "status": None,
                "compact_result": "failed",
                "compact_error": "Not enough messages to compact.",
            },
            _result_line(result="Not enough messages to compact."),
        )
        out = parse_stream(stream)
        assert out["compact"]["result"] == "failed"
        assert out["compact"]["error"] == "Not enough messages to compact."

    def test_unrelated_status_events_do_not_look_like_a_compaction(self):
        stream = _ndjson(
            {"type": "system", "subtype": "status", "status": "thinking"},
            _result_line(result="done"),
        )
        assert "result" not in parse_stream(stream)["compact"]

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

    def test_last_assistant_usage_is_captured(self):
        """The reading a compaction faces is the final prompt size, so only the
        last assistant message's usage survives to the envelope."""
        stream = _ndjson(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [],
                    "usage": {"input_tokens": 20_000},
                },
            },
            {"type": "user", "message": {"content": []}},
            {
                "type": "assistant",
                "message": {
                    "id": "msg_2",
                    "content": [],
                    "usage": {"input_tokens": 22_100, "output_tokens": 45},
                },
            },
            _result_line(),
        )
        out = parse_stream(stream)
        assert out["last_assistant_usage"] == {
            "input_tokens": 22_100,
            "output_tokens": 45,
        }

    def test_no_assistant_message_leaves_no_usage_reading(self):
        """A compaction turn emits no assistant message; the envelope must not
        fall back to its top-level `usage` (the whole turn's aggregate)."""
        stream = _ndjson(_result_line())
        assert "last_assistant_usage" not in parse_stream(stream)


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
    cache_write: int = 0,
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
            "cache_creation_input_tokens": cache_write,
            "output_tokens": output_tokens,
        },
        "modelUsage": {model: {"input_tokens": input_tokens}} if model else {},
    }


class TestRun:
    @pytest.fixture(autouse=True)
    def _reset_backend_env(self, clear_backend_env):
        """Auto-apply env reset so a dev's `CLAUDE_MODE=pty` (or similar)
        doesn't route these mocked tests through `_exec_pty` and spawn the
        real claude-pty binary."""

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

    async def test_session_not_found_falls_back_to_new(
        self, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        """No session JSONL on disk → backend skips --resume entirely and
        goes straight to --session-id with a handoff preamble. Uses
        tmp_path + the redirect fixtures so the existence check sees only
        the test sandbox."""
        output = _cli_output(result="new session reply")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            resp = await run(workspace, "sess-1", "hello", "slack", "alice")

        assert resp.body == "new session reply"
        # Existence check fails → single call with --session-id, no failed
        # --resume attempt first.
        assert mock.await_count == 1
        cmd = mock.call_args_list[0][0][1]
        assert "--session-id" in cmd
        assert "--resume" not in cmd

    async def test_existing_session_resumes(
        self, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        """JSONL present in the projects dir → backend uses --resume; no
        fallback to --session-id."""
        from claude_on_the_fly.transcript import _workspace_to_claude_hash

        output = _cli_output(result="resumed")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_dir = claude_projects_dir / _workspace_to_claude_hash(workspace)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "sess-existing.jsonl").write_text('{"type":"user"}\n')

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await run(workspace, "sess-existing", "hi", "telegram")

        cmd = mock.call_args_list[0][0][1]
        assert "--resume" in cmd
        assert "--session-id" not in cmd

    async def test_other_runtime_error_reraised(self):
        with (
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                side_effect=RuntimeError("something completely different"),
            ),
            pytest.raises(RuntimeError, match="something completely different"),
        ):
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
            resp = await run(Path("/tmp"), "sess-1", "hi", "telegram")

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
        with (
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                side_effect=ClaudeUnavailableError("monthly usage limit"),
            ) as mock,
            pytest.raises(ClaudeUnavailableError, match="usage limit"),
        ):
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

    async def test_resume_cmd_contains_session_uuid(
        self, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        """Pre-create the session JSONL so the backend takes the --resume
        branch (not --session-id), then verify the uuid+prompt make it
        through."""
        from claude_on_the_fly.transcript import _workspace_to_claude_hash

        output = _cli_output()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_dir = claude_projects_dir / _workspace_to_claude_hash(workspace)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "my-uuid.jsonl").write_text('{"type":"user"}\n')

        with patch(
            "claude_on_the_fly.agent._exec", new_callable=AsyncMock, return_value=output
        ) as mock:
            await run(workspace, "my-uuid", "hi", "telegram", "hoss", "channel:dev")

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

    def test_a_per_chat_source_wins_over_the_global(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("global")
        channel = tmp_path / "personas" / "oncall.md"
        channel.parent.mkdir()
        channel.write_text("oncall")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace, channel)

        for filename in ("CLAUDE.md", "AGENTS.md"):
            assert (workspace / filename).resolve() == channel.resolve()

    def test_a_stale_link_is_removed_when_nothing_resolves(
        self, tmp_path: Path
    ) -> None:
        """The persona entry was deleted and there is no global file to fall back
        to. Leaving the link would keep the chat on a persona nothing configures."""
        gone = tmp_path / "personas" / "old.md"
        gone.parent.mkdir()
        gone.write_text("retired")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace, gone)
            gone.unlink()
            ensure_persona(workspace)

        assert not (workspace / "CLAUDE.md").exists()
        assert not (workspace / "AGENTS.md").is_symlink()

    def test_a_real_file_is_left_alone_when_nothing_resolves(
        self, tmp_path: Path
    ) -> None:
        """Only links into DATA_DIR are ours. A CLAUDE.md the agent wrote in its own
        workspace is content, and deleting it would be data loss."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "CLAUDE.md").write_text("written by the agent")

        with patch("claude_on_the_fly.agent.DATA_DIR", tmp_path):
            ensure_persona(workspace)

        assert (workspace / "CLAUDE.md").read_text() == "written by the agent"

    def test_a_link_outside_data_dir_is_left_alone(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere.md"
        elsewhere.write_text("not ours")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "CLAUDE.md").symlink_to(elsewhere)

        with patch("claude_on_the_fly.agent.DATA_DIR", data_dir):
            ensure_persona(workspace)

        assert (workspace / "CLAUDE.md").is_symlink()


# ---------------------------------------------------------------------------
# persona_for
# ---------------------------------------------------------------------------


class TestPersonaFor:
    def _persona(self, root: Path, name: str = "oncall.md") -> Path:
        path = root / "personas" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"# {name}")
        return path

    def test_no_personas_section_means_the_global_persona(
        self, operator_settings: Path
    ) -> None:
        operator_settings.write_text("slack:\n  stats: off\n")
        assert persona_for("slack", ("C07ABCDEF",)) is None

    def test_a_channel_id_key_matches(self, operator_settings: Path) -> None:
        root = operator_settings.parent
        wanted = self._persona(root)
        operator_settings.write_text(
            "slack:\n  personas:\n    C07ABCDEF: personas/oncall.md\n"
        )
        assert persona_for("slack", ("C07ABCDEF", "ops-alerts")) == wanted.resolve()

    def test_a_channel_name_key_matches(self, operator_settings: Path) -> None:
        root = operator_settings.parent
        wanted = self._persona(root)
        operator_settings.write_text(
            "slack:\n  personas:\n    ops-alerts: personas/oncall.md\n"
        )
        assert persona_for("slack", ("C07ABCDEF", "ops-alerts")) == wanted.resolve()

    def test_the_first_key_wins_over_a_later_one(self, operator_settings: Path) -> None:
        root = operator_settings.parent
        by_id = self._persona(root, "by-id.md")
        self._persona(root, "by-name.md")
        operator_settings.write_text(
            "slack:\n"
            "  personas:\n"
            "    C07ABCDEF: personas/by-id.md\n"
            "    ops-alerts: personas/by-name.md\n"
        )
        assert persona_for("slack", ("C07ABCDEF", "ops-alerts")) == by_id.resolve()

    def test_default_is_consulted_after_every_key(
        self, operator_settings: Path
    ) -> None:
        fallback = self._persona(operator_settings.parent, "team.md")
        operator_settings.write_text(
            "slack:\n"
            "  personas:\n"
            "    C0OTHER: personas/oncall.md\n"
            "    default: personas/team.md\n"
        )
        assert persona_for("slack", ("C07ABCDEF",)) == fallback.resolve()

    def test_a_listed_chat_wins_over_default(self, operator_settings: Path) -> None:
        root = operator_settings.parent
        wanted = self._persona(root)
        self._persona(root, "team.md")
        operator_settings.write_text(
            "slack:\n"
            "  personas:\n"
            "    C07ABCDEF: personas/oncall.md\n"
            "    default: personas/team.md\n"
        )
        assert persona_for("slack", ("C07ABCDEF",)) == wanted.resolve()

    def test_no_match_and_no_default_means_the_root_persona(
        self, operator_settings: Path
    ) -> None:
        self._persona(operator_settings.parent)
        operator_settings.write_text(
            "slack:\n  personas:\n    C0OTHER: personas/oncall.md\n"
        )
        assert persona_for("slack", ("C07ABCDEF",)) is None

    def test_a_missing_file_falls_back_and_says_so(
        self, operator_settings: Path, caplog
    ) -> None:
        operator_settings.write_text(
            "slack:\n  personas:\n    C07ABCDEF: personas/typo.md\n"
        )
        with caplog.at_level(logging.ERROR):
            assert persona_for("slack", ("C07ABCDEF",)) is None
        assert "does not exist" in caplog.text
        assert "C07ABCDEF" in caplog.text

    def test_a_path_escaping_the_data_root_is_refused(
        self, operator_settings: Path, caplog
    ) -> None:
        outside = operator_settings.parent.parent / "elsewhere.md"
        outside.write_text("someone else's file")
        operator_settings.write_text(
            f"slack:\n  personas:\n    C07ABCDEF: ../{outside.name}\n"
        )
        with caplog.at_level(logging.ERROR):
            assert persona_for("slack", ("C07ABCDEF",)) is None
        assert "escapes" in caplog.text

    def test_an_absolute_path_is_refused(self, operator_settings: Path, caplog) -> None:
        outside = operator_settings.parent.parent / "elsewhere.md"
        outside.write_text("someone else's file")
        operator_settings.write_text(f"slack:\n  personas:\n    C07ABCDEF: {outside}\n")
        with caplog.at_level(logging.ERROR):
            assert persona_for("slack", ("C07ABCDEF",)) is None
        assert "escapes" in caplog.text

    def test_a_non_string_value_is_refused(
        self, operator_settings: Path, caplog
    ) -> None:
        operator_settings.write_text("slack:\n  personas:\n    C07ABCDEF: [a, b]\n")
        with caplog.at_level(logging.ERROR):
            assert persona_for("slack", ("C07ABCDEF",)) is None
        assert "must be a path relative to" in caplog.text

    def test_a_rejected_key_still_falls_through_to_the_next_one(
        self, operator_settings: Path, caplog
    ) -> None:
        root = operator_settings.parent
        wanted = self._persona(root, "by-name.md")
        operator_settings.write_text(
            "slack:\n"
            "  personas:\n"
            "    C07ABCDEF: personas/typo.md\n"
            "    ops-alerts: personas/by-name.md\n"
        )
        with caplog.at_level(logging.ERROR):
            assert persona_for("slack", ("C07ABCDEF", "ops-alerts")) == wanted.resolve()
        assert "does not exist" in caplog.text

    def test_a_rejected_key_still_falls_through_to_default(
        self, operator_settings: Path, caplog
    ) -> None:
        fallback = self._persona(operator_settings.parent, "team.md")
        operator_settings.write_text(
            "slack:\n"
            "  personas:\n"
            "    C07ABCDEF: personas/typo.md\n"
            "    default: personas/team.md\n"
        )
        with caplog.at_level(logging.ERROR):
            assert persona_for("slack", ("C07ABCDEF",)) == fallback.resolve()
        assert "does not exist" in caplog.text

    def test_personas_that_is_not_a_mapping_is_named(
        self, operator_settings: Path, caplog
    ) -> None:
        operator_settings.write_text("slack:\n  personas: personas/oncall.md\n")
        with caplog.at_level(logging.ERROR):
            assert persona_for("slack", ("C07ABCDEF",)) is None
        assert "must be a mapping" in caplog.text


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
# current_backend_key — fold backend/mode/model into a canonical string
# ---------------------------------------------------------------------------


class TestCurrentBackendKey:
    def test_default_native_model_is_empty(self, clear_backend_env, monkeypatch):
        """No CLAUDE_MODEL → empty model segment (don't pin sonnet)."""
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        assert current_backend_key() == "claude:native:"

    def test_claude_native_includes_explicit_model(
        self, clear_backend_env, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_MODEL", "opus")
        assert current_backend_key() == "claude:native:opus"

    def test_claude_ollama_includes_model(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:32b")
        assert current_backend_key() == "claude:ollama:qwen2.5-coder:32b"

    def test_claude_pty_includes_model(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "pty")
        monkeypatch.setenv("CLAUDE_MODEL", "sonnet")
        assert current_backend_key() == "claude:pty:sonnet"

    def test_codex_native_includes_model(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODEL", "gpt-5")
        assert current_backend_key() == "codex:native:gpt-5"

    def test_codex_native_blank_model_defaults(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.delenv("CODEX_MODEL", raising=False)
        assert current_backend_key() == "codex:native:default"

    def test_codex_ollama_includes_model(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        monkeypatch.setenv("CODEX_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "llama3.1")
        assert current_backend_key() == "codex:ollama:llama3.1"

    def test_ollama_without_model_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            current_backend_key()

    def test_unknown_backend_raises(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "gemini")
        with pytest.raises(ValueError, match="gemini"):
            current_backend_key()

    def test_switching_modes_produces_distinct_keys(
        self, clear_backend_env, monkeypatch
    ):
        """The whole point of this helper: each combo must give a unique key
        so session UUIDs derived from it don't collide across model switches."""
        monkeypatch.setenv("CLAUDE_MODE", "native")
        native_key = current_backend_key()

        monkeypatch.setenv("CLAUDE_MODE", "ollama")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder")
        ollama_key = current_backend_key()

        assert native_key != ollama_key


# ---------------------------------------------------------------------------
# ClaudeBackend launcher injection
# ---------------------------------------------------------------------------


class TestClaudeBackendLauncher:
    async def test_model_flag_follows_env(self, monkeypatch):
        output = _cli_output()
        # Explicit CLAUDE_MODEL → --model present.
        monkeypatch.setenv("CLAUDE_MODEL", "opus")
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await ClaudeBackend().run(Path("/tmp"), "sess-1", "hi", "telegram")
        cmd = mock.call_args[0][1]
        assert cmd[0] == "claude"
        assert "--model" in cmd and "opus" in cmd

        # Unset CLAUDE_MODEL → --model omitted (claude CLI picks its default).
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock2:
            await ClaudeBackend().run(Path("/tmp"), "sess-1", "hi", "telegram")
        assert "--model" not in mock2.call_args[0][1]

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

    async def test_effort_flag_only_under_launcher(self, monkeypatch):
        """OLLAMA_EFFORT must not reach native argv: native inherits the CLI's
        own settings.json effortLevel, and a flag here would override it."""
        output = _cli_output()
        monkeypatch.setenv("OLLAMA_EFFORT", "max")
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await ClaudeBackend().run(Path("/tmp"), "sess-1", "hi", "telegram")
        assert "--effort" not in mock.call_args[0][1]

    async def test_effort_flag_under_launcher(self, monkeypatch):
        """Ollama mode: the served model differs from the CLI's native provider,
        so the operator's effort setting is passed explicitly."""
        output = _cli_output()
        monkeypatch.setenv("OLLAMA_EFFORT", "max")
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
        assert "--effort" in cmd
        assert cmd[cmd.index("--effort") + 1] == "max"

    async def test_effort_omitted_without_setting(self, monkeypatch):
        """Unset OLLAMA_EFFORT → no --effort even under the launcher."""
        output = _cli_output()
        monkeypatch.delenv("OLLAMA_EFFORT", raising=False)
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await ClaudeBackend(launcher=launcher).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )
        assert "--effort" not in mock.call_args[0][1]

    async def test_effort_level_not_in_claude_set_skipped(self, monkeypatch, caplog):
        """`minimal` is codex-only; claude must skip it rather than pass it to
        the CLI, which would reject the turn."""
        output = _cli_output()
        monkeypatch.setenv("OLLAMA_EFFORT", "minimal")
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ) as mock:
            await ClaudeBackend(launcher=launcher).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )
        assert "--effort" not in mock.call_args[0][1]
        assert "ignoring unknown effort 'minimal'" in caplog.text

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
            cache_write=400,
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
        # Four non-overlapping buckets, each billed at its own rate. This used to
        # be `(model, 150, 200)` — cache reads folded into the prompt figure at
        # the prompt rate, and the 400 cache-*write* tokens dropped on the floor,
        # which understated a measured turn by 39-43%.
        mock_pricing.assert_called_once_with(
            "deepseek-v4-flash:cloud", 100, 200, 50, 400
        )

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
    @pytest.fixture(autouse=True)
    def _reset_backend_env(
        self, clear_backend_env, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        """Force native backend selection so `CLAUDE_MODE=pty` in the dev's
        shell can't reroute these tests to the real `claude-pty` binary.
        Also redirect the projects dir so the new "session JSONL exists?"
        check finds nothing and routes to the new-session branch."""
        self._workspace = tmp_path / "ws"
        self._workspace.mkdir()

    async def test_new_session_injects_codex_handoff(self):
        output = _cli_output(result="new session reply")
        prior_turns = [
            Turn("user", "prior codex msg"),
            Turn("assistant", "prior codex reply"),
        ]
        with (
            patch(
                "claude_on_the_fly.backends.claude.transcript.find_latest_prior_transcript",
                return_value=(prior_turns, "codex"),
            ),
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=output,
            ) as mock,
        ):
            await run(self._workspace, "sess-1", "CURRENT_TEXT", "telegram")

        # No prior session JSONL → backend goes straight to --session-id.
        cmd = mock.call_args_list[0][0][1]
        assert "--session-id" in cmd
        prompt_arg = cmd[-1]
        assert "[Prior conversation via codex" in prompt_arg
        assert "prior codex msg" in prompt_arg
        assert "prior codex reply" in prompt_arg
        assert prompt_arg.endswith("CURRENT_TEXT")

    async def test_new_session_with_no_codex_history_just_uses_prompt(self):
        output = _cli_output(result="new session reply")
        with (
            patch(
                "claude_on_the_fly.backends.claude.transcript.find_latest_prior_transcript",
                return_value=None,
            ),
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=output,
            ) as mock,
        ):
            await run(self._workspace, "sess-2", "JUST_THIS", "telegram")

        cmd = mock.call_args_list[0][0][1]
        assert "[Prior conversation" not in cmd[-1]
        assert cmd[-1] == "JUST_THIS"

    async def test_extractor_exception_falls_through_silently(self):
        output = _cli_output(result="new session reply")
        with (
            patch(
                "claude_on_the_fly.backends.claude.transcript.find_latest_prior_transcript",
                side_effect=RuntimeError("read failed"),
            ),
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=output,
            ) as mock,
        ):
            resp = await run(self._workspace, "sess-3", "TEXT", "telegram")

        # Daemon must keep serving the user even when transcript extraction breaks.
        assert resp.body == "new session reply"
        assert mock.call_args_list[0][0][1][-1] == "TEXT"

    async def test_resume_skips_extractor_when_session_exists(self):
        """When the JSONL is present, backend takes the resume branch and
        never consults the handoff extractor."""
        from claude_on_the_fly.transcript import _workspace_to_claude_hash

        output = _cli_output()
        # Pre-create the session JSONL so the existence check passes.
        session_dir = (
            self._workspace.parent.parent
            / "claude-projects"
            / _workspace_to_claude_hash(self._workspace)
        )
        # Wherever the claude_projects_dir fixture pointed the resolver.
        from claude_on_the_fly import transcript

        session_dir = transcript.claude_projects_dir() / _workspace_to_claude_hash(
            self._workspace
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "sess-4.jsonl").write_text('{"type":"user"}\n')

        with (
            patch(
                "claude_on_the_fly.backends.claude.transcript.find_latest_prior_transcript"
            ) as mock_extract,
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=output,
            ),
        ):
            await run(self._workspace, "sess-4", "hi", "telegram")

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

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(projects_dir.parent)}):
            cmd = ClaudeBackend().takeover_command(workspace, session_uuid)

        assert cmd == f"claude --resume {session_uuid}"

    def test_returns_none_when_no_session_jsonl(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(projects_dir.parent)}):
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

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(projects_dir.parent)}):
            path = ClaudeBackend().session_log_path(workspace, session_uuid)

        assert path == expected

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(projects_dir.parent)}):
            path = ClaudeBackend().session_log_path(workspace, "absent")

        assert path is None


# ---------------------------------------------------------------------------
# ClaudeBackend pty mode
# ---------------------------------------------------------------------------


def _pty_envelope(
    result: str = "pty reply",
    cost: float = 0.05,
    duration_ms: int = 3000,
    model: str = "claude-haiku-4-5-20251001",
    input_tokens: int = 20,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 76208,
    output_tokens: int = 102,
    rate_5h_pct: int = 33,
    rate_5h_resets_at: int = 1779429600,
    rate_7d_pct: int = 33,
    rate_7d_resets_at: int = 1779854400,
    context_pct: int = 19,
    exceeds_200k: bool = False,
    fast_mode: bool = False,
) -> dict:
    """Mirrors the live claude-pty envelope shape we captured during planning."""
    return {
        "type": "result",
        "subtype": "success",
        "result": result,
        "is_error": False,
        "total_cost_usd": cost,
        "duration_ms": duration_ms,
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "output_tokens": output_tokens,
        },
        "modelUsage": {
            model: {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cacheReadInputTokens": cache_read_input_tokens,
                "cacheCreationInputTokens": cache_creation_input_tokens,
                "webSearchRequests": 0,
            }
        },
        "statusline": {
            "rate_limits": {
                "five_hour": {
                    "used_percentage": rate_5h_pct,
                    "resets_at": rate_5h_resets_at,
                },
                "seven_day": {
                    "used_percentage": rate_7d_pct,
                    "resets_at": rate_7d_resets_at,
                },
            },
            "context_window": {"used_percentage": context_pct},
            "exceeds_200k_tokens": exceeds_200k,
            "fast_mode": fast_mode,
        },
    }


class TestClaudeBackendPty:
    async def test_argv_uses_pty_binary_and_drops_p_flags(
        self, tmp_path, claude_projects_dir, codex_sessions_dir, monkeypatch
    ):
        from claude_on_the_fly.transcript import _workspace_to_claude_hash

        # Explicit model so --model is in the argv (it's omitted when unset).
        monkeypatch.setenv("CLAUDE_MODEL", "sonnet")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        # Pre-create the session JSONL so the backend takes the --resume
        # branch; this test asserts on resume-mode argv shape.
        session_dir = claude_projects_dir / _workspace_to_claude_hash(workspace)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "sess-1.jsonl").write_text('{"type":"user"}\n')

        with (
            patch(
                "claude_on_the_fly.backends.claude.resolve_pty_binary",
                return_value="/fake/bin/claude-pty",
            ),
            patch(
                "claude_on_the_fly.backends.claude._exec_pty",
                new_callable=AsyncMock,
                return_value=_pty_envelope(),
            ) as mock,
        ):
            await ClaudeBackend(pty=True).run(workspace, "sess-1", "hi", "telegram")

        cmd = mock.call_args[0][1]
        assert cmd[0] == "/fake/bin/claude-pty"
        assert "-p" not in cmd
        assert "--output-format" not in cmd
        assert "--verbose" not in cmd
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd
        assert "--model" in cmd
        # Healthy resume (session JSONL has content) reuses the persisted
        # system prompt, so --system-prompt is NOT re-sent.
        assert "--system-prompt" not in cmd
        assert "--resume" in cmd
        assert cmd[-1] == "hi"

    async def test_response_carries_rate_limits_and_context_window(self):
        envelope = _pty_envelope(
            rate_5h_pct=73,
            rate_5h_resets_at=1779429600,
            context_pct=42,
            fast_mode=True,
        )
        with (
            patch(
                "claude_on_the_fly.backends.claude.resolve_pty_binary",
                return_value="/fake/bin/claude-pty",
            ),
            patch(
                "claude_on_the_fly.backends.claude._exec_pty",
                new_callable=AsyncMock,
                return_value=envelope,
            ),
        ):
            resp = await ClaudeBackend(pty=True).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )

        assert resp.rate_limits_5h_pct == 73
        assert resp.rate_limits_5h_resets_at == 1779429600
        assert resp.rate_limits_7d_pct == 33
        assert resp.context_window_pct == 42
        assert resp.fast_mode is True
        assert resp.exceeds_200k is False

    async def test_tokens_derived_from_model_usage_not_usage(self):
        """modelUsage carries cross-turn aggregates; usage is last-message-only.

        We sum modelUsage entries — using `usage` would undercount on
        multi-step turns.
        """
        envelope = _pty_envelope(
            input_tokens=10,  # `usage` shows last message only
            output_tokens=51,
            cache_read_input_tokens=0,
        )
        # Override modelUsage to reflect the cross-turn total
        envelope["modelUsage"]["claude-haiku-4-5-20251001"] = {
            "inputTokens": 200,
            "outputTokens": 500,
            "cacheReadInputTokens": 100,
            "cacheCreationInputTokens": 0,
            "webSearchRequests": 0,
        }
        with (
            patch(
                "claude_on_the_fly.backends.claude.resolve_pty_binary",
                return_value="/fake/bin/claude-pty",
            ),
            patch(
                "claude_on_the_fly.backends.claude._exec_pty",
                new_callable=AsyncMock,
                return_value=envelope,
            ),
        ):
            resp = await ClaudeBackend(pty=True).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )

        # 200 inputTokens + 100 cacheReadInputTokens = 300, NOT 10 from `usage`.
        assert resp.tokens_in == 300
        assert resp.tokens_out == 500

    async def test_empty_body_retry_takes_second_envelope_wholesale(self):
        """claude-pty empty-body retry skips _merge_cli_output: pty envelopes have
        no per-tool counts to merge and `usage` is last-message-only."""
        first = _pty_envelope(result="", cost=0.10, duration_ms=1000)
        second = _pty_envelope(result="recovered", cost=0.05, duration_ms=500)

        with (
            patch(
                "claude_on_the_fly.backends.claude.resolve_pty_binary",
                return_value="/fake/bin/claude-pty",
            ),
            patch(
                "claude_on_the_fly.backends.claude._exec_pty",
                new_callable=AsyncMock,
                side_effect=[first, second],
            ),
            patch(
                "claude_on_the_fly.agent._merge_cli_output",
            ) as mock_merge,
        ):
            resp = await ClaudeBackend(pty=True).run(
                Path("/tmp"), "sess-1", "hi", "telegram"
            )

        mock_merge.assert_not_called()
        assert resp.body == "recovered"
        # Cost/duration come from the second envelope, not summed.
        assert resp.cost == 0.05
        assert resp.duration == 0.5

    async def test_native_response_has_no_rate_limits(self):
        """Regression: native (non-pty) backend leaves pty-only fields as None/False."""
        output = _cli_output()
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=output,
        ):
            resp = await ClaudeBackend().run(Path("/tmp"), "sess-1", "hi", "telegram")

        assert resp.rate_limits_5h_pct is None
        assert resp.rate_limits_5h_resets_at is None
        assert resp.context_window_pct is None
        assert resp.fast_mode is False
        assert resp.exceeds_200k is False

    def test_launcher_and_pty_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            ClaudeBackend(launcher=OllamaLauncher(model="qwen"), pty=True)


class TestResumeSystemPrompt:
    """--system-prompt is attached only when (re-)establishing a session.

    A healthy resume reuses the prompt claude persisted into the session, so
    re-sending it every turn is wasted tokens. But the session file merely
    existing isn't proof the session was established — a failed first turn can
    leave it empty — so an empty/absent session must re-supply the prompt or
    the agent would run prompt-less.
    """

    async def test_missing_session_creates_with_system_prompt(
        self, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=_cli_output(),
        ) as mock:
            await ClaudeBackend().run(workspace, "sess-missing", "hi", "telegram")
        cmd = mock.call_args_list[0][0][1]
        assert "--session-id" in cmd
        assert "--system-prompt" in cmd

    async def test_empty_session_resumes_with_system_prompt(
        self, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        from claude_on_the_fly.transcript import _workspace_to_claude_hash

        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_dir = claude_projects_dir / _workspace_to_claude_hash(workspace)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "sess-empty.jsonl").write_text("")  # exists, no content

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=_cli_output(),
        ) as mock:
            await ClaudeBackend().run(workspace, "sess-empty", "hi", "telegram")
        cmd = mock.call_args_list[0][0][1]
        assert "--resume" in cmd
        assert "--system-prompt" in cmd

    async def test_content_session_resumes_without_system_prompt(
        self, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        from claude_on_the_fly.transcript import _workspace_to_claude_hash

        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_dir = claude_projects_dir / _workspace_to_claude_hash(workspace)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "sess-real.jsonl").write_text('{"type":"user"}\n')

        with patch(
            "claude_on_the_fly.agent._exec",
            new_callable=AsyncMock,
            return_value=_cli_output(),
        ) as mock:
            await ClaudeBackend().run(workspace, "sess-real", "hi", "telegram")
        cmd = mock.call_args_list[0][0][1]
        assert "--resume" in cmd
        assert "--system-prompt" not in cmd


class TestHandoffByPlatform:
    """A background job is a fresh one-shot, so it must NOT inherit an unrelated
    transcript via the handoff preamble. A cron job is the opposite case: it
    carries a session key precisely so it can resume its own earlier run."""

    async def test_normal_platform_forwards_handoff(
        self, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with (
            patch(
                "claude_on_the_fly.transcript.prepend_latest_handoff",
                return_value="hi",
            ) as handoff,
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=_cli_output(),
            ),
        ):
            await ClaudeBackend().run(workspace, "sess-new", "hi", "telegram")
        handoff.assert_called_once()

    async def test_jobs_platform_skips_handoff(
        self, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with (
            patch(
                "claude_on_the_fly.transcript.prepend_latest_handoff",
            ) as handoff,
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=_cli_output(),
            ),
        ):
            await ClaudeBackend().run(workspace, "sess-new", "hi", "jobs")
        handoff.assert_not_called()

    async def test_cron_platform_forwards_handoff(
        self, tmp_path, claude_projects_dir, codex_sessions_dir
    ):
        """A keyed cron job resumes the session its earlier run left behind, so
        suppressing the preamble here would throw away the continuity the key
        exists to provide."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with (
            patch(
                "claude_on_the_fly.transcript.prepend_latest_handoff",
                return_value="hi",
            ) as handoff,
            patch(
                "claude_on_the_fly.agent._exec",
                new_callable=AsyncMock,
                return_value=_cli_output(),
            ),
        ):
            await ClaudeBackend().run(workspace, "sess-new", "hi", "cron")
        handoff.assert_called_once()


# ---------------------------------------------------------------------------
# resolve_pty_binary
# ---------------------------------------------------------------------------


class TestResolvePtyBinary:
    def test_prefers_path_when_present(self, monkeypatch):
        from claude_on_the_fly.backends.claude import resolve_pty_binary

        monkeypatch.setattr(
            "claude_on_the_fly.backends.claude.shutil.which",
            lambda name: "/usr/local/bin/claude-pty" if name == "claude-pty" else None,
        )
        assert resolve_pty_binary() == "/usr/local/bin/claude-pty"

    def test_falls_back_to_install_home(self, tmp_path, monkeypatch):
        from claude_on_the_fly.backends.claude import resolve_pty_binary

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pty = bin_dir / "claude-pty"
        pty.write_text("#!/bin/sh\n")
        pty.chmod(0o755)

        monkeypatch.setattr(
            "claude_on_the_fly.backends.claude.shutil.which", lambda _: None
        )
        monkeypatch.setenv("CLAUDE_INTERACTIVE_P_HOME", str(tmp_path))
        assert resolve_pty_binary() == str(pty)

    def test_returns_none_when_missing(self, monkeypatch, tmp_path):
        from claude_on_the_fly.backends.claude import resolve_pty_binary

        monkeypatch.setattr(
            "claude_on_the_fly.backends.claude.shutil.which", lambda _: None
        )
        monkeypatch.setenv("CLAUDE_INTERACTIVE_P_HOME", str(tmp_path / "nowhere"))
        assert resolve_pty_binary() is None


# ---------------------------------------------------------------------------
# Response pty-derived footer formatting
# ---------------------------------------------------------------------------


class TestResponsePtyFooter:
    def test_appends_context_window_pct(self):
        r = Response(body="x", cost=0.01, model="haiku", context_window_pct=42)
        stats = r.format_stats()
        assert "ctx 42%" in stats

    def test_skips_5h_below_threshold(self):
        r = Response(
            body="x",
            cost=0.01,
            model="haiku",
            rate_limits_5h_pct=12,
            rate_limits_5h_resets_at=1779429600,
        )
        assert "5h" not in r.format_stats()

    def test_includes_5h_above_threshold_with_reset_time(self):
        r = Response(
            body="x",
            cost=0.01,
            model="haiku",
            rate_limits_5h_pct=73,
            rate_limits_5h_resets_at=1779429600,
        )
        stats = r.format_stats()
        assert "5h 73%" in stats
        # resets_at -> HH:MM, format depends on local tz; just check the arrow.
        assert "→" in stats


# ---------------------------------------------------------------------------
# CLAUDE_MODE=pty factory wiring
# ---------------------------------------------------------------------------


class TestPtyModeFactory:
    def test_claude_pty_mode(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "pty")
        with patch(
            "claude_on_the_fly.backends.claude.resolve_pty_binary",
            return_value="/fake/bin/claude-pty",
        ):
            backend = get_backend()
        assert isinstance(backend, ClaudeBackend)
        assert backend.pty is True
        assert backend.launcher is None

    def test_pty_mode_missing_binary_raises(self, clear_backend_env, monkeypatch):
        """Defense in depth: ctor raises if the binary vanished after preflight."""
        monkeypatch.setenv("CLAUDE_MODE", "pty")
        with (
            patch(
                "claude_on_the_fly.backends.claude.resolve_pty_binary",
                return_value=None,
            ),
            pytest.raises(RuntimeError, match="claude-pty binary not found"),
        ):
            get_backend()

    def test_unknown_mode_message_lists_pty(self, clear_backend_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODE", "garbage")
        with pytest.raises(ValueError, match="pty"):
            get_backend()


class TestResolveSessionLog:
    """resolve_session_log finds a job's log in whichever backend wrote it,
    independent of the current env backend (daemon may run codex, TUI claude)."""

    def test_returns_none_when_no_backend_has_it(
        self, claude_projects_dir, codex_sessions_dir, tmp_path
    ) -> None:
        from claude_on_the_fly.agent import resolve_session_log

        assert resolve_session_log(tmp_path / "ws", "missing-uuid") is None

    def test_finds_claude_session_even_when_env_is_codex(
        self,
        claude_projects_dir,
        codex_sessions_dir,
        tmp_path,
        monkeypatch,
    ) -> None:
        from claude_on_the_fly.agent import resolve_session_log
        from claude_on_the_fly.backends.claude import _workspace_to_claude_hash

        ws = tmp_path / "ws"
        proj = claude_projects_dir / _workspace_to_claude_hash(ws)
        proj.mkdir(parents=True)
        log = proj / "uuid-x.jsonl"
        log.write_text('{"type":"x"}\n')

        # Env points at codex, but the session was written by claude — still found.
        monkeypatch.setenv("AGENT_BACKEND", "codex")
        assert resolve_session_log(ws, "uuid-x") == log


# ---------------------------------------------------------------------------
# _kill_process_tree — orphan-safe reap
# ---------------------------------------------------------------------------


class TestKillProcessTree:
    async def test_reaps_grandchild(self):
        import os

        from claude_on_the_fly.agent import _kill_process_tree

        # A shell (session leader) backgrounds a grandchild sleep and prints its
        # pid. Job control is off in a script, so the sleep shares the shell's
        # process group — killpg must take it down with the shell.
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            "sleep 300 & echo $!; wait",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
        grandchild = int(line.strip())
        os.kill(grandchild, 0)  # alive — raises if not

        await _kill_process_tree(proc)

        for _ in range(30):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("grandchild survived the process-tree kill")

    async def test_noop_on_exited_process(self):
        from claude_on_the_fly.agent import _kill_process_tree

        proc = await asyncio.create_subprocess_exec(
            "true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await proc.wait()
        # Already exited — must not raise.
        await _kill_process_tree(proc)


# ---------------------------------------------------------------------------
# Backend skill enumeration
# ---------------------------------------------------------------------------


class TestListSkills:
    async def test_probe_parses_and_sorts_init_skills(self, monkeypatch):
        from claude_on_the_fly.backends import claude as claude_mod

        init = (
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "skills": ["review", "babysit", "loop"],
                    "plugins": [{"name": "p", "path": "/x"}, "junk"],
                }
            ).encode()
            + b"\n"
        )
        fake_proc = MagicMock()
        fake_proc.stdout.readline = AsyncMock(return_value=init)
        fake_proc.returncode = 0

        async def fake_exec(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        monkeypatch.setattr(claude_mod.agent, "_kill_process_tree", AsyncMock())

        names, plugins = await claude_mod._probe_skills([], ["claude"])
        assert names == ["babysit", "loop", "review"]
        assert plugins == [{"name": "p", "path": "/x"}]  # non-dict entries dropped

    async def test_probe_ignores_non_init_first_line(self, monkeypatch):
        from claude_on_the_fly.backends import claude as claude_mod

        fake_proc = MagicMock()
        fake_proc.stdout.readline = AsyncMock(return_value=b'{"type":"assistant"}\n')
        fake_proc.returncode = 0

        async def fake_exec(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        monkeypatch.setattr(claude_mod.agent, "_kill_process_tree", AsyncMock())

        assert await claude_mod._probe_skills([], ["claude"]) == ([], [])

    async def test_claude_list_skills_builds_name_description_pairs(self, monkeypatch):
        from claude_on_the_fly.backends import claude as claude_mod

        probe = AsyncMock(return_value=(["a", "b"], []))
        monkeypatch.setattr(claude_mod, "_probe_skills", probe)
        monkeypatch.setattr(
            claude_mod, "_skill_descriptions", lambda plugins: {"a": "first"}
        )
        backend = claude_mod.ClaudeBackend()
        # missing description -> ""; caching now lives in agent.cached_skills.
        assert await backend.list_skills() == [("a", "first"), ("b", "")]

    async def test_claude_descriptions_from_skill_frontmatter(
        self, tmp_path, monkeypatch
    ):
        from claude_on_the_fly.backends import claude as claude_mod

        skill = tmp_path / "plug" / "skills" / "deploy"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: deploy\ndescription: |\n  Ship the\n  release safely\n---\nbody"
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-cfg"))
        out = claude_mod._skill_descriptions(
            [{"name": "gf-ops", "path": str(tmp_path / "plug")}]
        )
        # block scalar folded to one line, keyed both plain and namespaced
        # (plugin skills appear namespaced in the init list).
        assert out["deploy"] == "Ship the release safely"
        assert out["gf-ops:deploy"] == "Ship the release safely"

    async def test_codex_lists_prompt_files(self, tmp_path, monkeypatch):
        from claude_on_the_fly.backends.codex import CodexBackend

        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "deploy.md").write_text(
            "---\ndescription: ship it\n---\nrun the deploy"
        )
        (prompts / "review.md").write_text("review the diff")  # no front-matter -> ""
        (prompts / "notes.txt").write_text("not a prompt")  # non-.md ignored
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        assert await CodexBackend().list_skills() == [
            ("deploy", "ship it"),
            ("review", ""),
        ]

    async def test_codex_empty_without_prompts_dir(self, tmp_path, monkeypatch):
        from claude_on_the_fly.backends.codex import CodexBackend

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # no prompts/ subdir
        assert await CodexBackend().list_skills() == []


# ---------------------------------------------------------------------------
# cached_skills — shared TTL disk cache
# ---------------------------------------------------------------------------


class TestSkillsCacheTtl:
    """Read per call, not bound at import: a module constant could not see a value
    `load_dotenv()` put in the environment afterwards, nor a config edit at all."""

    def test_unset_is_the_default(self, monkeypatch):
        monkeypatch.delenv("SKILLS_CACHE_TTL_SECONDS", raising=False)
        assert agent_mod.skills_cache_ttl() == agent_mod.DEFAULT_SKILLS_CACHE_TTL

    def test_a_configured_value_is_used(self, monkeypatch):
        monkeypatch.setenv("SKILLS_CACHE_TTL_SECONDS", "45")
        assert agent_mod.skills_cache_ttl() == 45.0

    def test_junk_falls_back_and_says_so(self, monkeypatch, caplog):
        """This is a latency optimisation, not something worth refusing to start
        over -- but a typo that silently reverted to an hour would look like a
        working setting."""
        monkeypatch.setenv("SKILLS_CACHE_TTL_SECONDS", "an hour")
        with caplog.at_level("WARNING", logger="claude_on_the_fly.agent"):
            assert agent_mod.skills_cache_ttl() == agent_mod.DEFAULT_SKILLS_CACHE_TTL
        assert "is not a number" in caplog.text


class TestCachedSkills:
    def _reset(self, monkeypatch, tmp_path, ttl):
        from claude_on_the_fly import agent

        monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
        monkeypatch.setattr(agent, "_skills_mem", {})
        monkeypatch.setattr(agent, "skills_cache_ttl", lambda: ttl)
        monkeypatch.setenv("AGENT_BACKEND", "claude")
        return agent

    async def test_caches_within_ttl_and_persists_to_disk(self, tmp_path, monkeypatch):
        agent = self._reset(monkeypatch, tmp_path, 1000.0)
        backend = MagicMock()
        backend.list_skills = AsyncMock(return_value=[("x", "desc")])
        assert await agent.cached_skills(backend) == [("x", "desc")]
        assert await agent.cached_skills(backend) == [("x", "desc")]
        backend.list_skills.assert_awaited_once()  # served from cache 2nd time
        assert (tmp_path / "cache" / "skills-claude.json").is_file()

    async def test_disabled_ttl_probes_every_call(self, tmp_path, monkeypatch):
        agent = self._reset(monkeypatch, tmp_path, 0.0)  # TTL <= 0 disables cache
        backend = MagicMock()
        backend.list_skills = AsyncMock(return_value=[("x", "")])
        await agent.cached_skills(backend)
        await agent.cached_skills(backend)
        assert backend.list_skills.await_count == 2  # probes every call
        assert not (tmp_path / "cache" / "skills-claude.json").exists()  # no disk write

    async def test_force_bypasses_cache_and_reprobes(self, tmp_path, monkeypatch):
        agent = self._reset(monkeypatch, tmp_path, 1000.0)  # long TTL
        backend = MagicMock()
        backend.list_skills = AsyncMock(return_value=[("x", "")])
        await agent.cached_skills(backend)  # populate (fresh) cache
        await agent.cached_skills(backend, force=True)  # startup-warm behaviour
        assert backend.list_skills.await_count == 2  # force re-probes despite TTL

    async def test_loads_from_disk_without_probing(self, tmp_path, monkeypatch):
        agent = self._reset(monkeypatch, tmp_path, 1000.0)  # fresh in-mem (restart)
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "skills-claude.json").write_text(
            json.dumps({"cached_at": time.time(), "skills": [["disk", "fromdisk"]]})
        )
        backend = MagicMock()
        backend.list_skills = AsyncMock(return_value=[("live", "")])
        assert await agent.cached_skills(backend) == [("disk", "fromdisk")]
        backend.list_skills.assert_not_awaited()  # disk hit, no CLI probe


class TestProcessListeners:
    """The seam that lets a worker write down what it will orphan if killed."""

    async def test_exec_announces_the_group_start_and_end(self):
        from claude_on_the_fly.agent import add_process_listener

        seen: list[tuple[int, str, bool]] = []
        proc = _make_proc(0, _ndjson(_result_line(result="hi")))
        proc.pid = 4321

        add_process_listener(lambda *args: seen.append(args))
        try:
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                await _exec(Path("/tmp"), ["claude", "-p", "hi"])
        finally:
            agent_mod._process_listeners.clear()

        # pgid == pid: start_new_session makes the child its own group leader,
        # which is knowable without racing the child's setsid().
        assert seen[0] == (4321, "claude", True)
        assert seen[-1][0] == 4321
        assert seen[-1][2] is False

    async def test_announcement_survives_a_broken_listener(self):
        """A listener writing to a full disk must not take the agent run down."""
        from claude_on_the_fly.agent import add_process_listener

        proc = _make_proc(0, _ndjson(_result_line(result="hi")))
        proc.pid = 4321

        def _explode(pgid, command, running):
            raise OSError("no space left on device")

        add_process_listener(_explode)
        try:
            with patch("asyncio.create_subprocess_exec", return_value=proc):
                result = await _exec(Path("/tmp"), ["claude", "-p", "hi"])
        finally:
            agent_mod._process_listeners.clear()

        assert result["result"] == "hi"

    async def test_already_exited_process_is_still_announced_finished(self):
        """A naturally exited process is still announced as finished."""
        from claude_on_the_fly.agent import _kill_process_tree, add_process_listener

        seen: list[tuple[int, str, bool]] = []
        proc = MagicMock()
        proc.pid = 777
        proc.returncode = 0  # already exited
        proc.wait = AsyncMock(return_value=0)

        add_process_listener(lambda *args: seen.append(args))
        try:
            await _kill_process_tree(proc)
        finally:
            agent_mod._process_listeners.clear()

        assert seen == [(777, "", False)]


# ---------------------------------------------------------------------------
# Outbox failure modes
# ---------------------------------------------------------------------------


class TestOutboxSurvivesAFilesystemThatSaysNo:
    """Every failure here is skip-and-log, never raise: a broken attachment must
    not cost the user the agent's actual answer."""

    def test_a_file_that_cannot_be_stated_is_skipped(
        self, tmp_path, monkeypatch, caplog
    ):
        outbox = tmp_path / OUTBOX_DIRNAME
        outbox.mkdir()
        vanishing = outbox / "report.txt"
        vanishing.write_text("body")
        real_stat = Path.stat
        seen = 0

        def stat_then_vanish(self, *args, **kwargs):
            # The realistic version of this: the file passes the is_file() check
            # and the agent deletes it before the size is read.
            nonlocal seen
            if self.name == "report.txt":
                seen += 1
                if seen > 1:
                    vanishing.unlink(missing_ok=True)
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat_then_vanish)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.agent"):
            assert collect_outbox(tmp_path) == []
        assert "cannot stat" in "\n".join(r.getMessage() for r in caplog.records)

    def test_an_unwritable_archive_dir_leaves_the_files_where_they_are(
        self, tmp_path, monkeypatch, caplog
    ):
        """Archiving is what stops a delivered file re-sending next turn. If it
        cannot happen, the files must stay put rather than be deleted."""
        outbox = tmp_path / OUTBOX_DIRNAME
        outbox.mkdir()
        delivered = outbox / "report.txt"
        delivered.write_text("body")

        def mkdir_fails(self, *args, **kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "mkdir", mkdir_fails)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.agent"):
            archive_outbox(tmp_path, [delivered])
        assert delivered.is_file(), "the file was lost"
        assert "cannot create archive dir" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    def test_a_move_that_fails_is_logged_per_file(self, tmp_path, monkeypatch, caplog):
        outbox = tmp_path / OUTBOX_DIRNAME
        outbox.mkdir()
        first = outbox / "one.txt"
        first.write_text("a")
        second = outbox / "two.txt"
        second.write_text("b")

        def move_fails(src, _dst):
            if src.endswith("one.txt"):
                raise OSError("cross-device link")

        monkeypatch.setattr(agent_mod.shutil, "move", move_fails)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.agent"):
            archive_outbox(tmp_path, [first, second])
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "failed to archive one.txt" in logged
        # The second file is still attempted: one bad file is not the batch.
        assert "two.txt" not in logged

    def test_archiving_nothing_is_a_no_op(self, tmp_path):
        archive_outbox(tmp_path, [])
        assert not (tmp_path / OUTBOX_DIRNAME).exists()


# ---------------------------------------------------------------------------
# Compaction summaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("compaction", "expected"),
    [
        (
            Compaction(ok=True, pre_tokens=0, post_tokens=0),
            "Compacted the conversation.",
        ),
        (
            # codex publishes no duration, and "in 0s" reads as a suspiciously
            # fast compaction rather than as a missing figure.
            Compaction(ok=True, pre_tokens=120_000, post_tokens=8_000),
            "Compacted the conversation: 120,000 → 8,000 tokens.",
        ),
        (
            Compaction(ok=True, pre_tokens=120_000, post_tokens=8_000, duration=42.4),
            "Compacted the conversation: 120,000 → 8,000 tokens in 42s.",
        ),
    ],
)
def test_compaction_summary(compaction, expected):
    assert compaction.summary() == expected


# ---------------------------------------------------------------------------
# Process listeners
# ---------------------------------------------------------------------------


def test_removing_an_unregistered_listener_is_silent():
    """Callers unregister in a finally without tracking whether they registered."""
    agent_mod.remove_process_listener(lambda *_args: None)


def test_removing_a_registered_listener_stops_the_notifications():
    seen: list[tuple] = []

    def listener(*args):
        seen.append(args)

    agent_mod.add_process_listener(listener)
    agent_mod.remove_process_listener(listener)
    proc = MagicMock()
    proc.pid = 4242
    try:
        agent_mod._announce_process(proc, "claude -p", running=True)
    finally:
        agent_mod._process_listeners.clear()
    assert seen == []


# ---------------------------------------------------------------------------
# Backend key resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"AGENT_BACKEND": "claude", "CLAUDE_MODE": "banana"}, "Unknown CLAUDE_MODE"),
        ({"AGENT_BACKEND": "codex", "CODEX_MODE": "banana"}, "Unknown CODEX_MODE"),
        ({"AGENT_BACKEND": "gemini"}, "Unknown AGENT_BACKEND"),
        (
            {"AGENT_BACKEND": "codex", "CODEX_MODE": "ollama", "OLLAMA_MODEL": ""},
            "requires OLLAMA_MODEL",
        ),
    ],
)
def test_an_unusable_backend_config_fails_loudly(monkeypatch, env, message):
    """The key names the transcript's own format, so a wrong one silently mixes
    two backends' histories. Better to refuse at resolution time."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=message):
        current_backend_key()


def test_codex_native_without_a_model_says_default(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "codex")
    monkeypatch.setenv("CODEX_MODE", "native")
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    assert current_backend_key() == "codex:native:default"


def test_codex_ollama_key_names_the_model(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "codex")
    monkeypatch.setenv("CODEX_MODE", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    assert current_backend_key() == "codex:ollama:qwen3:8b"


def test_claude_pty_key_carries_the_model(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "claude")
    monkeypatch.setenv("CLAUDE_MODE", "pty")
    monkeypatch.setenv("CLAUDE_MODEL", "opus")
    assert current_backend_key() == "claude:pty:opus"


# ---------------------------------------------------------------------------
# Compaction dispatch
# ---------------------------------------------------------------------------


async def test_compact_returns_none_when_the_backend_cannot_do_it(
    monkeypatch, tmp_path
):
    """codex has no compaction, so the caller gets None rather than an error, and
    reports "not supported" instead of "failed"."""
    backend = MagicMock(spec=[])
    monkeypatch.setattr(agent_mod, "get_backend", lambda: backend)
    assert await agent_mod.compact(tmp_path, "session-uuid") is None


async def test_compact_delegates_when_the_backend_supports_it(monkeypatch, tmp_path):
    expected = Compaction(ok=True, pre_tokens=10, post_tokens=1)
    backend = MagicMock()
    backend.compact = AsyncMock(return_value=expected)
    monkeypatch.setattr(agent_mod, "get_backend", lambda: backend)
    assert await agent_mod.compact(tmp_path, "session-uuid", timeout=5) is expected
    backend.compact.assert_awaited_once_with(tmp_path, "session-uuid", timeout=5)


# ---------------------------------------------------------------------------
# Process teardown races
# ---------------------------------------------------------------------------


async def test_a_process_group_that_is_already_gone_is_not_an_error(monkeypatch):
    """`_kill_process_tree` runs from a finally on the abort path, so it races a
    natural exit by construction. A raise here would replace the user's abort with
    a traceback."""
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)

    monkeypatch.setattr(agent_mod.os, "getpgid", lambda _pid: 4242)

    def already_reaped(_pgid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(agent_mod.os, "killpg", already_reaped)
    await agent_mod._kill_process_tree(proc)
    proc.kill.assert_not_called()


async def test_a_pgid_lookup_that_fails_falls_back_to_a_plain_kill(monkeypatch):
    """A failed process-group kill falls back to the direct child."""
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)

    def no_such_group(_pid, _signal):
        raise OSError("no such process")

    monkeypatch.setattr(agent_mod.os, "killpg", no_such_group)
    await agent_mod._kill_process_tree(proc)
    proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------


def test_unparseable_front_matter_is_treated_as_absent():
    """A hand-edited skill file with a broken block must not take the daemon down
    on startup; the skill just loses its description."""
    assert agent_mod.parse_frontmatter("---\ndescription: [unclosed\n---\nbody") == {}


def test_front_matter_that_is_not_a_mapping_is_ignored():
    assert agent_mod.parse_frontmatter("---\n- just\n- a list\n---\nbody") == {}


def test_front_matter_is_parsed_as_real_yaml():
    parsed = agent_mod.parse_frontmatter("---\ndescription: |\n  two\n  lines\n---\nx")
    assert parsed["description"] == "two\nlines"


# ---------------------------------------------------------------------------
# Suggestions block stripping
# ---------------------------------------------------------------------------


def test_strip_suggestions_leaves_a_plain_body_untouched():
    assert agent_mod.strip_suggestions_blocks("plain reply") == "plain reply"


def test_strip_suggestions_removes_the_block_keeps_the_text():
    assert (
        agent_mod.strip_suggestions_blocks('a\n\n<suggestions>["x?"]</suggestions>')
        == "a"
    )


def test_strip_suggestions_removes_every_block():
    assert (
        agent_mod.strip_suggestions_blocks(
            'a <suggestions>["x?"]</suggestions> b <suggestions>["y?"]</suggestions>'
        )
        == "a  b"
    )


def test_strip_suggestions_removes_the_wrapping_fence_pair():
    assert (
        agent_mod.strip_suggestions_blocks(
            'answer\n```json\n<suggestions>["x?"]</suggestions>\n```'
        )
        == "answer"
    )


def test_strip_suggestions_a_block_only_body_is_empty():
    assert agent_mod.strip_suggestions_blocks('<suggestions>["x?"]</suggestions>') == ""


# ---------------------------------------------------------------------------
# Skills cache
# ---------------------------------------------------------------------------


class TestSkillsCache:
    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Both layers. Leaving the on-disk one behind makes the next test in this
        class read a warm cache and never reach the code it is aiming at."""
        cache_dir = agent_mod.DATA_DIR / "cache"

        def wipe():
            agent_mod._skills_mem.clear()
            for stale in cache_dir.glob("skills-*.json"):
                stale.unlink()

        wipe()
        yield
        wipe()

    async def test_concurrent_misses_probe_the_cli_once(self, monkeypatch):
        """Probing spawns the CLI (~0.8s), so a picker sending a query per
        keystroke must not turn into a queue of CLI launches."""
        probes = 0

        class SlowBackend:
            async def list_skills(self):
                nonlocal probes
                probes += 1
                await asyncio.sleep(0.05)
                return [("a", "first")]

        backend = SlowBackend()
        results = await asyncio.gather(
            *(agent_mod.cached_skills(backend) for _ in range(5))
        )
        assert probes == 1, f"probed {probes} times"
        assert all(r == [("a", "first")] for r in results)

    async def test_a_cache_that_cannot_be_written_still_returns_the_skills(
        self, monkeypatch, caplog
    ):
        """The disk layer is a speed-up, not the answer. A read-only DATA_DIR must
        cost latency, not the feature."""

        class Backend:
            async def list_skills(self):
                return [("a", "first")]

        def write_fails(self, *_args, **_kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "write_text", write_fails)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.agent"):
            assert await agent_mod.cached_skills(Backend()) == [("a", "first")]
        assert "skills cache: write failed" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_the_cache_can_be_turned_off(self, monkeypatch):
        probes = 0

        class Backend:
            async def list_skills(self):
                nonlocal probes
                probes += 1
                return []

        monkeypatch.setattr(agent_mod, "skills_cache_ttl", lambda: 0)
        backend = Backend()
        await agent_mod.cached_skills(backend)
        await agent_mod.cached_skills(backend)
        assert probes == 2


# ---------------------------------------------------------------------------
# Cross-backend session log lookup
# ---------------------------------------------------------------------------


def test_a_backend_that_raises_while_looking_is_skipped_not_fatal(
    monkeypatch, tmp_path
):
    """The TUI calls this for whichever backend ran the job, which may not be the
    one this process is configured for. A backend whose store is missing or
    unreadable must not stop the next one from being tried."""
    from claude_on_the_fly.backends import claude as claude_mod

    def explode(self, _workspace, _uuid):
        raise RuntimeError("no store on this machine")

    monkeypatch.setattr(claude_mod.ClaudeBackend, "session_log_path", explode)
    # codex is tried after claude and finds nothing here, so the answer is None
    # rather than a propagated RuntimeError.
    assert agent_mod.resolve_session_log(tmp_path, "some-uuid") is None


# ---------------------------------------------------------------------------
# Draining a subprocess's streams
# ---------------------------------------------------------------------------


class TestStreamsAreDrainedToEof:
    """These use a real subprocess on purpose.

    The bug they exist for is a property of `asyncio.StreamReader.read(n)`:
    it returns as soon as any byte is buffered, so a collector built on one
    `read(cap + 1)` keeps only whatever the child flushed first. A test double
    that hands back its whole output in one call cannot reproduce that, which
    is how a single-`read` collector passed a suite at 100% coverage while
    truncating every codex turn to its opening JSONL event.
    """

    @staticmethod
    async def _spawn(script: str) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    # Three writes separated by real gaps: the first read can only see chunk0.
    _CHUNKED = (
        "import sys, time\n"
        "for i in range(3):\n"
        "    sys.{stream}.write('chunk%d\\n' % i)\n"
        "    sys.{stream}.flush()\n"
        "    time.sleep(0.05)\n"
    )

    async def test_stdout_written_in_several_chunks_arrives_whole(self):
        proc = await self._spawn(self._CHUNKED.format(stream="stdout"))
        stdout, _stderr = await agent_mod.communicate_capped(proc)
        assert stdout == b"chunk0\nchunk1\nchunk2\n"

    async def test_stderr_written_in_several_chunks_arrives_whole(self):
        proc = await self._spawn(self._CHUNKED.format(stream="stderr"))
        _stdout, stderr = await agent_mod.communicate_capped(proc)
        assert stderr == b"chunk0\nchunk1\nchunk2\n"

    async def test_the_helper_itself_reads_past_the_first_chunk(self):
        """`_consume` drains stderr through this directly, so pin it here too."""
        proc = await self._spawn(self._CHUNKED.format(stream="stderr"))
        assert proc.stderr is not None
        assert await agent_mod._read_to_eof_capped(proc.stderr) == (
            b"chunk0\nchunk1\nchunk2\n"
        )
        await proc.wait()

    async def test_a_child_that_outruns_the_cap_is_killed(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "MAX_AGENT_OUTPUT_BYTES", 256)
        killed: list[object] = []

        async def spy(proc):
            killed.append(proc)

        monkeypatch.setattr(agent_mod, "_kill_process_tree", spy)
        proc = await self._spawn("import sys; sys.stdout.write('x' * 5000)")
        with pytest.raises(agent_mod.AgentOutputLimitError):
            await agent_mod.communicate_capped(proc)
        assert killed == [proc]
        # The spy stood in for the real kill, so reap the child here. It may
        # have finished writing on its own already.
        if proc.returncode is None:
            proc.kill()
        await proc.wait()

    async def test_a_child_that_stays_under_the_cap_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "MAX_AGENT_OUTPUT_BYTES", 4096)
        killed: list[object] = []

        async def spy(proc):
            killed.append(proc)

        monkeypatch.setattr(agent_mod, "_kill_process_tree", spy)
        proc = await self._spawn("import sys; sys.stdout.write('x' * 100)")
        stdout, _stderr = await agent_mod.communicate_capped(proc)
        assert len(stdout) == 100
        assert killed == []

    async def test_a_stream_that_only_blows_the_cap_after_the_other_closes(
        self, monkeypatch
    ):
        """stdout finishes small and clean; stderr goes over afterwards.

        The early guard only inspects whichever stream completed first, so this
        is the ordering that reaches the check after both have been gathered.
        """
        monkeypatch.setattr(agent_mod, "MAX_AGENT_OUTPUT_BYTES", 256)
        killed: list[object] = []

        async def spy(proc):
            killed.append(proc)

        monkeypatch.setattr(agent_mod, "_kill_process_tree", spy)

        proc = MagicMock()
        proc.stdout = asyncio.StreamReader()
        proc.stderr = asyncio.StreamReader()
        proc.stdout.feed_data(b"done")
        proc.stdout.feed_eof()

        async def blow_the_cap_late():
            # Long enough that the first wait returns with only stdout done;
            # a bare sleep(0) lets both land in the same batch.
            await asyncio.sleep(0.05)
            proc.stderr.feed_data(b"x" * 5000)
            proc.stderr.feed_eof()

        asyncio.get_running_loop().create_task(blow_the_cap_late())
        with pytest.raises(agent_mod.AgentOutputLimitError):
            await agent_mod.communicate_capped(proc)
        assert killed == [proc]

    async def test_a_proc_without_real_streams_is_still_capped(self, monkeypatch):
        """Doubles and embedders that expose only `communicate()`."""
        monkeypatch.setattr(agent_mod, "MAX_AGENT_OUTPUT_BYTES", 256)
        killed: list[object] = []

        async def spy(proc):
            killed.append(proc)

        monkeypatch.setattr(agent_mod, "_kill_process_tree", spy)

        proc = MagicMock()
        proc.stdout = None
        proc.stderr = None
        proc.communicate = AsyncMock(return_value=(b"x" * 5000, b""))
        with pytest.raises(agent_mod.AgentOutputLimitError):
            await agent_mod.communicate_capped(proc)
        assert killed == [proc]


class TestConsumeRefusesAnOversizedTurn:
    """`_consume` caps each stream separately, and says which one it was."""

    async def test_stdout_past_the_cap_is_refused(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "MAX_AGENT_OUTPUT_BYTES", 64)
        proc = _make_proc(0, b"x" * 5000 + b"\n")
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(agent_mod.AgentOutputLimitError, match="stdout"),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"])

    async def test_stderr_past_the_cap_is_refused(self, monkeypatch):
        # Roomy enough that the (small) stdout stream is not what trips it.
        monkeypatch.setattr(agent_mod, "MAX_AGENT_OUTPUT_BYTES", 1024)
        proc = _make_proc(0, _ndjson(_result_line(result="hi")), stderr=b"x" * 5000)
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            pytest.raises(agent_mod.AgentOutputLimitError, match="stderr"),
        ):
            await _exec(Path("/tmp"), ["claude", "-p", "hi"])


class TestAttachmentHandoffRefusesWhatItCannotVouchFor:
    """The jail boundary. Every check re-runs on the descriptor that supplies
    the bytes, because the path the caller validated and the file it ends up
    reading are not guaranteed to be the same thing."""

    def test_an_outbox_entry_that_cannot_be_inspected_is_skipped_loudly(
        self, tmp_path, caplog
    ):
        workspace = tmp_path / "ws"
        outbox = workspace / OUTBOX_DIRNAME
        outbox.mkdir(parents=True)
        (outbox / "good.txt").write_text("fine")
        (outbox / "gone.txt").write_text("vanishing")
        real_lstat = Path.lstat

        def flaky(self):
            if self.name == "gone.txt":
                raise OSError("vanished")
            return real_lstat(self)

        with (
            patch.object(Path, "lstat", flaky),
            caplog.at_level("WARNING"),
        ):
            collected = collect_outbox(workspace)

        assert [p.name for p in collected] == ["good.txt"]
        assert "cannot inspect gone.txt" in caplog.text

    def test_a_missing_outbox_is_not_an_error(self, tmp_path):
        assert collect_outbox(tmp_path / "no-such-ws") == []

    def test_an_outbox_that_is_not_a_directory_is_ignored(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / OUTBOX_DIRNAME).write_text("a file, not a directory")
        assert collect_outbox(workspace) == []

    def test_reading_a_non_regular_file_is_refused(self, tmp_path):
        """`os.open` on a fifo would block on a reader; the fstat check is what
        stops a non-file ever getting that far."""
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        flags = os.O_RDONLY | os.O_NONBLOCK
        fd = os.open(fifo, flags)
        try:
            with (
                patch.object(agent_mod.os, "open", return_value=os.dup(fd)),
                pytest.raises(OSError, match="not a regular file"),
            ):
                read_attachment(fifo)
        finally:
            os.close(fd)

    def test_reading_a_file_over_the_cap_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_mod, "MAX_ATTACHMENT_BYTES", 16)
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 64)
        with pytest.raises(ValueError, match="exceeds 16 bytes"):
            read_attachment(big)

    def test_a_file_that_grows_past_the_cap_while_being_read_is_refused(
        self, tmp_path, monkeypatch
    ):
        """The size check is a fast reject, not the guarantee. The running
        total is what actually bounds the read."""
        monkeypatch.setattr(agent_mod, "MAX_ATTACHMENT_BYTES", 32)
        path = tmp_path / "grows.bin"
        path.write_bytes(b"x" * 8)
        real_fstat = agent_mod.os.fstat

        class _SmallStat:
            def __init__(self, real):
                self.st_mode = real.st_mode
                self.st_size = 1

        monkeypatch.setattr(
            agent_mod.os, "fstat", lambda fd: _SmallStat(real_fstat(fd))
        )
        monkeypatch.setattr(agent_mod.os, "read", lambda fd, n: b"y" * 64)
        with pytest.raises(ValueError, match="exceeds 32 bytes"):
            read_attachment(path)

    def test_writing_more_than_the_cap_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_mod, "MAX_ATTACHMENT_BYTES", 8)
        with pytest.raises(ValueError, match="exceeds 8 bytes"):
            agent_mod.write_attachment(tmp_path / "out.bin", b"x" * 9)

    def test_a_failed_write_leaves_no_temp_file_behind(self, tmp_path, monkeypatch):
        dest = tmp_path / "out.bin"

        def boom(_src, _dst):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(agent_mod.os, "replace", boom)
        with pytest.raises(OSError, match="read-only"):
            agent_mod.write_attachment(dest, b"data")
        assert list(tmp_path.iterdir()) == []

    def test_installing_a_non_regular_download_is_refused(self, tmp_path):
        source = tmp_path / "a-directory"
        source.mkdir()
        with pytest.raises(OSError, match="not a regular file"):
            agent_mod.install_download(source, tmp_path / "dest")

    def test_installing_an_oversized_download_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_mod, "MAX_ATTACHMENT_BYTES", 4)
        source = tmp_path / "big.bin"
        source.write_bytes(b"x" * 16)
        with pytest.raises(ValueError, match="exceeds 4 bytes"):
            agent_mod.install_download(source, tmp_path / "dest")
        assert source.exists()
