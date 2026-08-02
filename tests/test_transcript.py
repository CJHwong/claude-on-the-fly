"""Tests for cross-backend transcript extraction and handoff formatting."""

from __future__ import annotations

import json
import time
from pathlib import Path

from claude_on_the_fly import transcript
from claude_on_the_fly.transcript import (
    Turn,
    _workspace_to_claude_hash,
    extract_claude,
    extract_codex,
    extract_codex_cumulative_tokens,
    extract_codex_model,
    format_handoff,
    remove_workspace_sessions,
)


def _write_mapping(workspace: Path, session_uuid: str, thread_id: str) -> Path:
    """Create a daemon-owned Codex mapping for transcript tests."""
    return transcript.codex_state.write_thread_id(workspace, session_uuid, thread_id)


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
                Path("/Users/me/.claude-on-the-fly/workspaces/cron/proj-1")
            )
            == "-Users-me--claude-on-the-fly-workspaces-cron-proj-1"
        )

    def test_underscore_in_segment_becomes_dash(self) -> None:
        # Sanitized github workspaces look like `hardcoretech_gf-external-api_754`
        # (sanitize_key turns `/` and `#` into `_`). Claude CLI normalises
        # underscores to dashes too, so the hash must match.
        # Cross-checked against:
        # ~/.claude/projects/-Users-me--claude-on-the-fly-workspaces-cron-github-hardcoretech-gf-external-api-754/
        assert (
            _workspace_to_claude_hash(
                Path(
                    "/Users/me/.claude-on-the-fly/workspaces/cron/github/"
                    "hardcoretech_gf-external-api_754"
                )
            )
            == "-Users-me--claude-on-the-fly-workspaces-cron-github-"
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
        workspace.mkdir()
        mapping = transcript.codex_state.mapping_path(workspace, "u1")
        mapping.parent.mkdir(parents=True, exist_ok=True)
        mapping.write_text("   ")
        assert extract_codex(workspace, "u1") is None

    def test_no_matching_rollout_returns_none(self, tmp_path: Path, codex_sessions_dir):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-xyz")
        assert extract_codex(workspace, "u1") is None

    def test_extracts_user_and_agent_messages(
        self, tmp_path: Path, codex_sessions_dir, ndjson
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-abc")

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
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-xx")
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
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-yy")
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
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-zz")
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
        _write_mapping(workspace, uuid, thread_id)
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


class TestRemoveWorkspaceSessions:
    def test_removes_the_claude_directory(
        self, tmp_path: Path, claude_projects_dir: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        claude_dir = claude_projects_dir / _workspace_to_claude_hash(workspace)
        claude_dir.mkdir(parents=True)
        (claude_dir / "session.jsonl").write_text("{}\n")

        remove_workspace_sessions(workspace)

        assert not claude_dir.exists()

    def test_removes_the_codex_mapping_too(self, tmp_path: Path, monkeypatch) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mapping_root = tmp_path / "codex-mappings"
        monkeypatch.setattr(transcript.codex_state, "MAPPINGS_DIR", mapping_root)
        mapping = _write_mapping(workspace, "u1", "thread-1")

        remove_workspace_sessions(workspace)

        assert not mapping.exists()

    def test_leaves_other_workspaces_alone(
        self, tmp_path: Path, claude_projects_dir: Path
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
        self, tmp_path: Path, claude_projects_dir: Path
    ) -> None:
        """Cleanup is best-effort: a backend that never wrote a session store
        must not turn teardown into a failure."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        remove_workspace_sessions(workspace)


class TestExtractCodexPromptTokens:
    """The auto-compact gate's reading for codex, and the only evidence a
    compaction did anything — codex publishes no in-band signal."""

    def _rollout(self, tmp_path, monkeypatch, *turns: tuple[int, int]):
        root = tmp_path / "codex-sessions" / "2026" / "07" / "28"
        root.mkdir(parents=True)
        path = root / "rollout-2026-07-28T19-58-02-thread-1.jsonl"
        lines = [
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"input_tokens": prompt},
                            "model_context_window": window,
                        },
                    },
                }
            )
            for prompt, window in turns
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr(
            transcript, "CODEX_SESSIONS_DIR", tmp_path / "codex-sessions"
        )
        return path

    def test_returns_the_latest_prompt_and_window(self, tmp_path, monkeypatch):
        self._rollout(tmp_path, monkeypatch, (18_318, 258_400), (45_730, 258_400))
        assert transcript.extract_codex_prompt_tokens("thread-1") == (45_730, 258_400)

    def test_the_compaction_pass_itself_is_skipped(self, tmp_path, monkeypatch):
        """Compaction reports a turn with input_tokens 0; that describes the pass,
        not the context it left behind, so it must not be read as an empty
        context."""
        self._rollout(
            tmp_path, monkeypatch, (46_357, 258_400), (0, 258_400), (18_507, 258_400)
        )
        assert transcript.extract_codex_prompt_tokens("thread-1") == (18_507, 258_400)

    def test_a_trailing_zero_does_not_erase_the_reading(self, tmp_path, monkeypatch):
        self._rollout(tmp_path, monkeypatch, (18_507, 258_400), (0, 258_400))
        assert transcript.extract_codex_prompt_tokens("thread-1") == (18_507, 258_400)

    def test_missing_rollout_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            transcript, "CODEX_SESSIONS_DIR", tmp_path / "codex-sessions"
        )
        assert transcript.extract_codex_prompt_tokens("nope") is None

    def test_no_token_count_events_returns_none(self, tmp_path, monkeypatch):
        self._rollout(tmp_path, monkeypatch)
        assert transcript.extract_codex_prompt_tokens("thread-1") is None


# ---------------------------------------------------------------------------
# Every reader here runs against files another process owns
# ---------------------------------------------------------------------------


class TestUnreadableFilesAreSkippedNotFatal:
    """A rollout or session log can vanish or become unreadable mid-read: codex and
    claude own these files, not this process. Handoff is a nicety, so every failure
    degrades to "no transcript" rather than losing the user's turn."""

    def test_a_jsonl_that_cannot_be_read_yields_no_records(self, tmp_path: Path):
        missing = tmp_path / "gone.jsonl"
        assert list(transcript._iter_jsonl(missing)) == []

    def test_blank_and_malformed_lines_are_skipped(self, tmp_path: Path):
        path = tmp_path / "mixed.jsonl"
        path.write_bytes(b'\n  \n{"a": 1}\nnot json\n{"b": 2}\n')
        assert list(transcript._iter_jsonl(path)) == [{"a": 1}, {"b": 2}]

    def test_a_first_line_that_cannot_be_read_is_none(self, tmp_path: Path):
        assert transcript._read_first_jsonl(tmp_path / "gone.jsonl") is None

    def test_a_first_line_that_is_not_json_is_none(self, tmp_path: Path):
        path = tmp_path / "bad.jsonl"
        path.write_bytes(b"not json\n")
        assert transcript._read_first_jsonl(path) is None

    def test_a_first_line_that_is_not_a_mapping_is_none(self, tmp_path: Path):
        path = tmp_path / "list.jsonl"
        path.write_bytes(b"[1, 2, 3]\n")
        assert transcript._read_first_jsonl(path) is None

    def test_a_session_mapping_that_cannot_be_read_returns_none(
        self, tmp_path: Path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-abc")

        def read_fails(self, *_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", read_fails)
        assert extract_codex(workspace, "u1") is None


class TestFindCodexRolloutByCwd:
    """Used for *live* tailing: codex only reveals its thread id after the first
    turn finishes, so a fresh session has no mapping and the cwd is the only handle.
    Bounded because a 1Hz caller pays for every stat."""

    def _rollout(self, root: Path, name: str, cwd: str, *, age_s: float = 0.0) -> Path:
        rollout_dir = root / "2026" / "07" / "30"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        path = rollout_dir / f"rollout-2026-07-30T12-00-00-{name}.jsonl"
        path.write_bytes(
            json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}).encode()
            + b"\n"
        )
        if age_s:
            stamp = time.time() - age_s
            import os

            os.utime(path, (stamp, stamp))
        return path

    def test_an_empty_cwd_never_matches(self, codex_sessions_dir):
        """Otherwise a caller with no workspace would adopt an unrelated session."""
        assert transcript._find_codex_rollout_by_cwd("") is None

    def test_no_rollouts_at_all_is_none(self, codex_sessions_dir):
        assert transcript._find_codex_rollout_by_cwd("/ws") is None

    def test_the_freshest_rollout_is_matched_by_cwd(self, codex_sessions_dir):
        self._rollout(codex_sessions_dir, "old", "/ws", age_s=10)
        expected = self._rollout(codex_sessions_dir, "new", "/ws")
        assert transcript._find_codex_rollout_by_cwd("/ws") == expected

    def test_a_stale_rollout_is_not_opened(self, codex_sessions_dir):
        """A live run keeps its mtime current, so anything older than the window is
        a finished session and reading it would attach to the wrong thread."""
        self._rollout(codex_sessions_dir, "stale", "/ws", age_s=1000)
        assert transcript._find_codex_rollout_by_cwd("/ws") is None

    def test_the_freshest_rollout_belonging_to_another_workspace_is_refused(
        self, codex_sessions_dir
    ):
        """Only the freshest candidate is read, so a match for a different cwd means
        no answer rather than falling back to an older rollout: attaching to another
        workspace's session is worse than attaching to none."""
        self._rollout(codex_sessions_dir, "mine", "/ws", age_s=10)
        self._rollout(codex_sessions_dir, "theirs", "/other")
        assert transcript._find_codex_rollout_by_cwd("/ws") is None

    def test_a_rollout_that_cannot_be_stated_is_skipped(
        self, codex_sessions_dir, monkeypatch
    ):
        self._rollout(codex_sessions_dir, "vanishing", "/ws")
        real_stat = Path.stat

        def stat_fails(self, *args, **kwargs):
            if self.name.startswith("rollout-"):
                raise OSError("stale handle")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat_fails)
        assert transcript._find_codex_rollout_by_cwd("/ws") is None

    def test_a_first_line_that_is_not_session_meta_is_refused(self, codex_sessions_dir):
        rollout_dir = codex_sessions_dir / "2026" / "07" / "30"
        rollout_dir.mkdir(parents=True)
        (rollout_dir / "rollout-2026-07-30T12-00-00-x.jsonl").write_bytes(
            json.dumps({"type": "event_msg"}).encode() + b"\n"
        )
        assert transcript._find_codex_rollout_by_cwd("/ws") is None


class TestCodexRolloutScannersSkipIrrelevantRecords:
    def _rollout(self, root: Path, *records: dict) -> Path:
        rollout_dir = root / "2026" / "07" / "30"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        path = rollout_dir / "rollout-2026-07-30T12-00-00-thread-abc.jsonl"
        path.write_bytes(b"\n".join(json.dumps(r).encode() for r in records) + b"\n")
        return path

    def test_extract_codex_ignores_non_event_records(
        self, tmp_path: Path, codex_sessions_dir
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-abc")
        self._rollout(
            codex_sessions_dir,
            {"type": "session_meta", "payload": {"cwd": "/ws"}},
            {"type": "turn_context", "payload": {"model": "gpt-5"}},
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hello"},
            },
        )
        turns = extract_codex(workspace, "u1")
        assert turns is not None
        assert [t.text for t in turns] == ["hello"]

    def test_a_rollout_with_no_turn_context_has_no_model(self, codex_sessions_dir):
        self._rollout(codex_sessions_dir, {"type": "event_msg", "payload": {}})
        assert extract_codex_model("thread-abc") is None

    def test_a_blank_model_string_is_not_a_model(self, codex_sessions_dir):
        """Native mode without CODEX_MODEL writes an empty string, and reporting it
        would label the run with nothing instead of falling back."""
        self._rollout(
            codex_sessions_dir, {"type": "turn_context", "payload": {"model": ""}}
        )
        assert extract_codex_model("thread-abc") is None

    def test_cumulative_tokens_skips_records_that_are_not_token_counts(
        self, codex_sessions_dir
    ):
        self._rollout(
            codex_sessions_dir,
            {"type": "turn_context", "payload": {"model": "gpt-5"}},
            {"type": "event_msg", "payload": {"type": "user_message"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 7}},
                },
            },
        )
        assert extract_codex_cumulative_tokens("thread-abc") == {"input_tokens": 7}

    def test_prompt_tokens_skips_records_that_are_not_token_counts(
        self, codex_sessions_dir
    ):
        self._rollout(
            codex_sessions_dir,
            {"type": "turn_context", "payload": {"model": "gpt-5"}},
            {"type": "event_msg", "payload": {"type": "user_message"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 900},
                        "model_context_window": 400_000,
                    },
                },
            },
        )
        assert transcript.extract_codex_prompt_tokens("thread-abc") == (900, 400_000)


class TestListCodexSessionFiles:
    def test_no_sessions_dir_is_an_empty_list(self, tmp_path: Path):
        assert transcript._list_codex_session_files(tmp_path) == []

    def test_a_subdirectory_is_not_a_mapping(
        self, tmp_path: Path, codex_sessions_dir, monkeypatch
    ):
        mapping_root = tmp_path / "codex-mappings"
        monkeypatch.setattr(transcript.codex_state, "MAPPINGS_DIR", mapping_root)
        sessions = mapping_root
        sessions.mkdir()
        (sessions / "somedir").mkdir()
        assert transcript._list_codex_session_files(tmp_path) == []

    def test_a_mapping_that_cannot_be_read_is_skipped(
        self, tmp_path: Path, codex_sessions_dir, monkeypatch
    ):
        mapping_root = tmp_path / "codex-mappings"
        monkeypatch.setattr(transcript.codex_state, "MAPPINGS_DIR", mapping_root)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-abc")

        def open_fails(path, *_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(transcript.codex_state.os, "open", open_fails)
        assert transcript._list_codex_session_files(workspace) == []

    def test_a_mapping_symlink_is_skipped(
        self, tmp_path: Path, codex_sessions_dir, monkeypatch
    ):
        mapping_root = tmp_path / "codex-mappings"
        monkeypatch.setattr(transcript.codex_state, "MAPPINGS_DIR", mapping_root)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        mapping = _write_mapping(workspace, "u1", "thread-abc")
        target = tmp_path / "target.json"
        target.write_bytes(mapping.read_bytes())
        mapping.unlink()
        mapping.symlink_to(target)

        assert transcript._list_codex_session_files(workspace) == []

    def test_a_mapping_whose_rollout_is_gone_is_skipped(
        self, tmp_path: Path, codex_sessions_dir
    ):
        """A pruned rollout leaves the mapping behind; offering it as a handoff
        candidate would make the caller extract nothing and stop looking."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-vanished")
        assert transcript._list_codex_session_files(workspace) == []

    def test_a_rollout_that_cannot_be_stated_is_skipped(
        self, tmp_path: Path, codex_sessions_dir, monkeypatch
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "u1", "thread-abc")
        rollout_dir = codex_sessions_dir / "2026" / "07" / "30"
        rollout_dir.mkdir(parents=True)
        (rollout_dir / "rollout-2026-07-30T12-00-00-thread-abc.jsonl").write_bytes(b"")
        real_stat = Path.stat
        seen = 0

        def stat_fails(self, *args, **kwargs):
            nonlocal seen
            if self.name.startswith("rollout-"):
                seen += 1
                if seen > 1:
                    raise OSError("stale handle")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat_fails)
        assert transcript._list_codex_session_files(workspace) == []

    def test_a_claude_log_that_cannot_be_stated_is_skipped(
        self, tmp_path: Path, claude_projects_dir, monkeypatch
    ):
        workspace = tmp_path / "ws"
        project_dir = claude_projects_dir / transcript._workspace_to_claude_hash(
            workspace
        )
        project_dir.mkdir(parents=True)
        (project_dir / "u1.jsonl").write_bytes(b"")
        real_stat = Path.stat

        def stat_fails(self, *args, **kwargs):
            # Only the log file: the enclosing dir still has to stat, or the
            # is_dir() guard above returns before the loop is reached.
            if self.suffix == ".jsonl":
                raise OSError("stale handle")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat_fails)
        assert transcript._list_claude_session_files(workspace) == []


class TestFindLatestPriorExcludesTheCurrentSession:
    def test_the_excluded_uuid_is_not_a_candidate_on_either_backend(
        self, tmp_path: Path, codex_sessions_dir, monkeypatch
    ):
        """Without this the handoff would forward the session's own turns back into
        itself, doubling the context it was meant to carry across."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setattr(
            transcript,
            "_list_claude_session_files",
            lambda _ws: [(Path("/x"), "current", 100.0)],
        )
        monkeypatch.setattr(
            transcript,
            "_list_codex_session_files",
            lambda _ws: [(Path("/y"), "current", 200.0)],
        )
        assert (
            transcript.find_latest_prior_transcript(workspace, exclude_uuid="current")
            is None
        )


class TestPrependLatestHandoff:
    def test_a_scan_that_raises_starts_clean(self, tmp_path: Path, monkeypatch, caplog):
        """The user still gets a reply, just without handoff context."""
        monkeypatch.setattr(
            transcript,
            "find_latest_prior_transcript",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("scan blew up")),
        )
        with caplog.at_level("ERROR", logger="claude_on_the_fly.transcript"):
            assert transcript.prepend_latest_handoff(tmp_path, "hi") == "hi"
        assert "starting clean" in "\n".join(r.getMessage() for r in caplog.records)

    def test_nothing_prior_leaves_the_prompt_alone(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            transcript, "find_latest_prior_transcript", lambda *_a, **_kw: None
        )
        assert transcript.prepend_latest_handoff(tmp_path, "hi") == "hi"

    def test_turns_that_format_to_nothing_leave_the_prompt_alone(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(
            transcript,
            "find_latest_prior_transcript",
            lambda *_a, **_kw: ([], "claude"),
        )
        assert transcript.prepend_latest_handoff(tmp_path, "hi") == "hi"

    def test_a_real_handoff_is_prefixed(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            transcript,
            "find_latest_prior_transcript",
            lambda *_a, **_kw: ([Turn(role="user", text="earlier")], "codex"),
        )
        out = transcript.prepend_latest_handoff(tmp_path, "now")
        assert out.endswith("now")
        assert "earlier" in out


class TestOneBadExtractorDoesNotStopTheSearch:
    def test_the_next_candidate_is_tried_after_a_failure(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Candidates are ordered newest first, so a corrupt recent log would
        otherwise hide every older one behind it."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setattr(
            transcript,
            "_list_claude_session_files",
            lambda _ws: [(Path("/x"), "newest", 200.0), (Path("/y"), "older", 100.0)],
        )
        monkeypatch.setattr(transcript, "_list_codex_session_files", lambda _ws: [])

        def extract(_workspace, uuid):
            if uuid == "newest":
                raise ValueError("corrupt log")
            return [Turn(role="user", text="from the older one")]

        monkeypatch.setattr(transcript, "extract_claude", extract)
        with caplog.at_level("ERROR", logger="claude_on_the_fly.transcript"):
            found = transcript.find_latest_prior_transcript(workspace)
        assert found is not None
        turns, backend = found
        assert backend == "claude"
        assert turns[0].text == "from the older one"
        assert "trying next" in "\n".join(r.getMessage() for r in caplog.records)

    def test_every_candidate_failing_yields_none(self, tmp_path: Path, monkeypatch):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setattr(
            transcript,
            "_list_claude_session_files",
            lambda _ws: [(Path("/x"), "only", 200.0)],
        )
        monkeypatch.setattr(transcript, "_list_codex_session_files", lambda _ws: [])
        monkeypatch.setattr(
            transcript,
            "extract_claude",
            lambda *_a: (_ for _ in ()).throw(ValueError("corrupt")),
        )
        assert transcript.find_latest_prior_transcript(workspace) is None


class TestPrependHandoffFormatsToNothing:
    def test_turns_that_format_to_an_empty_handoff_leave_the_prompt_alone(
        self, tmp_path: Path, monkeypatch
    ):
        """format_handoff drops turns with no usable text, so a non-empty turn list
        can still produce nothing to prepend."""
        monkeypatch.setattr(transcript, "format_handoff", lambda *_a, **_kw: "")
        assert (
            transcript.prepend_handoff(
                tmp_path,
                "uuid",
                "hi",
                from_backend="claude",
                extractor=lambda *_a: [Turn(role="user", text="")],
            )
            == "hi"
        )
