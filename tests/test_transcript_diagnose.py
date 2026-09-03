"""Failure diagnosis over a codex rollout.

Every case here is modelled on a real failed cron fire, because the first draft
of these rules passed on invented data and then mislabelled a healthy 51s run
as a stall. The shapes are what the rules have to survive:

- a run killed at its timeout, mid-reasoning, before it ran its own payload
- a run that reached `task_complete` and was still reported as failed
- a run that kept getting errors back from the same tool
- a healthy run, which must stay quiet
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


def _profile(backend: str = "codex") -> agent.AgentProfile:
    """A resolved profile, which is what `_failure_signals` gates on now."""
    return agent.AgentProfile(backend=backend, mode="native", model="", effort="")


class TestFailureSignalsWiring:
    """`_failure_signals` is the gate: opt-in, codex-only, and never fatal."""

    def test_off_by_default(self, monkeypatch, tmp_path):
        from claude_on_the_fly.jobs import agent_runner

        monkeypatch.delenv("JOBS_DIAGNOSE_FAILURES", raising=False)
        called = False

        def _never(*_a, **_k):
            nonlocal called
            called = True
            return ["should not be reached"]

        monkeypatch.setattr(agent_runner.transcript, "diagnose_codex", _never)
        assert (
            agent_runner._failure_signals(tmp_path, "uuid", "", 600, _profile()) == ""
        )
        assert called is False

    def test_claude_backend_gets_nothing(self, monkeypatch, tmp_path):
        from claude_on_the_fly.jobs import agent_runner

        monkeypatch.setenv("JOBS_DIAGNOSE_FAILURES", "true")
        monkeypatch.setattr(
            agent_runner.transcript,
            "diagnose_codex",
            lambda *_a, **_k: ["should not be reached"],
        )
        signals = agent_runner._failure_signals(
            tmp_path, "uuid", "", 600, _profile("claude")
        )
        assert signals == ""

    def test_signals_render_as_bullets(self, monkeypatch, tmp_path):
        from claude_on_the_fly.jobs import agent_runner

        monkeypatch.setenv("JOBS_DIAGNOSE_FAILURES", "1")
        monkeypatch.setattr(
            agent_runner.transcript,
            "diagnose_codex",
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

        monkeypatch.setattr(agent_runner.transcript, "diagnose_codex", _boom)
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
