"""Tests for cross-backend transcript extraction and handoff formatting."""

from __future__ import annotations

import json
import time
from pathlib import Path

from unittest.mock import patch

from claude_on_the_fly.transcript import (
    Turn,
    _workspace_to_claude_hash,
    _workspace_to_pi_hash,
    extract_claude,
    extract_codex,
    extract_codex_cumulative_tokens,
    extract_codex_model,
    extract_opencode,
    format_handoff,
    remove_workspace_sessions,
)


# ---------------------------------------------------------------------------
# _workspace_to_claude_hash — must match claude's own scheme byte-for-byte
# ---------------------------------------------------------------------------


class TestWorkspaceToClaudeHash:
    def test_plain_path(self) -> None:
        assert (
            _workspace_to_claude_hash(Path("/Users/me/Workspace"))
            == "-Users-me-Workspace"
        )

    def test_dotted_dir_becomes_double_dash(self) -> None:
        # `/Users/me/.claude-on-the-fly/foo` → `-Users-me--claude-on-the-fly-foo`.
        # The `.` is replaced with `-` just like `/`, producing the double dash
        # where the dot used to sit. Cross-checked against a live claude
        # session directory at ~/.claude/projects/.
        assert (
            _workspace_to_claude_hash(
                Path("/Users/me/.claude-on-the-fly/workspaces/symphony/proj-1")
            )
            == "-Users-me--claude-on-the-fly-workspaces-symphony-proj-1"
        )

    def test_underscore_in_segment_becomes_dash(self) -> None:
        # Sanitized github workspaces look like `hardcoretech_gf-external-api_754`
        # (sanitize_key turns `/` and `#` into `_`). Claude CLI normalises
        # underscores to dashes too, so the hash must match.
        # Cross-checked against:
        # ~/.claude/projects/-Users-me--claude-on-the-fly-workspaces-symphony-github-hardcoretech-gf-external-api-754/
        assert (
            _workspace_to_claude_hash(
                Path(
                    "/Users/me/.claude-on-the-fly/workspaces/symphony/github/"
                    "hardcoretech_gf-external-api_754"
                )
            )
            == "-Users-me--claude-on-the-fly-workspaces-symphony-github-"
            "hardcoretech-gf-external-api-754"
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


class TestExtractCodexModel:
    def test_returns_none_when_no_rollout(self, codex_sessions_dir):
        assert extract_codex_model("nonexistent-thread") is None

    def test_returns_none_when_empty_thread_id(self, codex_sessions_dir):
        assert extract_codex_model("") is None

    def test_extracts_model_from_turn_context(self, codex_sessions_dir, ndjson):
        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-2026-05-18T12-00-00-thread-abc.jsonl"
        rollout.write_bytes(
            ndjson(
                {"type": "session_meta", "payload": {"id": "x"}},
                {
                    "type": "turn_context",
                    "payload": {"model": "gpt-4.1", "provider": "openai"},
                },
                {"type": "event_msg", "payload": {"type": "user_message"}},
            )
        )
        assert extract_codex_model("thread-abc") == "gpt-4.1"

    def test_returns_first_turn_context_model(self, codex_sessions_dir, ndjson):
        """Codex emits turn_context per turn; the first hit is sufficient."""
        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-2026-05-18T12-00-00-thread-x.jsonl"
        rollout.write_bytes(
            ndjson(
                {"type": "turn_context", "payload": {"model": "first-model"}},
                {"type": "turn_context", "payload": {"model": "second-model"}},
            )
        )
        assert extract_codex_model("thread-x") == "first-model"

    def test_skips_non_string_model_values(self, codex_sessions_dir, ndjson):
        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-2026-05-18T12-00-00-thread-y.jsonl"
        rollout.write_bytes(
            ndjson(
                {"type": "turn_context", "payload": {"model": None}},
                {"type": "turn_context", "payload": {"model": ""}},
                {"type": "turn_context", "payload": {"model": "real-model"}},
            )
        )
        assert extract_codex_model("thread-y") == "real-model"


class TestExtractCodexCumulativeTokens:
    def test_returns_none_when_no_rollout(self, codex_sessions_dir):
        assert extract_codex_cumulative_tokens("nonexistent") is None

    def test_returns_latest_total_token_usage(self, codex_sessions_dir, ndjson):
        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-2026-05-18T12-00-00-thread-abc.jsonl"
        rollout.write_bytes(
            ndjson(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "output_tokens": 10,
                                "reasoning_output_tokens": 0,
                            },
                            "last_token_usage": {"input_tokens": 100},
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 300,
                                "output_tokens": 40,
                                "reasoning_output_tokens": 5,
                            },
                            "last_token_usage": {"input_tokens": 200},
                        },
                    },
                },
            )
        )
        out = extract_codex_cumulative_tokens("thread-abc")
        assert out == {
            "input_tokens": 300,
            "output_tokens": 40,
            "reasoning_output_tokens": 5,
        }

    def test_skips_non_dict_total(self, codex_sessions_dir, ndjson):
        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-2026-05-18T12-00-00-thread-x.jsonl"
        rollout.write_bytes(
            ndjson(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": "not a dict"},
                    },
                },
            )
        )
        assert extract_codex_cumulative_tokens("thread-x") is None


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


# ---------------------------------------------------------------------------
# find_latest_prior_transcript — newest-by-mtime scan across backends
# ---------------------------------------------------------------------------


class TestFindLatestPriorTranscript:
    def _claude_session(
        self,
        claude_projects_dir,
        workspace: Path,
        uuid: str,
        ndjson,
        *,
        text: str = "from claude",
        mtime: float | None = None,
    ) -> Path:
        from claude_on_the_fly.transcript import _workspace_to_claude_hash

        session_dir = claude_projects_dir / _workspace_to_claude_hash(workspace)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"{uuid}.jsonl"
        path.write_bytes(ndjson(_claude_user("hi"), _claude_assistant_text(text)))
        if mtime is not None:
            import os

            os.utime(path, (mtime, mtime))
        return path

    def _codex_session(
        self,
        codex_sessions_dir,
        workspace: Path,
        uuid: str,
        thread_id: str,
        ndjson,
        *,
        text: str = "from codex",
        mtime: float | None = None,
    ) -> Path:
        (workspace / ".codex_sessions").mkdir(parents=True, exist_ok=True)
        (workspace / ".codex_sessions" / uuid).write_text(thread_id)
        rollout_dir = codex_sessions_dir / "2026" / "05" / "18"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        rollout = rollout_dir / f"rollout-2026-05-18T12-00-00-{thread_id}.jsonl"
        rollout.write_bytes(
            ndjson(
                _codex_user("SYS\n\n---\n\nhi"),
                _codex_agent(text),
            )
        )
        if mtime is not None:
            import os

            os.utime(rollout, (mtime, mtime))
        return rollout

    def test_returns_none_when_no_prior_sessions(
        self, tmp_path: Path, claude_projects_dir, codex_sessions_dir
    ):
        from claude_on_the_fly.transcript import find_latest_prior_transcript

        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert find_latest_prior_transcript(workspace) is None

    def test_picks_only_claude_session(
        self, tmp_path: Path, claude_projects_dir, codex_sessions_dir, ndjson
    ):
        from claude_on_the_fly.transcript import find_latest_prior_transcript

        workspace = tmp_path / "ws"
        workspace.mkdir()
        self._claude_session(claude_projects_dir, workspace, "u1", ndjson)

        result = find_latest_prior_transcript(workspace)
        assert result is not None
        turns, backend = result
        assert backend == "claude"
        assert any(t.text == "from claude" for t in turns)

    def test_picks_only_codex_session(
        self, tmp_path: Path, claude_projects_dir, codex_sessions_dir, ndjson
    ):
        from claude_on_the_fly.transcript import find_latest_prior_transcript

        workspace = tmp_path / "ws"
        workspace.mkdir()
        self._codex_session(codex_sessions_dir, workspace, "u1", "thread-abc", ndjson)

        result = find_latest_prior_transcript(workspace)
        assert result is not None
        turns, backend = result
        assert backend == "codex"
        assert any(t.text == "from codex" for t in turns)

    def test_picks_newest_across_backends(
        self, tmp_path: Path, claude_projects_dir, codex_sessions_dir, ndjson
    ):
        """Cross-backend tiebreak: file with the newest mtime wins."""
        from claude_on_the_fly.transcript import find_latest_prior_transcript

        workspace = tmp_path / "ws"
        workspace.mkdir()
        # Older claude session, newer codex session.
        self._claude_session(
            claude_projects_dir,
            workspace,
            "u1",
            ndjson,
            text="old claude turn",
            mtime=1000.0,
        )
        self._codex_session(
            codex_sessions_dir,
            workspace,
            "u2",
            "thread-new",
            ndjson,
            text="new codex turn",
            mtime=2000.0,
        )

        result = find_latest_prior_transcript(workspace)
        assert result is not None
        turns, backend = result
        assert backend == "codex"
        assert any(t.text == "new codex turn" for t in turns)

    def test_picks_newest_across_two_claude_modes(
        self, tmp_path: Path, claude_projects_dir, codex_sessions_dir, ndjson
    ):
        """Multiple claude sessions (one per backend_key) live side-by-side
        under the same project dir; the newest one is the handoff source."""
        from claude_on_the_fly.transcript import find_latest_prior_transcript

        workspace = tmp_path / "ws"
        workspace.mkdir()
        self._claude_session(
            claude_projects_dir,
            workspace,
            "u-ollama",
            ndjson,
            text="ollama turn",
            mtime=1000.0,
        )
        self._claude_session(
            claude_projects_dir,
            workspace,
            "u-native",
            ndjson,
            text="native turn",
            mtime=5000.0,
        )

        result = find_latest_prior_transcript(workspace)
        assert result is not None
        turns, backend = result
        assert backend == "claude"
        assert any(t.text == "native turn" for t in turns)

    def test_exclude_uuid_skips_current_session(
        self, tmp_path: Path, claude_projects_dir, codex_sessions_dir, ndjson
    ):
        """The caller's own session must not match itself — that would
        prepend the current conversation onto its own continuation."""
        from claude_on_the_fly.transcript import find_latest_prior_transcript

        workspace = tmp_path / "ws"
        workspace.mkdir()
        self._claude_session(
            claude_projects_dir,
            workspace,
            "u-current",
            ndjson,
            text="current turn",
            mtime=5000.0,
        )
        self._claude_session(
            claude_projects_dir,
            workspace,
            "u-prior",
            ndjson,
            text="prior turn",
            mtime=1000.0,
        )

        result = find_latest_prior_transcript(workspace, exclude_uuid="u-current")
        assert result is not None
        turns, _ = result
        assert any(t.text == "prior turn" for t in turns)
        assert not any(t.text == "current turn" for t in turns)

    def test_exclude_uuid_returns_none_when_only_self_exists(
        self, tmp_path: Path, claude_projects_dir, codex_sessions_dir, ndjson
    ):
        from claude_on_the_fly.transcript import find_latest_prior_transcript

        workspace = tmp_path / "ws"
        workspace.mkdir()
        self._claude_session(
            claude_projects_dir,
            workspace,
            "u-only",
            ndjson,
        )
        assert find_latest_prior_transcript(workspace, exclude_uuid="u-only") is None

    def test_falls_through_to_next_candidate_when_newest_yields_no_turns(
        self, tmp_path: Path, claude_projects_dir, codex_sessions_dir, ndjson
    ):
        """An empty/tool-only newest session must not block the handoff —
        scan continues to the next candidate."""
        from claude_on_the_fly.transcript import (
            _workspace_to_claude_hash,
            find_latest_prior_transcript,
        )

        workspace = tmp_path / "ws"
        workspace.mkdir()
        # Newest session yields no turns (tool-use only).
        session_dir = claude_projects_dir / _workspace_to_claude_hash(workspace)
        session_dir.mkdir(parents=True, exist_ok=True)
        empty_path = session_dir / "u-empty.jsonl"
        empty_path.write_bytes(
            ndjson(_claude_assistant_tooluse(), _claude_user_toolresult())
        )
        import os

        os.utime(empty_path, (5000.0, 5000.0))

        self._claude_session(
            claude_projects_dir,
            workspace,
            "u-prior",
            ndjson,
            text="prior turn",
            mtime=1000.0,
        )

        result = find_latest_prior_transcript(workspace)
        assert result is not None
        turns, backend = result
        assert backend == "claude"
        assert any(t.text == "prior turn" for t in turns)


# ---------------------------------------------------------------------------
# extract_opencode — reads back via `opencode export`, strips system prefix
# ---------------------------------------------------------------------------


def _opencode_export_payload(*messages: tuple[str, str]) -> dict:
    """Build a fake `opencode export` dict from (role, text) pairs."""
    return {
        "info": {"id": "ses_abc"},
        "messages": [
            {
                "info": {"role": role},
                "parts": [{"type": "text", "text": text}],
            }
            for role, text in messages
        ],
    }


def _write_opencode_mapping(workspace: Path, uuid: str, ses_id: str) -> None:
    sessions_dir = workspace / ".opencode_sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / uuid).write_text(ses_id)


class TestExtractOpencode:
    def test_no_mapping_returns_none(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert extract_opencode(workspace, "missing") is None

    def test_export_failure_returns_none(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_opencode_mapping(workspace, "u1", "ses_abc")
        with patch("claude_on_the_fly.transcript._opencode_export", return_value=None):
            assert extract_opencode(workspace, "u1") is None

    def test_happy_path_returns_turns(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_opencode_mapping(workspace, "u1", "ses_abc")
        payload = _opencode_export_payload(
            ("user", "question one"),
            ("assistant", "answer one"),
        )
        with patch(
            "claude_on_the_fly.transcript._opencode_export", return_value=payload
        ):
            turns = extract_opencode(workspace, "u1")
        assert turns == [Turn("user", "question one"), Turn("assistant", "answer one")]

    def test_strips_system_prompt_prefix_from_first_user_message(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_opencode_mapping(workspace, "u1", "ses_abc")
        payload = _opencode_export_payload(
            ("user", "SYSTEM PROMPT BLOCK\n\n---\n\nreal user text"),
            ("assistant", "reply"),
        )
        with patch(
            "claude_on_the_fly.transcript._opencode_export", return_value=payload
        ):
            turns = extract_opencode(workspace, "u1")
        assert turns == [Turn("user", "real user text"), Turn("assistant", "reply")]

    def test_no_messages_returns_none(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_opencode_mapping(workspace, "u1", "ses_abc")
        with patch(
            "claude_on_the_fly.transcript._opencode_export",
            return_value={"info": {}, "messages": []},
        ):
            assert extract_opencode(workspace, "u1") is None


# ---------------------------------------------------------------------------
# remove_workspace_sessions — the session stores that outlive a workspace
# ---------------------------------------------------------------------------


class TestRemoveWorkspaceSessions:
    def test_removes_the_claude_and_pi_directories(
        self, tmp_path: Path, claude_projects_dir: Path, pi_sessions_dir: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        claude_dir = claude_projects_dir / _workspace_to_claude_hash(workspace)
        pi_dir = pi_sessions_dir / _workspace_to_pi_hash(workspace)
        for directory in (claude_dir, pi_dir):
            directory.mkdir(parents=True)
            (directory / "session.jsonl").write_text("{}\n")

        remove_workspace_sessions(workspace)

        assert not claude_dir.exists()
        assert not pi_dir.exists()

    def test_leaves_other_workspaces_alone(
        self, tmp_path: Path, claude_projects_dir: Path, pi_sessions_dir: Path
    ) -> None:
        """The directory name encodes one workspace path, so a sibling's store
        must survive — this is what makes the removal safe to run per job."""
        mine = tmp_path / "mine"
        theirs = tmp_path / "theirs"
        mine.mkdir()
        theirs.mkdir()
        survivor = claude_projects_dir / _workspace_to_claude_hash(theirs)
        survivor.mkdir(parents=True)

        remove_workspace_sessions(mine)

        assert survivor.exists()

    def test_missing_directories_are_not_an_error(
        self, tmp_path: Path, claude_projects_dir: Path, pi_sessions_dir: Path
    ) -> None:
        """Cleanup is best-effort: a backend that never wrote a session store
        must not turn teardown into a failure."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        remove_workspace_sessions(workspace)
