"""Tests for cross-backend transcript extraction and handoff formatting."""

from __future__ import annotations

import json
import time
from pathlib import Path

from claude_on_the_fly.transcript import (
    Turn,
    extract_claude,
    extract_codex,
    format_handoff,
)


# ---------------------------------------------------------------------------
# extract_claude
# ---------------------------------------------------------------------------


def _claude_user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _claude_assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _claude_assistant_tooluse() -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "..."},
                {"type": "tool_use", "id": "t", "name": "Read", "input": {}},
            ],
        },
    }


def _claude_user_toolresult() -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t", "content": "..."}],
        },
    }


class TestExtractClaude:
    def test_missing_file_returns_none(self, claude_projects_dir):
        assert extract_claude(Path("/private/tmp/nope"), "uuid-x") is None

    def test_returns_user_and_assistant_text_in_order(
        self, claude_projects_dir, ndjson
    ):
        workspace = Path("/private/tmp/ws-a")
        session_dir = claude_projects_dir / "-private-tmp-ws-a"
        session_dir.mkdir()
        (session_dir / "u1.jsonl").write_bytes(
            ndjson(
                _claude_user("hi there"),
                _claude_assistant_text("hello"),
                _claude_user("again"),
                _claude_assistant_text("yes"),
            )
        )
        turns = extract_claude(workspace, "u1")
        assert turns == [
            Turn("user", "hi there"),
            Turn("assistant", "hello"),
            Turn("user", "again"),
            Turn("assistant", "yes"),
        ]

    def test_skips_tool_use_thinking_and_tool_result(self, claude_projects_dir, ndjson):
        workspace = Path("/private/tmp/ws-b")
        session_dir = claude_projects_dir / "-private-tmp-ws-b"
        session_dir.mkdir()
        (session_dir / "u1.jsonl").write_bytes(
            ndjson(
                _claude_user("real user msg"),
                _claude_assistant_tooluse(),
                _claude_user_toolresult(),
                _claude_assistant_text("final answer"),
            )
        )
        turns = extract_claude(workspace, "u1")
        assert turns == [
            Turn("user", "real user msg"),
            Turn("assistant", "final answer"),
        ]

    def test_picks_first_text_block_per_assistant_message(
        self, claude_projects_dir, ndjson
    ):
        workspace = Path("/private/tmp/ws-c")
        session_dir = claude_projects_dir / "-private-tmp-ws-c"
        session_dir.mkdir()
        msg = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},  # ignored
                ],
            },
        }
        (session_dir / "u1.jsonl").write_bytes(ndjson(msg))
        turns = extract_claude(workspace, "u1")
        assert turns == [Turn("assistant", "first")]

    def test_empty_strings_skipped(self, claude_projects_dir, ndjson):
        workspace = Path("/private/tmp/ws-d")
        session_dir = claude_projects_dir / "-private-tmp-ws-d"
        session_dir.mkdir()
        (session_dir / "u1.jsonl").write_bytes(
            ndjson(
                _claude_user("   "),
                _claude_assistant_text(""),
                _claude_user("real"),
                _claude_assistant_text("answer"),
            )
        )
        turns = extract_claude(workspace, "u1")
        assert turns == [Turn("user", "real"), Turn("assistant", "answer")]

    def test_malformed_lines_tolerated(self, claude_projects_dir):
        workspace = Path("/private/tmp/ws-e")
        session_dir = claude_projects_dir / "-private-tmp-ws-e"
        session_dir.mkdir()
        (session_dir / "u1.jsonl").write_bytes(
            json.dumps(_claude_user("ok")).encode()
            + b"\nnot-json\n"
            + json.dumps(_claude_assistant_text("yes")).encode()
            + b"\n"
        )
        turns = extract_claude(workspace, "u1")
        assert turns == [Turn("user", "ok"), Turn("assistant", "yes")]

    def test_returns_none_when_no_turns_present(self, claude_projects_dir, ndjson):
        workspace = Path("/private/tmp/ws-f")
        session_dir = claude_projects_dir / "-private-tmp-ws-f"
        session_dir.mkdir()
        (session_dir / "u1.jsonl").write_bytes(
            ndjson(_claude_assistant_tooluse(), _claude_user_toolresult())
        )
        turns = extract_claude(workspace, "u1")
        assert turns is None


# ---------------------------------------------------------------------------
# extract_codex
# ---------------------------------------------------------------------------


def _codex_user(text: str) -> dict:
    return {
        "timestamp": "x",
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    }


def _codex_agent(text: str) -> dict:
    return {
        "timestamp": "x",
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text},
    }


def _codex_other(payload_type: str) -> dict:
    return {
        "timestamp": "x",
        "type": "event_msg",
        "payload": {"type": payload_type, "data": "ignored"},
    }


class TestExtractCodex:
    def test_missing_session_file_returns_none(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert extract_codex(workspace, "no-such-uuid") is None

    def test_empty_thread_id_returns_none(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        (workspace / ".codex_sessions").mkdir(parents=True)
        (workspace / ".codex_sessions" / "u1").write_text("   ")
        assert extract_codex(workspace, "u1") is None

    def test_no_matching_rollout_returns_none(self, tmp_path: Path, codex_sessions_dir):
        workspace = tmp_path / "ws"
        (workspace / ".codex_sessions").mkdir(parents=True)
        (workspace / ".codex_sessions" / "u1").write_text("thread-xyz")
        assert extract_codex(workspace, "u1") is None

    def test_extracts_user_and_agent_messages(
        self, tmp_path: Path, codex_sessions_dir, ndjson
    ):
        workspace = tmp_path / "ws"
        (workspace / ".codex_sessions").mkdir(parents=True)
        (workspace / ".codex_sessions" / "u1").write_text("thread-abc")

        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-2026-05-18T12-00-00-thread-abc.jsonl"
        rollout.write_bytes(
            ndjson(
                _codex_user("SYSTEM PROMPT BLOB\n\n---\n\nhi there"),
                _codex_agent("hello"),
                _codex_user("SYSTEM PROMPT BLOB\n\n---\n\nagain"),
                _codex_agent("yes"),
            )
        )
        turns = extract_codex(workspace, "u1")
        assert turns == [
            Turn("user", "hi there"),
            Turn("assistant", "hello"),
            Turn("user", "again"),
            Turn("assistant", "yes"),
        ]

    def test_strips_system_prompt_prefix_with_rsplit_safe_separator(
        self, tmp_path: Path, codex_sessions_dir, ndjson
    ):
        """User text with `---` in it survives the split."""
        workspace = tmp_path / "ws"
        (workspace / ".codex_sessions").mkdir(parents=True)
        (workspace / ".codex_sessions" / "u1").write_text("thread-xx")
        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-2026-05-18T12-00-00-thread-xx.jsonl"
        rollout.write_bytes(
            ndjson(_codex_user("SYS\n\n---\n\nplease consider --- as text"))
        )
        turns = extract_codex(workspace, "u1")
        assert turns == [Turn("user", "please consider --- as text")]

    def test_skips_reasoning_and_other_payload_types(
        self, tmp_path: Path, codex_sessions_dir, ndjson
    ):
        workspace = tmp_path / "ws"
        (workspace / ".codex_sessions").mkdir(parents=True)
        (workspace / ".codex_sessions" / "u1").write_text("thread-yy")
        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-2026-05-18T12-00-00-thread-yy.jsonl"
        rollout.write_bytes(
            ndjson(
                _codex_other("agent_reasoning"),
                _codex_other("token_count"),
                _codex_user("SYS\n\n---\n\nhi"),
                _codex_other("task_started"),
                _codex_agent("hello"),
                _codex_other("task_complete"),
            )
        )
        turns = extract_codex(workspace, "u1")
        assert turns == [Turn("user", "hi"), Turn("assistant", "hello")]

    def test_picks_most_recent_rollout_when_multiple(
        self, tmp_path: Path, codex_sessions_dir, ndjson
    ):
        """Defensive: if two rollouts somehow exist for the same thread, take latest."""
        workspace = tmp_path / "ws"
        (workspace / ".codex_sessions").mkdir(parents=True)
        (workspace / ".codex_sessions" / "u1").write_text("thread-zz")
        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True)
        older = rollout_dir / "rollout-2026-05-18T10-00-00-thread-zz.jsonl"
        newer = rollout_dir / "rollout-2026-05-18T11-00-00-thread-zz.jsonl"
        older.write_bytes(
            ndjson(_codex_user("SYS\n\n---\n\nold"), _codex_agent("old-reply"))
        )
        newer.write_bytes(
            ndjson(_codex_user("SYS\n\n---\n\nnew"), _codex_agent("new-reply"))
        )
        time.sleep(0.01)
        newer.touch()
        turns = extract_codex(workspace, "u1")
        assert turns == [Turn("user", "new"), Turn("assistant", "new-reply")]


# ---------------------------------------------------------------------------
# format_handoff
# ---------------------------------------------------------------------------


class TestFormatHandoff:
    def test_empty_input_returns_empty_string(self):
        assert format_handoff([], from_backend="claude") == ""

    def test_renders_labeled_block(self):
        out = format_handoff(
            [Turn("user", "hi"), Turn("assistant", "hello")], from_backend="codex"
        )
        assert "[Prior conversation via codex, last 2 turn(s)]" in out
        assert "User: hi" in out
        assert "Assistant: hello" in out
        assert out.endswith("[Continue from here]\n\n")

    def test_caps_by_max_turns_keeping_newest(self):
        turns = [Turn("user", f"u{i}") for i in range(30)]
        out = format_handoff(turns, from_backend="claude", max_turns=5)
        assert "u29" in out
        assert "u25" in out
        assert "u24" not in out
        assert "last 5 turn(s)" in out

    def test_caps_by_max_chars_dropping_oldest_first(self):
        turns = [Turn("user", "x" * 40) for _ in range(5)]
        out = format_handoff(turns, from_backend="claude", max_chars=120)
        assert "last 2 turn(s)" in out or "last 3 turn(s)" in out

    def test_zero_budget_returns_empty_string(self):
        out = format_handoff([Turn("user", "hi")], from_backend="claude", max_chars=0)
        assert out == ""


# ---------------------------------------------------------------------------
# prepend_handoff
# ---------------------------------------------------------------------------


class TestPrependHandoff:
    def test_returns_unchanged_when_extractor_returns_none(self, tmp_path: Path):
        from claude_on_the_fly.transcript import prepend_handoff

        out = prepend_handoff(
            tmp_path,
            "sess-1",
            "original prompt",
            from_backend="codex",
            extractor=lambda *_a, **_k: None,
        )
        assert out == "original prompt"

    def test_returns_unchanged_when_extractor_raises(self, tmp_path: Path):
        from claude_on_the_fly.transcript import prepend_handoff

        def boom(*_a, **_k):
            raise RuntimeError("disk fell over")

        out = prepend_handoff(
            tmp_path,
            "sess-1",
            "original prompt",
            from_backend="claude",
            extractor=boom,
        )
        assert out == "original prompt"

    def test_prepends_labeled_preamble_when_extractor_yields_turns(
        self, tmp_path: Path
    ):
        from claude_on_the_fly.transcript import prepend_handoff

        out = prepend_handoff(
            tmp_path,
            "sess-1",
            "USER_TEXT",
            from_backend="claude",
            extractor=lambda *_a, **_k: [
                Turn("user", "earlier"),
                Turn("assistant", "earlier reply"),
            ],
        )
        assert "[Prior conversation via claude" in out
        assert "earlier" in out
        assert out.endswith("USER_TEXT")
