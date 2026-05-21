"""Tests for symphony.watch — JSONL tail loop and event formatter."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from claude_on_the_fly.symphony.watch import (
    _first_line_with_count,
    _format_tool_args,
    _indent_body,
    _short_ts,
    _truncate,
    format_event,
    tail,
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


class TestShortTs:
    def test_iso_ts(self) -> None:
        assert _short_ts("2026-05-21T04:53:36.096Z") == "04:53:36"

    def test_missing_ts_blank(self) -> None:
        assert _short_ts(None) == "        "

    def test_short_string_blank(self) -> None:
        assert _short_ts("nope") == "        "


class TestTruncate:
    def test_under_limit(self) -> None:
        assert _truncate("hi", 10) == "hi"

    def test_over_limit_ellipses(self) -> None:
        assert _truncate("abcdef", 4) == "abc…"


class TestFirstLineWithCount:
    def test_single_line(self) -> None:
        assert _first_line_with_count("hello") == "hello"

    def test_multi_line_shows_count(self) -> None:
        out = _first_line_with_count("first\nsecond\nthird")
        assert out.startswith("first")
        assert "+2 lines" in out

    def test_blank_lines_ignored_in_count(self) -> None:
        out = _first_line_with_count("first\n\n\nsecond")
        assert out.startswith("first")
        assert "+1 lines" in out


class TestIndentBody:
    def test_indents_each_line(self) -> None:
        out = _indent_body("a\nb")
        assert out == "    a\n    b"

    def test_truncates_long_text(self) -> None:
        out = _indent_body("x" * 1000, max_chars=50)
        assert out.endswith("…")
        assert len(out) <= 60  # 4 indent + 50 cap + ellipsis


class TestFormatToolArgs:
    def test_file_path_preferred(self) -> None:
        # Now returns the raw value (no `key=` prefix) — caller decides framing.
        out = _format_tool_args({"file_path": "/tmp/x.py", "noise": 1})
        assert out == "/tmp/x.py"

    def test_command_picked_up(self) -> None:
        out = _format_tool_args({"command": "pytest -x", "timeout": 10})
        assert out == "pytest -x"

    def test_multi_line_command_collapses(self) -> None:
        out = _format_tool_args({"command": "a\nb\nc"})
        assert out.startswith("a")
        assert "+2 lines" in out

    def test_fallback_to_first_key(self) -> None:
        out = _format_tool_args({"weird_key": "value"})
        assert out == "weird_key=…"

    def test_empty(self) -> None:
        assert _format_tool_args({}) == ""


# ---------------------------------------------------------------------------
# format_event — branches per event type
# ---------------------------------------------------------------------------


class TestFormatEvent:
    def test_user_prompt_string(self) -> None:
        raw = {
            "type": "user",
            "timestamp": "2026-05-21T04:53:36.000Z",
            "message": {"content": "Implement feature X"},
        }
        out = format_event(raw)
        assert out is not None
        assert "04:53:36" in out
        assert "USER" in out
        assert "Implement feature X" in out
        assert "cyan" in out  # rule is cyan

    def test_user_tool_result_array(self) -> None:
        raw = {
            "type": "user",
            "timestamp": "2026-05-21T04:53:36.000Z",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "(Bash completed)"},
                ]
            },
        }
        out = format_event(raw)
        assert out is not None
        assert "◂ result" in out
        assert "(Bash completed)" in out
        assert "[dim]" in out

    def test_assistant_text(self) -> None:
        raw = {
            "type": "assistant",
            "timestamp": "2026-05-21T04:53:36.000Z",
            "message": {"content": [{"type": "text", "text": "Tests pass."}]},
        }
        out = format_event(raw)
        assert out is not None
        assert "ASSISTANT" in out
        assert "Tests pass." in out
        assert "green" in out  # rule is green

    def test_assistant_thinking_first_line_only(self) -> None:
        raw = {
            "type": "assistant",
            "timestamp": "2026-05-21T04:53:36.000Z",
            "message": {
                "content": [{"type": "thinking", "thinking": "line 1\nline 2\nline 3"}]
            },
        }
        out = format_event(raw)
        assert out is not None
        assert "▸ thinking" in out
        assert "line 1" in out
        assert "line 2" not in out
        assert "dim italic" in out

    def test_assistant_tool_use(self) -> None:
        raw = {
            "type": "assistant",
            "timestamp": "2026-05-21T04:53:36.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "pytest -x"},
                    }
                ]
            },
        }
        out = format_event(raw)
        assert out is not None
        assert "[yellow]Bash[/yellow]" in out
        assert "pytest -x" in out

    def test_assistant_mixed_blocks(self) -> None:
        raw = {
            "type": "assistant",
            "timestamp": "2026-05-21T04:53:36.000Z",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "Result"},
                ]
            },
        }
        out = format_event(raw)
        assert out is not None
        assert "▸ thinking" in out
        assert "ASSISTANT" in out

    def test_result_event(self) -> None:
        raw = {
            "type": "result",
            "timestamp": "2026-05-21T04:55:00.000Z",
            "total_cost_usd": 0.0124,
            "duration_ms": 8300,
            "result": "Done.",
        }
        out = format_event(raw)
        assert out is not None
        assert "DONE" in out
        assert "$0.0124" in out
        assert "8.3s" in out
        assert "magenta" in out

    def test_skipped_metadata_types(self) -> None:
        for t in ("ai-title", "attachment", "last-prompt", "pr-link", "system"):
            assert format_event({"type": t}) is None

    def test_unknown_type_returns_none(self) -> None:
        assert format_event({"type": "mystery"}) is None

    def test_non_dict_returns_none(self) -> None:
        assert format_event("not a dict") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# tail — file growth + malformed lines
# ---------------------------------------------------------------------------


class TestTail:
    def test_yields_existing_lines_then_follows_appends(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        f.write_text('{"type": "system"}\n{"type": "user"}\n')

        collected: list[dict] = []
        stop_flag = threading.Event()

        def consumer() -> None:
            for ev in tail(f, poll_s=0.05):
                collected.append(ev)
                if stop_flag.is_set():
                    return

        thread = threading.Thread(target=consumer, daemon=True)
        thread.start()

        # Give it time to drain the existing two lines.
        time.sleep(0.2)
        # Append a third event while the loop is polling.
        with f.open("a") as fp:
            fp.write('{"type": "assistant"}\n')
        time.sleep(0.2)
        stop_flag.set()
        thread.join(timeout=1.0)

        types = [e.get("type") for e in collected]
        assert types[:2] == ["system", "user"]
        assert "assistant" in types

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        f.write_text('{"type": "user"}\nnot-json\n{"type": "assistant"}\n')

        collected: list[dict] = []
        stop_flag = threading.Event()

        def consumer() -> None:
            for ev in tail(f, poll_s=0.05):
                collected.append(ev)
                if len(collected) >= 2 or stop_flag.is_set():
                    return

        thread = threading.Thread(target=consumer, daemon=True)
        thread.start()
        time.sleep(0.2)
        stop_flag.set()
        thread.join(timeout=1.0)

        types = [e.get("type") for e in collected]
        assert types == ["user", "assistant"]

    def test_handles_partial_line(self, tmp_path: Path) -> None:
        """Lines written without a trailing newline aren't yielded yet."""
        f = tmp_path / "session.jsonl"
        f.write_text('{"type": "user"}\n')
        with f.open("a") as fp:
            fp.write('{"type": "assist')  # partial

        collected: list[dict] = []
        stop_flag = threading.Event()

        def consumer() -> None:
            for ev in tail(f, poll_s=0.05):
                collected.append(ev)
                if stop_flag.is_set():
                    return

        thread = threading.Thread(target=consumer, daemon=True)
        thread.start()
        time.sleep(0.2)
        # Complete the partial line.
        with f.open("a") as fp:
            fp.write('ant"}\n')
        time.sleep(0.2)
        stop_flag.set()
        thread.join(timeout=1.0)

        types = [e.get("type") for e in collected]
        assert types == ["user", "assistant"]


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
