"""Tests for the codex backend."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.agent import NUDGE_PROMPT, OllamaLauncher, get_backend
from claude_on_the_fly.backends import codex as codex_mod
from claude_on_the_fly.backends.codex import (
    CodexBackend,
    _merge_codex_results,
    parse_codex_rollout,
    read_rollout,
)
from claude_on_the_fly.transcript import Turn


def _write_rollout(thread_id: str, home: Path) -> Path:
    """The rollout file codex would have written for `thread_id` under `home`."""
    path = (
        home / "sessions/2026/08/14" / f"rollout-2026-08-14T00-00-00-{thread_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    return path


def _write_mapping(workspace: Path, session_uuid: str, thread_id: str) -> Path:
    """Create a daemon-owned mapping, plus the rollout that makes it resumable.

    The backend asks whether `resume` has a rollout to land on before it passes
    one, so a mapping on its own is the dead-mapping case rather than the ordinary
    one these tests are about.
    """
    _write_rollout(thread_id, codex_mod.codex_state.home_dir(workspace))
    return codex_mod.codex_state.write_thread_id(workspace, session_uuid, thread_id)


def _session_meta(thread_id: str) -> dict:
    return {"type": "session_meta", "payload": {"session_id": thread_id}}


def _item(item_type: str, **fields) -> dict:
    """One `item_completed` event, the rollout's record of a finished item."""
    return {
        "type": "event_msg",
        "payload": {"type": "item_completed", "item": {"type": item_type, **fields}},
    }


def _agent_message(text: str, phase: str = "final_answer") -> dict:
    return _item("AgentMessage", content=[{"type": "Text", "text": text}], phase=phase)


def _assistant_text(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _task_complete(text: str = "") -> dict:
    return {
        "type": "event_msg",
        "payload": {"type": "task_complete", "last_agent_message": text},
    }


def _token_count(**usage: int) -> dict:
    return {
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"last_token_usage": usage}},
    }


def _turn_aborted(reason: str) -> dict:
    return {
        "type": "event_msg",
        "payload": {"type": "turn_aborted", "reason": reason},
    }


def _rollout_text(*records: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


# ---------------------------------------------------------------------------
# parse_codex_rollout
# ---------------------------------------------------------------------------


class TestParseCodexRollout:
    def test_no_records_returns_defaults(self):
        out = parse_codex_rollout([])
        assert out["thread_id"] is None
        assert out["body"] == ""
        assert out["usage"] == {}
        assert out["error"] is None
        assert out["tool_counts"] == {}
        assert out["completed"] is False

    def test_happy_path_captures_thread_body_usage(self):
        out = parse_codex_rollout(
            [
                _session_meta("thread-abc"),
                _item("Reasoning"),
                _agent_message("pong"),
                _token_count(
                    input_tokens=100,
                    cached_input_tokens=20,
                    cache_write_input_tokens=10,
                    output_tokens=5,
                    reasoning_output_tokens=3,
                ),
                _task_complete("pong"),
            ]
        )
        assert out["thread_id"] == "thread-abc"
        assert out["body"] == "pong"
        assert out["completed"] is True
        assert out["usage"]["input_tokens"] == 100
        assert out["usage"]["cache_write_input_tokens"] == 10
        assert out["usage"]["reasoning_output_tokens"] == 3
        assert out["error"] is None
        # Reasoning is not a tool, and neither is the agent's own message.
        assert out["tool_counts"] == {}

    def test_command_execution_counted_as_tool(self):
        out = parse_codex_rollout(
            [
                _session_meta("t1"),
                _item("CommandExecution", command="ls"),
                _item("CommandExecution", command="pwd"),
                _agent_message("done"),
                _task_complete("done"),
            ]
        )
        assert out["tool_counts"] == {"CommandExecution": 2}
        assert out["body"] == "done"

    def test_mixed_tool_types_each_counted_separately(self):
        out = parse_codex_rollout(
            [
                _item("CommandExecution"),
                _item("FileChange"),
                _item("CommandExecution"),
                _item("Reasoning"),
                _item("UserMessage"),
                _agent_message("ok"),
            ]
        )
        assert out["tool_counts"] == {"CommandExecution": 2, "FileChange": 1}

    def test_an_unknown_item_counts_as_a_tool(self):
        """Codex adds tools. A new one belongs in the footer, not silently dropped."""
        out = parse_codex_rollout([_item("SomeFutureTool")])
        assert out["tool_counts"] == {"SomeFutureTool": 1}

    def test_turn_aborted_sets_error(self):
        out = parse_codex_rollout([_session_meta("t1"), _turn_aborted("interrupted")])
        assert out["error"] == "interrupted"

    def test_turn_aborted_without_a_reason_still_reports_an_error(self):
        out = parse_codex_rollout(
            [{"type": "event_msg", "payload": {"type": "turn_aborted"}}]
        )
        assert out["error"] == "codex turn aborted"

    def test_records_that_are_not_about_the_turn_are_ignored(self):
        """A rollout carries more than this parser needs: `world_state` and
        `turn_context` have no payload dict, and `compacted` has one that means
        nothing here."""
        out = parse_codex_rollout(
            [
                {"type": "world_state"},
                {"type": "turn_context", "payload": None},
                {"type": "compacted", "payload": {"message": "..."}},
            ]
        )
        assert out["body"] == ""
        assert out["tool_counts"] == {}

    def test_the_last_real_assistant_text_is_kept_for_a_block_only_reply(self):
        out = parse_codex_rollout(
            [
                _assistant_text("checking the tests"),
                _assistant_text('<suggestions>["a"]</suggestions>'),
                _task_complete('<suggestions>["a"]</suggestions>'),
            ]
        )
        # The suggestions block is the protocol token, not something it said.
        assert out["last_assistant_text"] == "checking the tests"

    def test_task_complete_without_a_message_does_not_clobber_the_body(self):
        out = parse_codex_rollout([_task_complete("real"), _task_complete("")])
        assert out["body"] == "real"


class TestReadRollout:
    def test_reading_starts_where_the_last_read_stopped(self, tmp_path: Path):
        """A resumed thread's rollout already holds every earlier turn."""
        path = tmp_path / "rollout.jsonl"
        path.write_text(_rollout_text(_task_complete("old turn")))
        offset = path.stat().st_size
        with path.open("a") as handle:
            handle.write(_rollout_text(_task_complete("this turn")))

        records, new_offset = read_rollout(path, offset)

        assert parse_codex_rollout(records)["body"] == "this turn"
        assert new_offset == path.stat().st_size

    def test_a_half_written_record_is_left_for_the_next_read(self, tmp_path: Path):
        """The follower polls a file codex is still writing."""
        path = tmp_path / "rollout.jsonl"
        path.write_text(_rollout_text(_task_complete("done")) + '{"type": "event_')

        records, offset = read_rollout(path, 0)

        assert len(records) == 1
        assert offset < path.stat().st_size

        # The rest of the line lands, and the next read picks it up whole.
        with path.open("a") as handle:
            handle.write('msg", "payload": {"type": "turn_aborted"}}\n')
        more, _ = read_rollout(path, offset)
        assert parse_codex_rollout(more)["error"] == "codex turn aborted"

    def test_malformed_lines_are_skipped(self, tmp_path: Path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            _rollout_text(_session_meta("t1"))
            + "not-json\n"
            + _rollout_text(_task_complete("ok"))
        )
        records, _ = read_rollout(path, 0)
        out = parse_codex_rollout(records)
        assert out["thread_id"] == "t1"
        assert out["body"] == "ok"

    def test_a_missing_file_reads_as_nothing(self, tmp_path: Path):
        records, offset = read_rollout(tmp_path / "never-written.jsonl", 0)
        assert records == []
        assert offset == 0


# ---------------------------------------------------------------------------
# _run_codex_exec
# ---------------------------------------------------------------------------


def _exec_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


async def _run_exec(
    proc,
    tmp_path: Path,
    *,
    records: tuple[dict, ...] = (),
    kill: AsyncMock | None = None,
    rollout: Path | None = None,
    interactive: list[str] | None = None,
):
    """Drive `_run_codex_exec` against a fake process and a fixture rollout.

    The rollout is what the turn is read from now, so a test states its records
    rather than a stdout stream. `rollout` lets a test own the file and append to
    it while the run is in flight.
    """
    path = rollout if rollout is not None else tmp_path / "rollout.jsonl"
    if records:
        path.write_text(_rollout_text(*records))
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch.object(codex_mod.agent, "track_agent_process"),
        patch.object(codex_mod.agent, "_kill_process_tree", kill or AsyncMock()),
        patch.object(
            codex_mod.transcript,
            "_find_codex_rollout_by_cwd",
            lambda _cwd, **_kw: path if path.exists() else None,
        ),
    ):
        return await codex_mod._run_codex_exec(
            tmp_path,
            ["codex", "exec", "prompt"],
            timeout=None,
            interactive=interactive,
        )


def _stream(data: bytes = b"", *, eof: bool = True) -> asyncio.StreamReader:
    """A real StreamReader — `communicate_capped` isinstance-checks for one.

    `eof=False` models a codex that has written everything but never exits, so
    its stdout pipe stays open (in the wild, held by a surviving
    `codex-code-mode-host` child). Reads block there until something feeds EOF.
    """
    stream = asyncio.StreamReader()
    if data:
        stream.feed_data(data)
    if eof:
        stream.feed_eof()
    return stream


def _streamed_proc(returncode: int, stdout: asyncio.StreamReader, stderr=None):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr if stderr is not None else _stream()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


class TestRolloutFollower:
    """The turn's live view: progress out, completion noticed."""

    def _follower(self, tmp_path: Path, emit=None):
        path = tmp_path / "rollout.jsonl"
        path.write_text("")
        follower = codex_mod._RolloutFollower(tmp_path, None, emit)
        with patch.object(
            codex_mod.transcript, "_find_codex_rollout_by_cwd", lambda _c, **_k: path
        ):
            follower._drain()
        return follower, path

    def test_commentary_is_relayed_but_the_closing_reply_is_not(self, tmp_path: Path):
        """The reply is delivered as the turn's answer; relaying it would double it."""
        relayed: list[str] = []
        follower, path = self._follower(tmp_path, relayed.append)
        path.write_text(
            _rollout_text(
                _agent_message("looking at the tests now", phase="commentary"),
                _agent_message("all green", phase="final_answer"),
            )
        )
        with patch.object(
            codex_mod.transcript, "_find_codex_rollout_by_cwd", lambda _c, **_k: path
        ):
            follower._drain()

        assert relayed == ["looking at the tests now"]

    def test_task_complete_marks_the_turn_finished(self, tmp_path: Path):
        follower, path = self._follower(tmp_path)
        assert not follower.turn_completed.is_set()
        path.write_text(_rollout_text(_task_complete("done")))
        follower._drain()

        assert follower.turn_completed.is_set()

    def test_a_broken_frontend_does_not_abort_the_turn(self, tmp_path: Path):
        def explode(_text: str) -> None:
            raise RuntimeError("frontend is down")

        follower, path = self._follower(tmp_path, explode)
        path.write_text(_rollout_text(_agent_message("narration", phase="commentary")))
        follower._drain()  # must not raise

        assert follower.records

    def test_a_resumed_thread_starts_after_what_is_already_there(self, tmp_path: Path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(_rollout_text(_task_complete("an earlier turn")))
        with patch.object(codex_mod.transcript, "_find_codex_rollout", lambda _t: path):
            follower = codex_mod._RolloutFollower(tmp_path, "t1", None)
        with path.open("a") as handle:
            handle.write(_rollout_text(_task_complete("this turn")))
        follower._drain()

        assert parse_codex_rollout(follower.records)["body"] == "this turn"


class TestHungCodexIsKilled:
    """codex can finish a turn and then never exit. Don't wait for it."""

    async def test_hang_after_the_turn_is_killed_and_reply_delivered(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        monkeypatch.setattr(codex_mod, "POST_TURN_EXIT_GRACE", 0.05)
        monkeypatch.setattr(codex_mod, "ROLLOUT_POLL_S", 0.01)
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text(
            _rollout_text(
                _session_meta("t1"),
                _agent_message("PR #804 is merged."),
                _task_complete("PR #804 is merged."),
            )
        )
        stdout = _stream(eof=False)
        stderr = _stream(eof=False)
        proc = _streamed_proc(-9, stdout, stderr)

        # The real _kill_process_tree closes the pipes, which is what lets the
        # readers finally see EOF; the fake has to do the same.
        def release(*_args):
            stdout.feed_eof()
            stderr.feed_eof()

        kill = AsyncMock(side_effect=release)

        with caplog.at_level(logging.WARNING):
            out = await asyncio.wait_for(
                _run_exec(proc, tmp_path, kill=kill, rollout=rollout), timeout=5
            )

        assert out["body"] == "PR #804 is merged."
        assert out["completed"] is True
        assert kill.await_count >= 1
        assert "silent for" in caplog.text

    async def test_no_kill_when_codex_exits_on_its_own(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setattr(codex_mod, "ROLLOUT_POLL_S", 0.01)
        proc = _streamed_proc(0, _stream())
        kill = AsyncMock()
        out = await asyncio.wait_for(
            _run_exec(
                proc,
                tmp_path,
                records=(_agent_message("done"), _task_complete("done")),
                kill=kill,
            ),
            timeout=5,
        )
        assert out["body"] == "done"
        # Only the unconditional `finally` teardown, never the watchdog.
        assert kill.await_count == 1

    async def test_grace_rearms_while_output_keeps_coming(
        self, tmp_path: Path, monkeypatch
    ):
        """A second turn in one exec (auto-compaction) must not be cut short."""
        monkeypatch.setattr(codex_mod, "POST_TURN_EXIT_GRACE", 0.4)
        monkeypatch.setattr(codex_mod, "ROLLOUT_POLL_S", 0.01)
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text(_rollout_text(_task_complete("first")))
        stdout = _stream(eof=False)
        proc = _streamed_proc(0, stdout)

        async def second_turn():
            # Well inside the grace, so the timer keeps re-arming.
            await asyncio.sleep(0.1)
            with rollout.open("a") as handle:
                handle.write(_rollout_text(_item("CommandExecution")))
            await asyncio.sleep(0.1)
            with rollout.open("a") as handle:
                handle.write(_rollout_text(_task_complete("second")))
            await asyncio.sleep(0.1)
            stdout.feed_eof()

        kill = AsyncMock()
        feeder = asyncio.create_task(second_turn())
        out = await asyncio.wait_for(
            _run_exec(proc, tmp_path, kill=kill, rollout=rollout), timeout=5
        )
        await feeder
        assert out["body"] == "second"
        assert kill.await_count == 1  # teardown only — the watchdog never fired


class TestRunCodexExec:
    async def test_nonzero_exit_after_completed_turn_still_returns_body(
        self, tmp_path: Path, caplog
    ):
        """codex can deadlock at exit with its reply already written; the turn is
        finished, so the reply must survive being killed."""
        proc = _exec_proc(-9, stderr=b"ERROR codex_core::tools::router: stale noise")
        with caplog.at_level(logging.WARNING):
            out = await _run_exec(
                proc,
                tmp_path,
                records=(
                    _session_meta("t1"),
                    _agent_message("PR #804 is merged."),
                    _task_complete("PR #804 is merged."),
                ),
            )
        assert out["body"] == "PR #804 is merged."
        assert out["thread_id"] == "t1"
        # The deadlock is un-root-caused upstream; stderr is the only artifact
        # that can explain a given instance, so it has to reach the log.
        assert "codex_core::tools::router" in caplog.text

    async def test_nonzero_exit_mid_turn_raises_instead_of_shipping_a_fragment(
        self, tmp_path: Path
    ):
        """A turn killed mid-work leaves an intermediate message that looks
        exactly like a final answer. Without task_complete it is not one, and
        delivering it would be a silently wrong reply."""
        proc = _exec_proc(-9, stderr=b"killed mid-turn")
        with pytest.raises(RuntimeError, match="killed mid-turn"):
            await _run_exec(
                proc,
                tmp_path,
                records=(
                    _session_meta("t1"),
                    _agent_message("Let me check the tests first"),
                ),
            )

    async def test_nonzero_exit_after_completed_turn_without_body_raises(
        self, tmp_path: Path
    ):
        """A finished turn with nothing to say is not a reply worth delivering."""
        proc = _exec_proc(-9, stderr=b"empty turn")
        with pytest.raises(RuntimeError, match="empty turn"):
            await _run_exec(proc, tmp_path, records=(_task_complete(""),))

    async def test_nonzero_exit_without_body_raises_stderr(self, tmp_path: Path):
        proc = _exec_proc(1, stderr=b"codex: command not found")
        with pytest.raises(RuntimeError, match="command not found"):
            await _run_exec(proc, tmp_path)

    async def test_nonzero_exit_without_body_or_stderr_raises_exit_code(
        self, tmp_path: Path
    ):
        proc = _exec_proc(-15)
        with pytest.raises(RuntimeError, match="Exit code -15"):
            await _run_exec(proc, tmp_path)

    async def test_turn_aborted_raises_even_with_a_body(self, tmp_path: Path):
        """An abort is terminal: a partial body must not mask it."""
        proc = _exec_proc(0)
        with pytest.raises(RuntimeError, match="context exhausted"):
            await _run_exec(
                proc,
                tmp_path,
                records=(_agent_message("partial"), _turn_aborted("context exhausted")),
            )

    async def test_turn_aborted_wins_over_stderr_on_nonzero_exit(self, tmp_path: Path):
        proc = _exec_proc(1, stderr=b"noisy teardown")
        with pytest.raises(RuntimeError, match="rate limited"):
            await _run_exec(proc, tmp_path, records=(_turn_aborted("rate limited"),))

    async def test_clean_exit_returns_parsed(self, tmp_path: Path):
        proc = _exec_proc(0)
        out = await _run_exec(proc, tmp_path, records=(_task_complete("done"),))
        assert out["body"] == "done"


# ---------------------------------------------------------------------------
# CodexBackend.run
# ---------------------------------------------------------------------------


def _success_result(
    thread_id: str | None = "thread-1",
    body: str = "hello",
    last_assistant_text: str = "",
    input_tokens: int = 100,
    cached: int = 20,
    cache_write: int = 0,
    output_tokens: int = 10,
    reasoning_tokens: int = 5,
    tool_counts: dict | None = None,
) -> dict:
    return {
        "thread_id": thread_id,
        "body": body,
        "last_assistant_text": last_assistant_text,
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write,
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
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text(
            _rollout_text(
                _session_meta("codex-thread-killed"),
                _agent_message("PR #804 is merged."),
                _token_count(input_tokens=100, output_tokens=5),
                _task_complete("PR #804 is merged."),
            )
        )
        proc = _exec_proc(-9, stderr=b"stale teardown noise")
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.object(codex_mod.agent, "track_agent_process"),
            patch.object(codex_mod.agent, "_kill_process_tree", AsyncMock()),
            patch.object(
                codex_mod.transcript,
                "_find_codex_rollout_by_cwd",
                lambda _cwd, **_kw: rollout,
            ),
        ):
            resp = await CodexBackend().run(
                workspace, "killed-session", "hi", "telegram"
            )

        assert resp.body == "PR #804 is merged."
        # task_complete gates delivery, and the rollout's token_count keeps the
        # fallback count honest instead of billing the turn as free.
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

    async def test_a_thread_with_no_rollout_left_starts_again(self, tmp_path: Path):
        """codex answers `resume` on a rollout it cannot find with "no rollout found
        for thread id", which fails the whole turn. A mapping naming a thread nothing
        can resume is dead, so it goes and the turn starts a new one -- forgetful
        rather than broken. The system prompt comes back with it, because the new
        thread has no persisted history holding it."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        codex_mod.codex_state.write_thread_id(workspace, "sess-dead", "vanished-thread")

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(thread_id="a-fresh-thread"),
        ) as mock:
            await CodexBackend().run(workspace, "sess-dead", "follow-up", "telegram")

        cmd = mock.call_args[0][1]
        assert "resume" not in cmd
        assert "You are Claude" in cmd[-1] or "---" in cmd[-1]
        # The dead mapping is replaced by the thread codex actually started, so a
        # later turn resumes something that exists.
        assert (
            codex_mod.codex_state.read_thread_id(workspace, "sess-dead")
            == "a-fresh-thread"
        )

    async def test_a_rollout_left_in_the_shared_tree_is_adopted(
        self, tmp_path: Path, scoped_sessions
    ):
        """The upgrade path for the session boundary. The thread was started while
        every rollout went to the shared tree; scoping moves the home codex reads,
        so the rollout is copied across and the conversation survives the flip."""
        from claude_on_the_fly import envfile

        workspace = tmp_path / "ws"
        workspace.mkdir()
        origin = _write_rollout("older-thread", envfile.codex_home())
        codex_mod.codex_state.write_thread_id(workspace, "sess-flip", "older-thread")
        home = codex_mod.codex_state.home_dir(workspace)
        assert not any(home.glob("sessions/**/*.jsonl"))

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(workspace, "sess-flip", "follow-up", "telegram")

        cmd = mock.call_args[0][1]
        assert cmd[cmd.index("resume") + 1] == "older-thread"
        adopted = home / "sessions/2026/08/14" / origin.name
        assert adopted.read_text() == "{}\n"
        # The original stays: the daemon reads it for token and model lookups, and
        # an operator who turns the boundary back off needs it there.
        assert origin.is_file()

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
        """OLLAMA_EFFORT belongs to the mode that swapped the model out. It must
        not leak into native argv, which has a key of its own."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("OLLAMA_EFFORT", "high")
        monkeypatch.delenv("CODEX_EFFORT", raising=False)
        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(workspace, "sess", "hi", "telegram")
        assert "-c" not in mock.call_args[0][1]

    async def test_native_effort_from_its_own_key(self, tmp_path, monkeypatch):
        """CODEX_EFFORT is how an operator overrides effort for cotf's own native
        spawns, without touching their own ~/.codex/config.toml."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("CODEX_EFFORT", "xhigh")
        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(workspace, "sess", "hi", "telegram")
        assert 'model_reasoning_effort="xhigh"' in mock.call_args[0][1]

    async def test_native_effort_omitted_when_its_key_is_unset(
        self, tmp_path, monkeypatch
    ):
        """Unset means "inherit": no override, so codex reads
        model_reasoning_effort from the operator's own config exactly as before."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.delenv("CODEX_EFFORT", raising=False)
        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(workspace, "sess", "hi", "telegram")
        assert "-c" not in mock.call_args[0][1]

    async def test_ollama_effort_wins_over_the_native_key(self, tmp_path, monkeypatch):
        """Both set: the launcher chose the model, so the launcher's key decides.
        The native key is not consulted, not merged."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("OLLAMA_EFFORT", "high")
        monkeypatch.setenv("CODEX_EFFORT", "low")
        launcher = OllamaLauncher(model="deepseek-v4-flash:cloud")
        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend(launcher=launcher).run(
                workspace, "sess", "hi", "telegram"
            )
        assert 'model_reasoning_effort="high"' in mock.call_args[0][1]

    async def test_native_effort_level_out_of_set_skipped(
        self, tmp_path, monkeypatch, caplog
    ):
        """A typo in config.yaml warns here rather than dying in codex's own
        config parse."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("CODEX_EFFORT", "enormous")
        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            return_value=_success_result(),
        ) as mock:
            await CodexBackend().run(workspace, "sess", "hi", "telegram")
        assert "-c" not in mock.call_args[0][1]
        assert "ignoring unknown effort 'enormous'" in caplog.text

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
        # No --json: it would render JSONL into the terminal the mirror shows,
        # and the rollout already carries everything it put on stdout.
        assert "--json" not in cmd

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
        """Report the counts this exec appended, not codex stdout's figure.

        Codex writes each model call's own usage into the session file, so a
        turn's cost is the sum of the records it added. Subtracting two
        `total_token_usage` snapshots undercounted input by three orders of
        magnitude and went negative whenever a turn produced less output than
        the one before it.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "sess-resume", "existing-thread")

        # One record existed before the exec; the exec appended one of its own.
        # Its own record is the answer, whatever the earlier turn happened to
        # cost -- note the earlier one is larger, which is what used to render
        # a negative.
        earlier = {
            "input_tokens": 26000,
            "output_tokens": 250,
            "reasoning_output_tokens": 0,
        }
        this_turn = {
            "input_tokens": 14000,
            "output_tokens": 150,
            "reasoning_output_tokens": 0,
        }

        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.extract_codex_usage_events",
                side_effect=[[earlier], [earlier, this_turn]],
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
                "claude_on_the_fly.backends.codex.transcript.extract_codex_usage_events",
                return_value=[],
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

    async def test_cost_uses_cache_buckets_from_stdout_usage(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        result = _success_result(
            input_tokens=200,
            cached=80,
            cache_write=20,
            output_tokens=40,
            reasoning_tokens=60,
        )

        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.extract_codex_model",
                return_value="gpt-test",
            ),
            patch(
                "claude_on_the_fly.backends.codex.transcript.extract_codex_usage_events",
                return_value=[],
            ),
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=result,
            ),
            patch(
                "claude_on_the_fly.backends.codex.pricing.cost_for",
                return_value=0.42,
            ) as cost_for,
        ):
            resp = await CodexBackend().run(workspace, "sess", "hi", "telegram")

        assert resp.cost == 0.42
        assert cost_for.call_args.args == ("gpt-test", 100, 100, 80, 20)

    async def test_cost_uses_cache_deltas_from_session_totals(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "sess-resume", "existing-thread")
        earlier = {
            "input_tokens": 1000,
            "cached_input_tokens": 600,
            "cache_write_input_tokens": 100,
            "output_tokens": 100,
            "reasoning_output_tokens": 20,
        }
        this_turn = {
            "input_tokens": 200,
            "cached_input_tokens": 100,
            "cache_write_input_tokens": 50,
            "output_tokens": 40,
            "reasoning_output_tokens": 15,
        }

        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.extract_codex_model",
                return_value="gpt-test",
            ),
            patch(
                "claude_on_the_fly.backends.codex.transcript.extract_codex_usage_events",
                side_effect=[[earlier], [earlier, this_turn]],
            ),
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(thread_id="existing-thread"),
            ),
            patch(
                "claude_on_the_fly.backends.codex.pricing.cost_for",
                return_value=0.24,
            ) as cost_for,
        ):
            resp = await CodexBackend().run(
                workspace, "sess-resume", "next turn", "telegram"
            )

        assert resp.cost == 0.24
        assert cost_for.call_args.args == ("gpt-test", 50, 55, 100, 50)

    async def test_a_quieter_turn_after_a_loud_one_is_not_negative(
        self, tmp_path: Path
    ):
        """A turn that says less than the previous one still costs what it cost.

        Measured against codex 0.150.1: `total_token_usage` carries each call's
        own figures, not a running total, so subtracting consecutive snapshots
        rendered `↓-285` in chat on two of four consecutive resumes. Nothing
        about a short reply after a long one is unusual, so this is the common
        case rather than an edge one.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_mapping(workspace, "sess-resume", "existing-thread")
        loud = {
            "input_tokens": 17647,
            "output_tokens": 285,
            "reasoning_output_tokens": 5,
        }
        quiet = {
            "input_tokens": 17813,
            "output_tokens": 5,
            "reasoning_output_tokens": 0,
        }

        with (
            patch(
                "claude_on_the_fly.backends.codex.transcript.extract_codex_usage_events",
                side_effect=[[loud], [loud, quiet]],
            ),
            patch(
                "claude_on_the_fly.backends.codex._run_codex_exec",
                new_callable=AsyncMock,
                return_value=_success_result(thread_id="existing-thread"),
            ),
        ):
            resp = await CodexBackend().run(
                workspace, "sess-resume", "next turn", "telegram"
            )

        assert resp.tokens_out == 5
        assert resp.tokens_in == 17813

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


class TestCodexUsageAccounting:
    def test_a_turn_costs_the_sum_of_the_calls_it_made(self):
        """One exec can fan out to several model calls, and the turn's cost is
        all of them. Reading only the last record would undercount it."""
        calls = [
            {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7},
            {"input_tokens": 250, "cached_input_tokens": 90, "output_tokens": 3},
        ]

        assert codex_mod._usage_from_events(calls) == {
            "input_tokens": 350,
            "cached_input_tokens": 130,
            "cache_write_input_tokens": 0,
            "output_tokens": 10,
            "reasoning_output_tokens": 0,
        }

    def test_a_thread_with_no_records_costs_nothing(self):
        assert codex_mod._usage_from_events([]) == dict.fromkeys(
            codex_mod._CODEX_USAGE_FIELDS, 0
        )


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
            input_tokens=100,
            cached=50,
            cache_write=12,
            output_tokens=10,
            reasoning_tokens=5,
        )
        second = _success_result(
            input_tokens=200,
            cached=30,
            cache_write=7,
            output_tokens=80,
            reasoning_tokens=20,
        )
        merged = _merge_codex_results(first, second)
        assert merged["usage"]["input_tokens"] == 300
        assert merged["usage"]["cached_input_tokens"] == 80
        assert merged["usage"]["cache_write_input_tokens"] == 19
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

    async def test_a_suggestions_only_body_is_not_retried(self, tmp_path: Path):
        """A well-formed <suggestions> block means the turn reached the end of
        its instructions and chose to say nothing — an unattended router told
        not to reply. That is a completed turn, not a dead one, so it is passed
        through for the orchestrator to strip rather than nudged: the retry
        re-asks a question the turn already answered."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        only_block = _success_result(
            thread_id="t1", body='<suggestions>["x?"]</suggestions>'
        )

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            side_effect=[only_block],
        ) as mock:
            resp = await CodexBackend().run(
                workspace,
                "sess-x",
                "hi",
                "telegram",
                nudge_prompt="nudge with template",
            )

        assert mock.await_count == 1
        # Handed on verbatim; the orchestrator owns the placeholder.
        assert resp.body == '<suggestions>["x?"]</suggestions>'

    async def test_a_block_only_body_falls_back_to_the_last_real_text(
        self,
        tmp_path: Path,
    ):
        """A turn that ends with only a <suggestions> block did say something
        earlier: the block is the protocol token, not a reply. The last real
        text replaces it so the user sees the answer instead of the
        orchestrator's placeholder — still without a second billed turn."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        only_block = _success_result(
            thread_id="t1",
            body='<suggestions>["x?"]</suggestions>',
            last_assistant_text="the real summary",
        )

        with patch(
            "claude_on_the_fly.backends.codex._run_codex_exec",
            new_callable=AsyncMock,
            side_effect=[only_block],
        ) as mock:
            resp = await CodexBackend().run(
                workspace,
                "sess-x",
                "hi",
                "telegram",
                nudge_prompt="nudge with template",
            )

        assert mock.await_count == 1
        assert resp.body == "the real summary"

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

    async def _run(self, proc, *, records: tuple[dict, ...] = (), **kwargs):
        rollout = Path(tempfile.mkdtemp(prefix="cotf-rollout-")) / "rollout.jsonl"
        rollout.write_text(_rollout_text(*records))
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(codex_mod.agent, "_kill_process_tree", new_callable=AsyncMock),
            patch.object(
                codex_mod.transcript,
                "_find_codex_rollout_by_cwd",
                lambda _cwd, **_kw: rollout,
            ),
        ):
            return await codex_mod._run_codex_exec(
                Path("/tmp"), ["codex"], kwargs.get("timeout")
            )

    async def test_a_clean_run_returns_the_parsed_turn(self) -> None:
        parsed = await self._run(
            self._proc(b""),
            records=(_session_meta("t-1"), _task_complete("hi")),
        )
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

    async def test_a_turn_failure_wins_over_the_exit_code(self) -> None:
        """codex records the real reason in `turn_aborted`; the exit code is just
        a number, so the rollout's reason is the better error."""
        with pytest.raises(RuntimeError, match="model refused"):
            await self._run(
                self._proc(b"", b"exit 1 noise", rc=1),
                records=(_turn_aborted("model refused"),),
            )

    async def test_stderr_is_used_when_the_stream_says_nothing(self) -> None:
        with pytest.raises(RuntimeError, match="command not found"):
            await self._run(self._proc(b"", b"command not found: codex", rc=127))

    async def test_a_bare_exit_code_is_reported_when_nothing_else_is_available(
        self,
    ) -> None:
        with pytest.raises(RuntimeError, match="Exit code 3"):
            await self._run(self._proc(b"", b"", rc=3))

    async def test_a_turn_failure_on_a_zero_exit_still_raises(self) -> None:
        """codex can exit 0 having failed the turn, so the rollout has to be
        checked even on a clean exit or the user gets an empty reply and no
        reason."""
        with pytest.raises(RuntimeError, match="context overflow"):
            await self._run(
                self._proc(b"", rc=0), records=(_turn_aborted("context overflow"),)
            )


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


def test_blank_lines_in_the_rollout_are_skipped(tmp_path: Path):
    """Treating a blank line as a parse failure would log a warning per turn."""
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n"
        + _rollout_text(_session_meta("t-1"))
        + "\n   \n"
        + _rollout_text(_task_complete("hi"))
        + "\n"
    )
    parsed = parse_codex_rollout(read_rollout(path, 0)[0])
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


def test_the_agents_own_items_are_not_counted_as_tools():
    """Reasoning, the user's message and the agent's own reply are things the turn
    did, not tools it called. Counting one put a phantom tool in the footer."""
    out = parse_codex_rollout(
        [
            _item("Reasoning"),
            _item("UserMessage"),
            _item("ContextCompaction"),
            _agent_message("done"),
            _item("CommandExecution", command="ls"),
        ]
    )
    assert out["tool_counts"] == {"CommandExecution": 1}


class TestCodexHomeReachesEverySpawn:
    """Rollouts only land in the per-thread home if the child is actually told
    about it, and the chat frontend is not the only caller."""

    async def test_the_spawn_is_told_its_per_thread_codex_home(self, tmp_path):
        """Set on the child rather than published as a session override, because
        the jobs and cron daemons never open a session. A codex turn there would
        otherwise write its rollout into the shared tree the jail no longer grants,
        and then be refused the read back on resume."""
        proc = _exec_proc(0, b"")
        captured: dict = {}

        async def capture(*args, **kwargs):
            captured.update(kwargs)
            return proc

        with (
            patch("asyncio.create_subprocess_exec", capture),
            patch.object(codex_mod.agent, "track_agent_process"),
            patch.object(codex_mod.agent, "_kill_process_tree", AsyncMock()),
        ):
            await codex_mod._run_codex_exec(tmp_path, ["codex", "exec"], timeout=None)
        expected = codex_mod.codex_state.home_dir(tmp_path)
        assert captured["env"]["CODEX_HOME"] == str(expected)
        # And it exists, because on Linux the jail mounts it and an absent mount
        # source takes the whole turn down.
        assert (expected / "sessions").is_dir()


class TestCodexInAPane:
    """The hosted arm runs the interactive TUI, which never exits on its own."""

    @staticmethod
    def _pane(tmp_path: Path):
        from claude_on_the_fly import tmux as tmux_mod

        root = Path(tempfile.mkdtemp(prefix="cotf-t-"))
        with patch.object(tmux_mod, "panes_root", lambda: root):
            return tmux_mod.pane_for("cotf-job-panearm")

    @staticmethod
    def _follower(tmp_path: Path):
        return codex_mod._RolloutFollower(tmp_path, None, None)

    @pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
    async def test_the_turn_ends_when_the_rollout_says_so_not_when_the_tui_exits(
        self, tmp_path: Path
    ):
        """An interactive codex returns to its prompt and waits. Waiting for it to
        exit would spend the whole timeout on a finished turn."""
        from claude_on_the_fly import tmux as tmux_mod

        pane = self._pane(tmp_path)
        follower = self._follower(tmp_path)
        try:
            # Stands in for the TUI: draws, then sits there like codex does.
            task = asyncio.create_task(
                codex_mod._run_codex_in_pane(
                    pane,
                    ["/bin/sh", "-c", "echo TUI-IS-DRAWING; sleep 60"],
                    tmp_path,
                    {**os.environ, **pane.env},
                    follower,
                    timeout=30,
                )
            )
            await asyncio.sleep(2)
            assert tmux_mod.alive(pane) is True
            grid = tmux_mod.capture(pane) or ""
            assert "TUI-IS-DRAWING" in grid

            follower.turn_completed.set()
            returncode, detail = await asyncio.wait_for(task, timeout=10)
        finally:
            tmux_mod.kill(pane)

        assert returncode == 0
        assert detail == ""

    @pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
    async def test_a_tui_that_dies_before_finishing_reports_what_it_printed(
        self, tmp_path: Path
    ):
        """No exit code to read, so the tap is the only account of why."""
        from claude_on_the_fly import tmux as tmux_mod

        pane = self._pane(tmp_path)
        try:
            returncode, detail = await codex_mod._run_codex_in_pane(
                pane,
                ["/bin/sh", "-c", "echo boom-detail; exit 7"],
                tmp_path,
                {**os.environ, **pane.env},
                self._follower(tmp_path),
                timeout=30,
            )
        finally:
            tmux_mod.kill(pane)

        assert returncode == -1
        assert "boom-detail" in detail

    async def test_tmux_refusing_to_host_is_a_failure_with_its_reason(
        self, tmp_path: Path
    ):
        from claude_on_the_fly import tmux as tmux_mod

        pane = self._pane(tmp_path)
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"duplicate session"))
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            pytest.raises(RuntimeError, match="duplicate session"),
        ):
            await codex_mod._run_codex_in_pane(
                pane,
                ["codex", "p"],
                tmp_path,
                dict(os.environ),
                self._follower(tmp_path),
                timeout=5,
            )
        tmux_mod.kill(pane)

    async def test_a_hosted_run_that_overruns_names_the_limit(self, tmp_path: Path):
        from claude_on_the_fly import tmux as tmux_mod

        pane = self._pane(tmp_path)
        ok = MagicMock()
        ok.returncode = 0
        ok.communicate = AsyncMock(return_value=(b"", b""))
        ok.wait = AsyncMock(return_value=0)
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=ok)),
            # Alive but never finishing is the shape this guards against.
            patch.object(tmux_mod, "alive", lambda _p: True),
            patch.object(codex_mod, "_PANE_POLL_S", 0.01),
            pytest.raises(RuntimeError, match=r"timed out after 0\.05s"),
        ):
            await codex_mod._run_codex_in_pane(
                pane,
                ["codex", "p"],
                tmp_path,
                dict(os.environ),
                self._follower(tmp_path),
                timeout=0.05,
            )
        tmux_mod.kill(pane)


class TestWorkspaceTrust:
    """The TUI will not act until the directory is trusted, and nobody is watching
    the pane to answer the dialog."""

    def test_the_stanza_names_the_resolved_path(self, tmp_path: Path):
        home = tmp_path / "home"
        home.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()

        codex_mod._ensure_workspace_trusted(workspace, home)

        written = (home / "config.toml").read_text()
        assert f'[projects."{os.path.realpath(workspace)}"]' in written
        assert 'trust_level = "trusted"' in written

    def test_it_appends_rather_than_replacing_the_operators_config(
        self, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.toml").write_text('model = "gpt-5.6-luna"\n')
        workspace = tmp_path / "ws"
        workspace.mkdir()

        codex_mod._ensure_workspace_trusted(workspace, home)

        written = (home / "config.toml").read_text()
        assert 'model = "gpt-5.6-luna"' in written
        assert "trust_level" in written

    def test_a_workspace_already_trusted_is_left_alone(self, tmp_path: Path):
        home = tmp_path / "home"
        home.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        codex_mod._ensure_workspace_trusted(workspace, home)
        once = (home / "config.toml").read_text()

        codex_mod._ensure_workspace_trusted(workspace, home)

        assert (home / "config.toml").read_text() == once


class TestHostingIsChosenFromTheEnvironment:
    async def test_a_hosted_run_takes_the_pane_arm(self, tmp_path: Path, monkeypatch):
        """`sandbox.session_env` is how the daemon tells a backend it has a pane."""
        seen: dict = {}

        async def fake_pane_arm(pane, argv, workspace, env, follower, timeout):
            seen["session"] = pane.session
            seen["argv"] = argv
            return 0, ""

        monkeypatch.setattr(codex_mod, "_run_codex_in_pane", fake_pane_arm)
        monkeypatch.setattr(
            codex_mod.sandbox,
            "agent_env",
            lambda: {
                "TMUX_TMPDIR": str(tmp_path / "sock"),
                "CLAUDE_PTY_TMUX_SESSION": "cotf-chat-9",
            },
        )
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text(_rollout_text(_task_complete("hosted")))
        with patch.object(
            codex_mod.transcript, "_find_codex_rollout_by_cwd", lambda _c, **_k: rollout
        ):
            out = await codex_mod._run_codex_exec(
                tmp_path,
                ["codex", "exec", "p"],
                None,
                interactive=["codex", "the prompt"],
            )

        assert seen["session"] == "cotf-chat-9"
        # The hosted arm runs the interactive binary, never `codex exec`.
        assert "exec" not in seen["argv"]
        assert out["body"] == "hosted"

    async def test_an_unhosted_run_takes_the_plain_arm(
        self, tmp_path: Path, monkeypatch
    ):
        taken: list[str] = []

        async def fake_plain(wrapped, workspace, env, follower, timeout):
            taken.append("plain")
            return 0, ""

        monkeypatch.setattr(codex_mod, "_run_codex_plain", fake_plain)
        monkeypatch.setattr(codex_mod.sandbox, "agent_env", lambda: {})
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text(_rollout_text(_task_complete("unhosted")))
        with patch.object(
            codex_mod.transcript, "_find_codex_rollout_by_cwd", lambda _c, **_k: rollout
        ):
            out = await codex_mod._run_codex_exec(
                tmp_path, ["codex", "exec", "p"], None
            )

        assert taken == ["plain"]
        assert out["body"] == "unhosted"


async def test_codex_never_inherits_this_processes_stdin(tmp_path: Path):
    """`codex exec` appends piped stdin to the prompt, so an inherited pipe makes
    it print "Reading additional input from stdin..." and block there for the
    whole turn timeout."""
    seen: dict = {}

    async def record(*_args, **kwargs):
        seen.update(kwargs)
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_rollout_text(_task_complete("ok")))
    with (
        patch("asyncio.create_subprocess_exec", record),
        patch.object(codex_mod.agent, "track_agent_process"),
        patch.object(codex_mod.agent, "_kill_process_tree", AsyncMock()),
        patch.object(
            codex_mod.transcript, "_find_codex_rollout_by_cwd", lambda _c, **_k: rollout
        ),
    ):
        await codex_mod._run_codex_exec(tmp_path, ["codex", "exec", "p"], None)

    assert seen["stdin"] == asyncio.subprocess.DEVNULL


class TestRolloutParsingEdges:
    """The shapes a rollout can take that the happy path never produces."""

    def test_a_content_string_is_read_as_its_own_text(self):
        assert codex_mod._rollout_content_text("plain text") == "plain text"

    def test_content_that_is_neither_a_list_nor_a_string_reads_as_empty(self):
        assert codex_mod._rollout_content_text(None) == ""
        assert codex_mod._rollout_content_text(7) == ""

    def test_a_blank_line_between_records_is_not_a_record(self, tmp_path: Path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(_rollout_text(_task_complete("ok")) + "\n\n")
        records, _ = read_rollout(path, 0)
        assert len(records) == 1

    def test_a_rollout_that_cannot_be_sized_reads_as_zero(self, tmp_path: Path):
        assert codex_mod.rollout_size(tmp_path / "nope.jsonl") == 0
        assert codex_mod.rollout_size(None) == 0


class TestRolloutFollowerEdges:
    def test_an_unfindable_rollout_is_simply_nothing_to_follow(self, tmp_path: Path):
        follower = codex_mod._RolloutFollower(tmp_path, None, None)
        with patch.object(
            codex_mod.transcript, "_find_codex_rollout_by_cwd", lambda _c, **_k: None
        ):
            follower._drain()
        assert follower.records == []

    def test_a_read_that_raises_is_logged_rather_than_taking_the_turn_down(
        self, tmp_path: Path, caplog
    ):
        path = tmp_path / "rollout.jsonl"
        path.write_text("")
        follower = codex_mod._RolloutFollower(tmp_path, None, None)
        follower._path = path
        with (
            patch.object(codex_mod, "read_rollout", side_effect=OSError("disk gone")),
            caplog.at_level(logging.ERROR, logger=codex_mod.__name__),
        ):
            follower._drain()
        assert "could not read the rollout" in caplog.text

    def test_records_that_say_nothing_about_the_turn_are_ignored(self, tmp_path: Path):
        relayed: list[str] = []
        follower = codex_mod._RolloutFollower(tmp_path, None, relayed.append)
        for record in (
            {"type": "world_state"},
            {"type": "event_msg", "payload": {"type": "token_count"}},
            _item("CommandExecution"),
            _agent_message("", phase="commentary"),
        ):
            follower._observe(record)
        assert relayed == []
        assert not follower.turn_completed.is_set()

    def test_commentary_with_no_sink_is_simply_dropped(self, tmp_path: Path):
        follower = codex_mod._RolloutFollower(tmp_path, None, None)
        follower._observe(_agent_message("narration", phase="commentary"))

    async def test_closing_reads_what_landed_after_the_last_poll(self, tmp_path: Path):
        """codex writes task_complete and exits, so the last records routinely
        arrive between two polls — and they carry the reply."""
        path = tmp_path / "rollout.jsonl"
        path.write_text("")
        follower = codex_mod._RolloutFollower(tmp_path, None, None)
        follower._path = path
        follower.start()
        path.write_text(_rollout_text(_task_complete("arrived late")))
        await follower.aclose()

        assert parse_codex_rollout(follower.records)["body"] == "arrived late"

    async def test_closing_a_follower_that_never_started_is_safe(self, tmp_path: Path):
        follower = codex_mod._RolloutFollower(tmp_path, None, None)
        await follower.aclose()


class TestPaneArmEdges:
    @staticmethod
    def _pane(tmp_path: Path):
        from claude_on_the_fly import tmux as tmux_mod

        root = Path(tempfile.mkdtemp(prefix="cotf-t-"))
        with patch.object(tmux_mod, "panes_root", lambda: root):
            return tmux_mod.pane_for("cotf-job-edges")

    async def test_a_failed_tap_costs_the_detail_but_not_the_run(
        self, tmp_path: Path, caplog
    ):
        """`pipe-pane` is the only account of a TUI that dies, but losing it must
        not wedge the pane until the timeout."""
        from claude_on_the_fly import tmux as tmux_mod

        pane = self._pane(tmp_path)
        created = MagicMock()
        created.returncode = 0
        created.communicate = AsyncMock(return_value=(b"", b""))
        tap = MagicMock()
        tap.returncode = 1
        tap.communicate = AsyncMock(return_value=(b"", b"no such pane"))
        plain = MagicMock()
        plain.returncode = 0
        plain.wait = AsyncMock(return_value=0)
        procs = [created, tap, plain]
        follower = codex_mod._RolloutFollower(tmp_path, None, None)
        follower.turn_completed.set()
        with (
            patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(side_effect=lambda *_a, **_k: procs.pop(0)),
            ),
            caplog.at_level(logging.WARNING, logger=codex_mod.__name__),
        ):
            returncode, _ = await codex_mod._run_codex_in_pane(
                pane,
                ["codex", "p"],
                tmp_path,
                dict(os.environ),
                follower,
                timeout=None,
            )

        assert "could not tap the pane" in caplog.text
        assert returncode == 0
        tmux_mod.kill(pane)

    def test_output_that_cannot_be_read_explains_nothing_rather_than_raising(
        self, tmp_path: Path
    ):
        assert codex_mod._pane_output_tail(tmp_path / "never-written") == ""


class TestCodexPtyMode:
    """`pty` picks the interface, `agent.pane` picks whether it is mirrored. Keeping
    them apart is what stops a break in codex's UI costing claude-pty its pane."""

    async def test_native_mode_never_offers_an_interactive_argv(
        self, tmp_path: Path, monkeypatch
    ):
        seen: dict = {}

        async def record(workspace, cmd, timeout, thread_id=None, interactive=None):
            seen["interactive"] = interactive
            return _success_result()

        monkeypatch.setattr(codex_mod, "_run_codex_exec", record)
        workspace = tmp_path / "ws"
        workspace.mkdir()

        await CodexBackend().run(workspace, "sess", "hi", "telegram")

        assert seen["interactive"] is None

    async def test_pty_mode_offers_the_interactive_binary(
        self, tmp_path: Path, monkeypatch
    ):
        seen: dict = {}

        async def record(workspace, cmd, timeout, thread_id=None, interactive=None):
            seen["interactive"] = interactive
            return _success_result()

        monkeypatch.setattr(codex_mod, "_run_codex_exec", record)
        workspace = tmp_path / "ws"
        workspace.mkdir()

        await CodexBackend(pty=True).run(workspace, "sess", "hi", "telegram")

        argv = seen["interactive"]
        assert argv is not None
        # The interactive entry point takes no `exec` subcommand.
        assert "exec" not in argv
        assert "--dangerously-bypass-approvals-and-sandbox" in argv

    async def test_pty_mode_resumes_with_the_interactive_subcommand(
        self, tmp_path: Path, monkeypatch
    ):
        """`codex exec resume` and `codex resume` are different entry points."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        argv = CodexBackend(pty=True)._interactive_argv(workspace, "thread-1", "go on")

        assert argv[:2] == ["codex", "resume"]
        assert argv[-2:] == ["thread-1", "go on"]

    async def test_pty_mode_without_a_pane_says_it_is_running_exec(
        self, tmp_path: Path, caplog
    ):
        """The operator asked for a UI. Getting plain lines instead is worth a line."""
        proc = _exec_proc(0)
        with caplog.at_level(logging.INFO, logger=codex_mod.__name__):
            await _run_exec(
                proc,
                tmp_path,
                records=(_task_complete("done"),),
                interactive=["codex", "p"],
            )

        assert "runs `codex exec`" in caplog.text


def test_get_backend_routes_codex_pty_mode(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "codex")
    monkeypatch.setenv("CODEX_MODE", "pty")
    backend = get_backend()

    assert isinstance(backend, CodexBackend)
    assert backend._pty is True


def test_an_unknown_codex_mode_still_names_the_supported_ones(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "codex")
    monkeypatch.setenv("CODEX_MODE", "telepathy")

    with pytest.raises(ValueError, match="native, ollama, pty"):
        get_backend()
