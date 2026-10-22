"""Failure diagnosis over an agent's own transcript.

Every case here is modelled on a real failed cron fire, because the first draft
of these rules passed on invented data and then mislabelled a healthy 51s run
as a stall. The shapes are what the rules have to survive:

- a run killed at its timeout, mid-reasoning, before it ran its own payload
- a run that reached `task_complete` and was still reported as failed
- a run that kept getting errors back from the same tool
- a healthy run, which must stay quiet

The claude arm reads a different file for the same four rules, so its cases are
the same shapes plus the two the format adds: a session file holds every fire of
a keyed job, and it ends in bookkeeping rows that are not events.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_on_the_fly import agent, transcript


def _row(ordinal: int, seconds: int, payload: dict) -> dict:
    return {
        "ordinal": ordinal,
        "timestamp": f"2026-09-03T03:{seconds // 60:02d}:{seconds % 60:02d}.000Z",
        "type": "response_item",
        "payload": payload,
    }


def _call(ordinal: int, seconds: int, call_id: str, cmd: str) -> dict:
    return _row(
        ordinal, seconds, {"type": "custom_tool_call", "call_id": call_id, "input": cmd}
    )


def _output(ordinal: int, seconds: int, call_id: str, out: str) -> dict:
    return _row(
        ordinal,
        seconds,
        {"type": "custom_tool_call_output", "call_id": call_id, "output": out},
    )


def _done(ordinal: int, seconds: int) -> dict:
    row = _row(ordinal, seconds, {"type": "task_complete"})
    row["type"] = "event_msg"
    return row


@pytest.fixture
def rollout(codex_sessions_dir, ndjson, tmp_path, monkeypatch):
    """Write a rollout for `workspace` and return that workspace path.

    Routed through the cwd scan rather than a thread-id mapping on purpose: a
    run that dies inside its first turn never persists a thread id, and that is
    exactly the failure this feature explains.
    """
    workspace = tmp_path / "run-workspace"
    workspace.mkdir()

    def _write(*rows: dict) -> Path:
        day = codex_sessions_dir / "2026" / "09" / "03"
        day.mkdir(parents=True, exist_ok=True)
        path = day / "rollout-2026-09-03T11-30-00-thread-diag.jsonl"
        meta = {
            "timestamp": rows[0]["timestamp"],
            "type": "session_meta",
            "payload": {"id": "thread-diag", "cwd": str(workspace)},
        }
        path.write_bytes(ndjson(meta, *rows[1:]))
        return workspace

    return _write


class TestDiagnoseCodex:
    def test_no_rollout_yields_no_signals(self, codex_sessions_dir, tmp_path):
        assert transcript.diagnose_codex(tmp_path / "nowhere", "uuid") == []

    def test_empty_rollout_yields_no_signals(
        self, codex_sessions_dir, tmp_path, monkeypatch
    ):
        day = codex_sessions_dir / "2026" / "09" / "03"
        day.mkdir(parents=True)
        (day / "rollout-2026-09-03T11-30-00-thread-empty.jsonl").write_bytes(b"")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert transcript.diagnose_codex(workspace, "uuid") == []

    def test_timed_out_run_reports_every_signal(self, rollout):
        """The 2026-09-03 11:30 fire: killed at 600s having run nothing."""
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _call(1, 528, "c1", "sed -n '1,240p' SKILL.md"),
            _output(2, 528, "c1", "Script completed"),
            _row(3, 599, {"type": "reasoning"}),
        )
        signals = transcript.diagnose_codex(
            workspace,
            "uuid",
            prompt="run ~/AveryNexus/scripts/run-deploy-watch.py --team flash",
            timeout_s=600,
        )
        assert signals == [
            "no task_complete, last event was reasoning",
            "599s model / 0.0s tool, stalled upstream",
            "payload never ran, ~/AveryNexus/scripts/run-deploy-watch.py "
            "absent from 1 tool calls",
        ]

    def test_completed_run_names_the_harness_as_the_fault(self, rollout):
        """The 2026-09-02 14:01 fire: the agent finished, cotf still alerted."""
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _call(1, 10, "c1", "run-deploy-watch.py --team flash"),
            _output(2, 40, "c1", "deploy-watch: auto-sent 0 alerts"),
            _done(3, 87),
        )
        assert transcript.diagnose_codex(
            workspace,
            "uuid",
            prompt="run run-deploy-watch.py",
            timeout_s=600,
        ) == ["agent reached task_complete in 87s, failure is ours"]

    def test_a_healthy_length_run_is_not_called_a_stall(self, rollout):
        """51s of model time under a 600s budget is slow, not stalled."""
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _call(1, 50, "c1", "echo hi"),
            _output(2, 50, "c1", "hi"),
            _done(3, 51),
        )
        signals = transcript.diagnose_codex(workspace, "uuid", timeout_s=600)
        assert not any("stalled" in s for s in signals)

    def test_stall_floor_falls_back_when_no_timeout_is_known(self, rollout):
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _call(1, 300, "c1", "echo hi"),
            _output(2, 300, "c1", "hi"),
            _row(3, 301, {"type": "reasoning"}),
        )
        signals = transcript.diagnose_codex(workspace, "uuid")
        assert "301s model / 0.0s tool, stalled upstream" in signals

    def test_repeated_tool_errors_read_as_a_capability_gap(self, rollout):
        """The ask-sme run: three fetches refused, no tool for the job."""
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _call(1, 1, "c1", "open hubspot"),
            _output(2, 2, "c1", "URL is not safe to open"),
            _call(3, 3, "c2", "open hubspot"),
            _output(4, 4, "c2", "URL is not safe to open"),
            _call(5, 5, "c3", "open hubspot"),
            _output(6, 6, "c3", "URL is not safe to open"),
            _done(7, 10),
        )
        signals = transcript.diagnose_codex(workspace, "uuid", timeout_s=600)
        assert "3/3 tool results were errors, capability gap?" in signals

    def test_two_tool_errors_are_not_a_capability_gap(self, rollout):
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _call(1, 1, "c1", "open x"),
            _output(2, 2, "c1", "no such file"),
            _call(3, 3, "c2", "open x"),
            _output(4, 4, "c2", "no such file"),
            _done(5, 10),
        )
        signals = transcript.diagnose_codex(workspace, "uuid", timeout_s=600)
        assert not any("capability gap" in s for s in signals)

    def test_a_payload_that_ran_is_not_reported(self, rollout):
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _call(1, 1, "c1", "uv run --script scripts/run-deploy-watch.py"),
            _output(2, 2, "c1", "ok"),
            _done(3, 10),
        )
        signals = transcript.diagnose_codex(
            workspace,
            "uuid",
            prompt="run scripts/run-deploy-watch.py now",
            timeout_s=600,
        )
        assert not any("payload never ran" in s for s in signals)

    def test_a_run_with_no_tool_calls_skips_the_payload_rule(self, rollout):
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _done(1, 10),
        )
        signals = transcript.diagnose_codex(
            workspace, "uuid", prompt="run thing.py", timeout_s=600
        )
        assert not any("payload never ran" in s for s in signals)

    def test_an_unparsable_timestamp_does_not_break_the_read(self, rollout):
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _done(1, 10),
        )
        day = next(
            iter((workspace.parent / "codex-home" / "sessions").rglob("*.jsonl"))
        )
        day.write_text(
            day.read_text().replace("2026-09-03T03:00:10.000Z", "not-a-timestamp")
        )
        assert transcript.diagnose_codex(workspace, "uuid", timeout_s=600) == [
            "agent reached task_complete in 0s, failure is ours"
        ]

    def test_a_missing_timestamp_does_not_break_the_read(self, rollout):
        workspace = rollout(
            _row(0, 0, {"type": "session_meta"}),
            _done(1, 10),
        )
        day = next(
            iter((workspace.parent / "codex-home" / "sessions").rglob("*.jsonl"))
        )
        day.write_text(
            day.read_text().replace(
                '"timestamp": "2026-09-03T03:00:10.000Z"', '"timestamp": 12345'
            )
        )
        assert transcript.diagnose_codex(workspace, "uuid", timeout_s=600) == [
            "agent reached task_complete in 0s, failure is ours"
        ]

    def test_a_persisted_thread_id_is_preferred_over_the_cwd_scan(
        self, codex_sessions_dir, ndjson, tmp_path
    ):
        from claude_on_the_fly import codex_state

        workspace = tmp_path / "keyed"
        workspace.mkdir()
        codex_state.write_thread_id(workspace, "uuid", "thread-keyed")
        day = codex_sessions_dir / "2026" / "09" / "03"
        day.mkdir(parents=True)
        (day / "rollout-2026-09-03T11-30-00-thread-keyed.jsonl").write_bytes(
            ndjson(
                {
                    "timestamp": "2026-09-03T03:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "thread-keyed", "cwd": "/elsewhere"},
                },
                _done(1, 42),
            )
        )
        assert transcript.diagnose_codex(workspace, "uuid", timeout_s=600) == [
            "agent reached task_complete in 42s, failure is ours"
        ]


def _stamp(seconds: int) -> str:
    return f"2026-09-03T03:{seconds // 60:02d}:{seconds % 60:02d}.000Z"


def _asked(seconds: int, text: str = "run scripts/deploy.py", **extra) -> dict:
    """A prompt row: the boundary one fire starts at."""
    return {
        "type": "user",
        "timestamp": _stamp(seconds),
        "message": {"role": "user", "content": text},
        **extra,
    }


def _said(seconds: int, *blocks: dict, stop: str = "tool_use", **extra) -> dict:
    return {
        "type": "assistant",
        "timestamp": _stamp(seconds),
        "message": {"role": "assistant", "content": list(blocks), "stop_reason": stop},
        **extra,
    }


def _answered(seconds: int, text: str = "done") -> dict:
    return _said(seconds, {"type": "text", "text": text}, stop="end_turn")


def _uses(call_id: str, command: str) -> dict:
    return {
        "type": "tool_use",
        "id": call_id,
        "name": "Bash",
        "input": {"command": command},
    }


def _returned(seconds: int, call_id: str, out: str, error: bool = False, **extra):
    return {
        "type": "user",
        "timestamp": _stamp(seconds),
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": out,
                    "is_error": error,
                }
            ],
        },
        **extra,
    }


@pytest.fixture
def session(claude_projects_dir, ndjson, tmp_path):
    """Write a claude session file for `workspace` and return that workspace."""
    workspace = tmp_path / "claude-workspace"
    workspace.mkdir()

    def _write(*rows: dict) -> Path:
        path = transcript.claude_session_dir(workspace) / "uuid.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ndjson(*rows) if rows else b"")
        return workspace

    return _write


class TestDiagnoseClaude:
    """Same four rules, read out of claude's session JSONL."""

    def test_no_session_file_yields_no_signals(self, claude_projects_dir, tmp_path):
        assert transcript.diagnose_claude(tmp_path / "nowhere", "uuid") == []

    def test_an_empty_session_file_yields_no_signals(self, session):
        assert transcript.diagnose_claude(session(), "uuid") == []

    def test_a_timed_out_run_reports_every_signal(self, session):
        """Killed at its budget holding a tool call, having never run its own
        payload -- the codex case's shape, in claude's format."""
        workspace = session(
            _asked(0),
            _said(10, _uses("t1", "sed -n '1,240p' SKILL.md")),
            _returned(11, "t1", "ok"),
            _said(599, _uses("t2", "grep -r thing .")),
        )
        assert transcript.diagnose_claude(
            workspace,
            "uuid",
            prompt="run scripts/deploy.py --team flash",
            timeout_s=600,
        ) == [
            "no end_turn, last event was assistant/tool_use",
            "598s model / 1.0s tool, stalled upstream",
            "payload never ran, scripts/deploy.py absent from 2 tool calls",
        ]

    def test_a_finished_run_names_the_harness_as_the_fault(self, session):
        workspace = session(
            _asked(0),
            _said(10, _uses("t1", "scripts/deploy.py")),
            _returned(40, "t1", "deployed"),
            _answered(87),
        )
        assert transcript.diagnose_claude(
            workspace, "uuid", prompt="run scripts/deploy.py", timeout_s=600
        ) == ["agent reached end_turn in 87s, failure is ours"]

    def test_only_the_newest_fire_is_read(self, session):
        """A keyed job resumes its session, so yesterday's fire is in the same
        file. Reading it would time this run in hours and blame its tools."""
        workspace = session(
            _asked(0),
            _said(1, _uses("old1", "x")),
            _returned(2, "old1", "no such file", error=True),
            _said(3, _uses("old2", "x")),
            _returned(4, "old2", "no such file", error=True),
            _said(5, _uses("old3", "x")),
            _returned(6, "old3", "no such file", error=True),
            _answered(7),
            _asked(100),
            _said(101, _uses("new1", "scripts/deploy.py")),
            _returned(140, "new1", "ok"),
            _answered(150),
        )
        assert transcript.diagnose_claude(
            workspace, "uuid", prompt="run scripts/deploy.py", timeout_s=600
        ) == ["agent reached end_turn in 50s, failure is ours"]

    def test_the_bookkeeping_tail_is_not_the_last_event(self, session):
        """claude ends a file with `mode` and `ai-title` rows that carry no
        timestamp. Reporting one as the last event names nothing an operator
        can look up."""
        workspace = session(
            _asked(0),
            _said(30, _uses("t1", "x")),
            {"type": "ai-title", "title": "a run"},
            {"type": "mode", "mode": "normal"},
        )
        signals = transcript.diagnose_claude(workspace, "uuid", timeout_s=600)
        assert signals[0] == "no end_turn, last event was assistant/tool_use"

    def test_a_finished_turn_is_still_finished_under_its_own_tail(self, session):
        """A completed turn writes `system` rows after its last message -- the
        stop hook and the turn duration. Reading the stop reason off the last row
        called 9 of 96 real finished fires unfinished."""
        workspace = session(
            _asked(0),
            _said(10, _uses("t1", "scripts/deploy.py")),
            _returned(40, "t1", "ok"),
            _answered(87),
            {
                "type": "system",
                "subtype": "stop_hook_summary",
                "timestamp": _stamp(88),
            },
            {"type": "system", "subtype": "turn_duration", "timestamp": _stamp(88)},
            {"type": "last-prompt"},
        )
        assert transcript.diagnose_claude(workspace, "uuid", timeout_s=600) == [
            "agent reached end_turn in 88s, failure is ours"
        ]

    def test_an_api_error_is_named_by_what_the_agent_was_doing(self, session):
        """The row after the failure is a `system` row, which names nothing an
        operator can act on. The message row does."""
        workspace = session(
            _asked(0),
            _said(10, _uses("t1", "curl thing")),
            _returned(70, "t1", "Proxy connection ended"),
            _said(
                200,
                {"type": "text", "text": "API Error: Unable to connect"},
                stop="stop_sequence",
            ),
            {"type": "system", "subtype": "post_turn", "timestamp": _stamp(201)},
        )
        signals = transcript.diagnose_claude(workspace, "uuid", timeout_s=600)
        assert signals[0] == "no end_turn, last event was assistant/stop_sequence"

    def test_a_run_with_no_prompt_row_is_read_whole(self, session):
        """No boundary to find, so there is nothing to trim to."""
        workspace = session(_said(10, _uses("t1", "x")), _answered(20))
        assert transcript.diagnose_claude(workspace, "uuid", timeout_s=600) == [
            "agent reached end_turn in 10s, failure is ours"
        ]

    def test_rows_without_timestamps_do_not_break_the_read(self, session):
        workspace = session({"type": "mode", "mode": "normal"})
        assert transcript.diagnose_claude(workspace, "uuid", timeout_s=600) == [
            "no end_turn, last event was mode"
        ]

    def test_repeated_tool_errors_read_as_a_capability_gap(self, session):
        workspace = session(
            _asked(0),
            _said(1, _uses("t1", "open hubspot")),
            _returned(2, "t1", "refused", error=True),
            _said(3, _uses("t2", "open hubspot")),
            _returned(4, "t2", "refused", error=True),
            _said(5, _uses("t3", "open hubspot")),
            _returned(6, "t3", "URL is not safe to open"),
            _answered(10),
        )
        assert (
            "3/3 tool results were errors, capability gap?"
            in transcript.diagnose_claude(workspace, "uuid", timeout_s=600)
        )

    def test_a_subagents_work_counts_as_this_runs_work(self, session):
        """A delegating run spends its wall clock inside a sidechain. Ignoring
        those rows reads it as a stall, and its prompt as one never run."""
        workspace = session(
            _asked(0),
            _said(1, _uses("t1", "Task: deploy")),
            _asked(2, "run scripts/deploy.py", isSidechain=True),
            _said(3, _uses("s1", "scripts/deploy.py"), isSidechain=True),
            _returned(590, "s1", "ok", isSidechain=True),
            _returned(591, "t1", "subagent done"),
            _said(599, _uses("t2", "echo x")),
        )
        assert transcript.diagnose_claude(
            workspace, "uuid", prompt="run scripts/deploy.py", timeout_s=600
        ) == ["no end_turn, last event was assistant/tool_use"]

    def test_a_healthy_length_run_is_not_called_a_stall(self, session):
        workspace = session(
            _asked(0),
            _said(1, _uses("t1", "echo hi")),
            _returned(2, "t1", "hi"),
            _answered(51),
        )
        signals = transcript.diagnose_claude(workspace, "uuid", timeout_s=600)
        assert not any("stalled" in signal for signal in signals)

    def test_the_stall_floor_falls_back_when_no_timeout_is_known(self, session):
        workspace = session(
            _asked(0),
            _said(1, _uses("t1", "echo hi")),
            _returned(1, "t1", "hi"),
            _said(301, _uses("t2", "echo hi")),
        )
        assert "301s model / 0.0s tool, stalled upstream" in transcript.diagnose_claude(
            workspace, "uuid"
        )

    def test_a_run_with_no_tool_calls_skips_the_payload_rule(self, session):
        workspace = session(_asked(0), _answered(10))
        signals = transcript.diagnose_claude(
            workspace, "uuid", prompt="run thing.py", timeout_s=600
        )
        assert not any("payload never ran" in signal for signal in signals)

    def test_a_text_only_result_block_is_still_paired(self, session):
        """An unpaired id must not be counted as time nobody spent."""
        workspace = session(
            _asked(0),
            _said(1, _uses("t1", "echo hi")),
            _returned(9, "mismatched-id", "hi"),
            _answered(10),
        )
        assert transcript.diagnose_claude(workspace, "uuid", timeout_s=600) == [
            "agent reached end_turn in 10s, failure is ours"
        ]


def _profile(backend: str = "codex") -> agent.AgentProfile:
    """A resolved profile, which is what `_failure_signals` gates on now."""
    return agent.AgentProfile(backend=backend, mode="native", model="", effort="")


class TestFailureSignalsWiring:
    """`_failure_signals` is the gate: opt-in, per-backend, and never fatal."""

    def test_off_by_default(self, monkeypatch, tmp_path):
        from claude_on_the_fly.jobs import agent_runner

        monkeypatch.delenv("JOBS_DIAGNOSE_FAILURES", raising=False)
        called = False

        def _never(*_a, **_k):
            nonlocal called
            called = True
            return ["should not be reached"]

        monkeypatch.setattr(agent_runner.transcript, "diagnose", _never)
        assert (
            agent_runner._failure_signals(tmp_path, "uuid", "", 600, _profile()) == ""
        )
        assert called is False

    def test_each_backend_is_read_by_its_own_reader(self, monkeypatch, tmp_path):
        """The profile's backend picks the reader, so a switched profile is never
        diagnosed against a transcript format it did not write."""
        from claude_on_the_fly.jobs import agent_runner

        monkeypatch.setenv("JOBS_DIAGNOSE_FAILURES", "true")
        seen: list[str] = []
        for name in ("diagnose_codex", "diagnose_claude"):
            monkeypatch.setattr(
                agent_runner.transcript,
                name,
                lambda *_a, _n=name, **_k: seen.append(_n) or [_n],
            )
        monkeypatch.setattr(
            agent_runner.transcript,
            "_DIAGNOSERS",
            {
                "codex": agent_runner.transcript.diagnose_codex,
                "claude": agent_runner.transcript.diagnose_claude,
            },
        )
        assert (
            agent_runner._failure_signals(tmp_path, "uuid", "", 600, _profile("claude"))
            == "\n- diagnose_claude"
        )
        assert (
            agent_runner._failure_signals(tmp_path, "uuid", "", 600, _profile("codex"))
            == "\n- diagnose_codex"
        )
        assert seen == ["diagnose_claude", "diagnose_codex"]

    def test_a_backend_with_no_reader_gets_nothing(self, monkeypatch, tmp_path):
        from claude_on_the_fly.jobs import agent_runner

        monkeypatch.setenv("JOBS_DIAGNOSE_FAILURES", "true")
        assert (
            agent_runner._failure_signals(tmp_path, "uuid", "", 600, _profile("ollama"))
            == ""
        )

    def test_signals_render_as_bullets(self, monkeypatch, tmp_path):
        from claude_on_the_fly.jobs import agent_runner

        monkeypatch.setenv("JOBS_DIAGNOSE_FAILURES", "1")
        monkeypatch.setattr(
            agent_runner.transcript,
            "diagnose",
            lambda *_a, **_k: [
                "no task_complete, last event was reasoning",
                "599s model / 0.2s tool, stalled upstream",
            ],
        )
        assert agent_runner._failure_signals(tmp_path, "uuid", "", 600, _profile()) == (
            "\n- no task_complete, last event was reasoning"
            "\n- 599s model / 0.2s tool, stalled upstream"
        )

    def test_a_broken_read_does_not_replace_the_real_error(
        self, monkeypatch, tmp_path, caplog
    ):
        from claude_on_the_fly.jobs import agent_runner

        monkeypatch.setenv("JOBS_DIAGNOSE_FAILURES", "yes")

        def _boom(*_a, **_k):
            raise OSError("rollout store went away")

        monkeypatch.setattr(agent_runner.transcript, "diagnose", _boom)
        assert (
            agent_runner._failure_signals(tmp_path, "uuid", "", 600, _profile()) == ""
        )
        assert "could not diagnose" in caplog.text


class TestRollutLookupUnderLoad:
    """The regression the unit tests missed and a real run caught.

    Every case above writes one rollout, so any lookup passes. On the host this
    was built for, cron fires every 15 minutes and the failed run is buried
    under newer rollouts. Checking only the freshest found nothing.
    """

    def test_finds_a_run_that_is_not_the_freshest_rollout(
        self, codex_sessions_dir, ndjson, tmp_path
    ):
        import os
        import time

        day = codex_sessions_dir / "2026" / "09" / "03"
        day.mkdir(parents=True)
        wanted = tmp_path / "wanted-workspace"
        wanted.mkdir()

        def _write(name: str, cwd: str, mtime: float) -> Path:
            path = day / name
            path.write_bytes(
                ndjson(
                    {
                        "timestamp": "2026-09-03T03:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": name, "cwd": cwd},
                    },
                    _done(1, 90),
                )
            )
            os.utime(path, (mtime, mtime))
            return path

        now = time.time()
        target = _write(
            "rollout-2026-09-03T11-30-00-thread-target.jsonl", str(wanted), now - 300
        )
        for index in range(5):
            _write(
                f"rollout-2026-09-03T11-4{index}-00-thread-newer{index}.jsonl",
                str(tmp_path / f"other-{index}"),
                now - index,
            )

        assert (
            transcript._find_finished_rollout_by_cwd(str(wanted), max_age_s=3600)
            == target
        )
        assert transcript.diagnose_codex(wanted, "uuid", timeout_s=600) == [
            "agent reached task_complete in 90s, failure is ours"
        ]

    def test_a_rollout_older_than_the_window_is_not_considered(
        self, codex_sessions_dir, ndjson, tmp_path
    ):
        import os
        import time

        day = codex_sessions_dir / "2026" / "09" / "03"
        day.mkdir(parents=True)
        stale = tmp_path / "stale-workspace"
        stale.mkdir()
        path = day / "rollout-2026-09-03T01-00-00-thread-stale.jsonl"
        path.write_bytes(
            ndjson(
                {
                    "timestamp": "2026-09-03T03:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "stale", "cwd": str(stale)},
                },
                _done(1, 10),
            )
        )
        old = time.time() - 7200
        os.utime(path, (old, old))
        assert (
            transcript._find_finished_rollout_by_cwd(str(stale), max_age_s=3600) is None
        )

    def test_an_empty_cwd_is_not_looked_up(self):
        assert transcript._find_finished_rollout_by_cwd("", max_age_s=3600) is None

    def test_an_unreadable_candidate_is_skipped(
        self, codex_sessions_dir, ndjson, tmp_path, monkeypatch
    ):
        day = codex_sessions_dir / "2026" / "09" / "03"
        day.mkdir(parents=True)
        wanted = tmp_path / "ws"
        wanted.mkdir()
        path = day / "rollout-2026-09-03T11-30-00-thread-ok.jsonl"
        path.write_bytes(
            ndjson(
                {
                    "timestamp": "2026-09-03T03:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "ok", "cwd": str(wanted)},
                },
                _done(1, 10),
            )
        )
        real_stat = Path.stat

        def _flaky(self, *args, **kwargs):
            if self.name.endswith("thread-ok.jsonl"):
                raise OSError("vanished mid-scan")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _flaky)
        assert (
            transcript._find_finished_rollout_by_cwd(str(wanted), max_age_s=3600)
            is None
        )

    def test_a_non_session_meta_first_line_is_not_matched(
        self, codex_sessions_dir, ndjson, tmp_path
    ):
        day = codex_sessions_dir / "2026" / "09" / "03"
        day.mkdir(parents=True)
        wanted = tmp_path / "ws2"
        wanted.mkdir()
        path = day / "rollout-2026-09-03T11-30-00-thread-odd.jsonl"
        path.write_bytes(ndjson(_done(0, 1), _done(1, 10)))
        assert (
            transcript._find_finished_rollout_by_cwd(str(wanted), max_age_s=3600)
            is None
        )


class TestSignalsSurviveTheAlert:
    """The alert body is capped from the tail, so placement is not cosmetic."""

    # A misconfigured backend can no longer reach here: the runner resolves the
    # profile before calling this, and refuses the job when it will not resolve.
    # That case is covered at its new home, `test_agent_profiles.py`'s
    # `test_a_bad_profile_name_fails_the_job_not_the_worker`, and the generic
    # guard is still covered by the OSError case above.

    def test_signals_survive_a_body_that_overflows_the_alert_cap(self):
        from claude_on_the_fly.jobs.alerts import ALERT_BODY_LIMIT, _alert_body
        from claude_on_the_fly.jobs.core import Result

        banner = "OpenAI Codex v0.152.0 banner line\n" * 40
        assert len(banner) > ALERT_BODY_LIMIT
        notes = "\n- no task_complete, last event was reasoning"
        rendered = _alert_body(
            {"kind": "cron", "entry": "watch-deploy"},
            Result(ok=False, text=f"Job failed:{notes}\n{banner}"),
        )
        assert "no task_complete, last event was reasoning" in rendered

    def test_appending_instead_would_have_lost_them(self):
        """Guards the ordering: the naive shape drops the diagnosis."""
        from claude_on_the_fly.jobs.alerts import _alert_body
        from claude_on_the_fly.jobs.core import Result

        banner = "OpenAI Codex v0.152.0 banner line\n" * 40
        notes = "\n- no task_complete, last event was reasoning"
        rendered = _alert_body(
            {"kind": "cron", "entry": "watch-deploy"},
            Result(ok=False, text=f"Job failed: {banner}{notes}"),
        )
        assert "no task_complete" not in rendered


class TestRollutRemovedMidDiagnosis:
    def test_a_rollout_deleted_after_the_lookup_yields_no_signals(
        self, codex_sessions_dir, ndjson, tmp_path, monkeypatch
    ):
        """`sweep_run_workspaces` can retire a run while its failure is read.

        The lookup opens the first line and the rule pass reads the whole file,
        so the file can go away in between. Two reads, two chances to lose it.
        """
        day = codex_sessions_dir / "2026" / "09" / "03"
        day.mkdir(parents=True)
        workspace = tmp_path / "doomed"
        workspace.mkdir()
        path = day / "rollout-2026-09-03T11-30-00-thread-doomed.jsonl"
        path.write_bytes(
            ndjson(
                {
                    "timestamp": "2026-09-03T03:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"id": "doomed", "cwd": str(workspace)},
                },
                _done(1, 10),
            )
        )
        real_read_bytes = Path.read_bytes

        def _vanished(self):
            if self.name.endswith("thread-doomed.jsonl"):
                raise OSError("removed by the workspace sweep")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", _vanished)
        assert transcript.diagnose_codex(workspace, "uuid", timeout_s=600) == []
