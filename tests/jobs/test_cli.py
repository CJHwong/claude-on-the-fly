"""jobs.cli: argv normalization, enqueue producer, and config-from-env helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_on_the_fly.jobs import cli


def test_normalize_argv_bare_defaults_to_run() -> None:
    assert cli._normalize_argv([]) == ["run"]


def test_normalize_argv_keeps_subcommands() -> None:
    assert cli._normalize_argv(["doctor"]) == ["doctor"]
    assert cli._normalize_argv(["enqueue", "hi"]) == ["enqueue", "hi"]
    assert cli._normalize_argv(["-h"]) == ["-h"]


def test_poll_interval_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("JOBS_POLL_INTERVAL_S", raising=False)
    assert cli._poll_interval_s() == cli.DEFAULT_POLL_INTERVAL_S
    monkeypatch.setenv("JOBS_POLL_INTERVAL_S", "0.5")
    assert cli._poll_interval_s() == 0.5
    monkeypatch.setenv("JOBS_POLL_INTERVAL_S", "notanumber")
    assert cli._poll_interval_s() == cli.DEFAULT_POLL_INTERVAL_S


def test_timeout_default_and_override(monkeypatch) -> None:
    from claude_on_the_fly import agent

    monkeypatch.delenv("JOBS_TIMEOUT", raising=False)
    assert cli._timeout_s() == agent.DEFAULT_TIMEOUT
    monkeypatch.setenv("JOBS_TIMEOUT", "120")
    assert cli._timeout_s() == 120.0


def test_timeout_non_positive_means_no_limit(monkeypatch) -> None:
    # 0 or negative JOBS_TIMEOUT = "no limit" → None, so agent.run skips wait_for
    # rather than firing an immediate/negative timeout.
    monkeypatch.setenv("JOBS_TIMEOUT", "0")
    assert cli._timeout_s() is None
    monkeypatch.setenv("JOBS_TIMEOUT", "-5")
    assert cli._timeout_s() is None


def test_enqueue_writes_job_to_queue(monkeypatch, tmp_path: Path, capsys) -> None:
    from claude_on_the_fly import agent

    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.delenv("JOBS_QUEUE_KIND", raising=False)

    rc = cli._cmd_enqueue("summarize the logs", channel="C1", thread_ts="1699.5")
    assert rc == 0

    out = capsys.readouterr().out
    assert out.startswith("queued job ")

    new_files = list((tmp_path / "jobs" / "new").glob("*.json"))
    assert len(new_files) == 1
    payload = json.loads(new_files[0].read_text())
    assert payload["prompt"] == "summarize the logs"
    assert payload["origin"] == {
        "channel": "C1",
        "thread_ts": "1699.5",
        "sender_id": "cli",
    }


def test_resolve_jobs_token_prefers_override(monkeypatch) -> None:
    name, token = cli.checks.resolve_jobs_token(
        {"JOBS_SLACK_TOKEN": "xoxb-jobs", "SLACK_TOKEN": "xoxp-frontend"}
    )
    assert name == "JOBS_SLACK_TOKEN"
    assert token == "xoxb-jobs"


def test_resolve_jobs_token_falls_back_to_slack_token() -> None:
    name, token = cli.checks.resolve_jobs_token({"SLACK_TOKEN": "xoxp-frontend"})
    assert name == "SLACK_TOKEN"
    assert token == "xoxp-frontend"


def test_loop_warning_fires_for_inherited_user_token() -> None:
    # Inheriting a user token from SLACK_TOKEN is the loop-prone default.
    assert cli._notifier_loop_warning("SLACK_TOKEN", "xoxp-abc") is not None


def test_loop_warning_silent_for_bot_token() -> None:
    assert cli._notifier_loop_warning("SLACK_TOKEN", "xoxb-abc") is None


def test_loop_warning_silent_for_explicit_override() -> None:
    # Deployer chose JOBS_SLACK_TOKEN explicitly — even a user token is their call.
    assert cli._notifier_loop_warning("JOBS_SLACK_TOKEN", "xoxp-abc") is None


def test_run_refuses_to_start_beside_a_live_worker(monkeypatch, capsys) -> None:
    """The worker is a singleton and only supervisor.spawn enforced it, so a
    hand-started `claude-jobs` next to a supervised one would run
    recover_stale(None) and steal the job the live worker is executing."""
    monkeypatch.setattr(cli, "live_pid", lambda frontend: 9999)
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)

    def _must_not_run(*args, **kwargs):
        raise AssertionError("started a second worker on a live queue")

    monkeypatch.setattr(cli, "check_backend", _must_not_run)
    monkeypatch.setattr(cli.asyncio, "run", _must_not_run)

    assert cli._cmd_run() == 2
    assert "already running (pid 9999)" in capsys.readouterr().err


def test_run_proceeds_when_no_worker_is_live(monkeypatch) -> None:
    monkeypatch.setattr(cli, "live_pid", lambda frontend: None)
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    monkeypatch.setattr(cli, "check_backend", lambda: None)
    monkeypatch.setattr(
        cli.checks, "resolve_jobs_token", lambda env: ("JOBS_SLACK_TOKEN", "xoxb-t")
    )
    ran: list[str] = []
    monkeypatch.setattr(
        cli.asyncio, "run", lambda coro: (coro.close(), ran.append("x"))
    )

    assert cli._cmd_run() == 0
    assert ran == ["x"]


def test_concurrency_below_one_falls_back_to_one(monkeypatch, caplog) -> None:
    """A junk or non-positive value must not refuse to start: the worker running
    slowly beats the worker not running."""
    monkeypatch.setenv("JOBS_CONCURRENCY", "0")
    with caplog.at_level("WARNING", logger="claude_on_the_fly.jobs.cli"):
        assert cli._concurrency() == 1
    assert "below 1, using 1" in "\n".join(r.getMessage() for r in caplog.records)


def test_concurrency_honours_a_sane_value(monkeypatch) -> None:
    monkeypatch.setenv("JOBS_CONCURRENCY", "4")
    assert cli._concurrency() == 4


def test_run_without_a_token_refuses_and_names_both_vars(monkeypatch, capsys) -> None:
    """The notifier is the only way a job's answer reaches anyone, so an install
    with no token would run jobs whose results go nowhere."""
    monkeypatch.setattr(cli, "live_pid", lambda frontend: None)
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    monkeypatch.setattr(cli, "check_backend", lambda: None)
    monkeypatch.setattr(cli.checks, "resolve_jobs_token", lambda env: ("", ""))

    def _must_not_run(*args, **kwargs):
        raise AssertionError("started the worker with no notifier token")

    monkeypatch.setattr(cli.asyncio, "run", _must_not_run)
    assert cli._cmd_run() == 2
    err = capsys.readouterr().err
    assert "JOBS_SLACK_TOKEN" in err and "SLACK_TOKEN" in err


def test_run_warns_about_an_inherited_user_token(monkeypatch, caplog) -> None:
    monkeypatch.setattr(cli, "live_pid", lambda frontend: None)
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    monkeypatch.setattr(cli, "check_backend", lambda: None)
    monkeypatch.setattr(
        cli.checks, "resolve_jobs_token", lambda env: ("SLACK_TOKEN", "xoxp-user")
    )
    monkeypatch.setattr(cli.asyncio, "run", lambda coro: coro.close())
    with caplog.at_level("WARNING", logger="claude_on_the_fly.jobs.cli"):
        assert cli._cmd_run() == 0
    assert caplog.records, "an inherited user token deserves a warning"


class TestDoctor:
    def _result(self, name, status, detail="", fix_hint=""):
        from claude_on_the_fly.checks import CheckResult

        return CheckResult(name=name, status=status, detail=detail, fix_hint=fix_hint)

    def test_all_ok_reports_success(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            cli.checks, "check_frontend", lambda *_a: [self._result("token", "ok")]
        )
        monkeypatch.setattr(
            cli.checks, "check_backend", lambda *_a: [self._result("cli", "ok")]
        )
        assert cli._cmd_doctor() == 0
        assert "all checks passed" in capsys.readouterr().out

    def test_a_blocking_failure_exits_nonzero_with_its_hint(
        self, monkeypatch, capsys
    ) -> None:
        bad = self._result("token", "fail", "missing", "set JOBS_SLACK_TOKEN")
        monkeypatch.setattr(cli.checks, "check_frontend", lambda *_a: [bad])
        monkeypatch.setattr(cli.checks, "check_backend", lambda *_a: [])
        monkeypatch.setattr(cli.checks, "is_blocking", lambda _r: True)
        assert cli._cmd_doctor() == 1
        out = capsys.readouterr().out
        assert "1 check(s) failed" in out
        assert "hint: set JOBS_SLACK_TOKEN" in out

    def test_an_advisory_result_warns_without_failing(
        self, monkeypatch, capsys
    ) -> None:
        """An enqueue-only worker is a legitimate install, not an error."""
        advisory = self._result("worker", "warn", "no producer", "start one")
        monkeypatch.setattr(cli.checks, "check_frontend", lambda *_a: [advisory])
        monkeypatch.setattr(cli.checks, "check_backend", lambda *_a: [])
        monkeypatch.setattr(cli.checks, "is_blocking", lambda _r: False)
        assert cli._cmd_doctor() == 0
        assert "all checks passed (1 warning(s))" in capsys.readouterr().out


class TestMainDispatch:
    def test_doctor_is_dispatched(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "load_dotenv", lambda: None)
        monkeypatch.setattr(cli.sys, "argv", ["claude-jobs", "doctor"])
        monkeypatch.setattr(cli, "_cmd_doctor", lambda: 7)
        assert cli.main() == 7

    def test_enqueue_is_dispatched_with_its_options(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "load_dotenv", lambda: None)
        monkeypatch.setattr(
            cli.sys,
            "argv",
            [
                "claude-jobs",
                "enqueue",
                "do it",
                "--channel",
                "C1",
                "--thread-ts",
                "1.1",
            ],
        )
        seen: list[tuple] = []
        monkeypatch.setattr(
            cli, "_cmd_enqueue", lambda *args: (seen.append(args), 0)[1]
        )
        assert cli.main() == 0
        assert seen == [("do it", "C1", "1.1")]

    def test_a_bare_invocation_runs_the_worker(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "load_dotenv", lambda: None)
        monkeypatch.setattr(cli.sys, "argv", ["claude-jobs"])
        monkeypatch.setattr(cli, "_cmd_run", lambda: 3)
        assert cli.main() == 3


class TestRunLoopWiring:
    async def test_orphans_are_reaped_before_anything_claims_work(
        self, monkeypatch, caplog, tmp_path
    ) -> None:
        """run_loop's first act is recover_stale, and re-running a job whose earlier
        copy is still executing is exactly what the sweep prevents."""
        from unittest.mock import AsyncMock, MagicMock

        ledger = MagicMock()
        ledger.sweep.return_value = 2
        monkeypatch.setattr(cli, "ProcessLedger", lambda _path: ledger)
        monkeypatch.setattr(
            cli,
            "build_components",
            lambda *_a: (MagicMock(), MagicMock(), MagicMock(), MagicMock(), None),
        )
        order: list[str] = []
        ledger.sweep.side_effect = lambda: (order.append("sweep"), 2)[1]

        async def fake_run_loop(*_args, **_kwargs):
            order.append("run_loop")

        monkeypatch.setattr(cli, "run_loop", fake_run_loop)
        heartbeat = MagicMock()
        heartbeat.run = AsyncMock()
        heartbeat.path = tmp_path / "hb.json"
        monkeypatch.setattr(cli, "HeartbeatWriter", lambda _role, **kwargs: heartbeat)

        with caplog.at_level("WARNING", logger="claude_on_the_fly.jobs.cli"):
            await cli._run("xoxb-token")

        assert order == ["sweep", "run_loop"]
        assert "reaped 2 orphaned agent process group(s)" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_finished_workspaces_are_retired_before_the_loop_claims_work(
        self, monkeypatch, caplog, tmp_path
    ) -> None:
        """Retention runs at startup, not per job: an rmtree on the run path
        competes with the in-flight cancel for the supervisor's 5s grace. Before
        the loop claims anything, so it also never competes with a live job."""
        from unittest.mock import AsyncMock, MagicMock

        ledger = MagicMock()
        ledger.sweep.return_value = 0
        monkeypatch.setattr(cli, "ProcessLedger", lambda _path: ledger)
        monkeypatch.setattr(
            cli,
            "build_components",
            lambda *_a: (MagicMock(), MagicMock(), MagicMock(), MagicMock(), None),
        )
        order: list[str] = []
        seen: dict[str, object] = {}

        def fake_sweep(data_dir, *, days):
            order.append("workspaces")
            seen["data_dir"], seen["days"] = data_dir, days
            return [tmp_path / "dead-run"]

        async def fake_run_loop(*_args, **_kwargs):
            order.append("run_loop")

        monkeypatch.setattr(cli, "sweep_run_workspaces", fake_sweep)
        monkeypatch.setattr(cli, "run_loop", fake_run_loop)
        heartbeat = MagicMock()
        heartbeat.run = AsyncMock()
        heartbeat.path = tmp_path / "hb.json"
        monkeypatch.setattr(cli, "HeartbeatWriter", lambda _role, **kwargs: heartbeat)

        with caplog.at_level("INFO", logger="claude_on_the_fly.jobs.cli"):
            await cli._run("xoxb-token")

        assert order == ["workspaces", "run_loop"]
        assert seen == {"data_dir": cli.agent.DATA_DIR, "days": 30}
        assert "retired 1 finished job workspace(s)" in "\n".join(
            r.getMessage() for r in caplog.records
        )

    async def test_a_sweep_that_retires_nothing_stays_quiet(
        self, monkeypatch, caplog, tmp_path
    ) -> None:
        """Every restart would otherwise log a line saying nothing happened."""
        from unittest.mock import AsyncMock, MagicMock

        ledger = MagicMock()
        ledger.sweep.return_value = 0
        monkeypatch.setattr(cli, "ProcessLedger", lambda _path: ledger)
        monkeypatch.setattr(
            cli,
            "build_components",
            lambda *_a: (MagicMock(), MagicMock(), MagicMock(), MagicMock(), None),
        )
        monkeypatch.setattr(cli, "sweep_run_workspaces", lambda *_a, **_k: [])

        async def fake_run_loop(*_args, **_kwargs):
            return None

        monkeypatch.setattr(cli, "run_loop", fake_run_loop)
        heartbeat = MagicMock()
        heartbeat.run = AsyncMock()
        heartbeat.path = tmp_path / "hb.json"
        monkeypatch.setattr(cli, "HeartbeatWriter", lambda _role, **kwargs: heartbeat)

        with caplog.at_level("INFO", logger="claude_on_the_fly.jobs.cli"):
            await cli._run("xoxb-token")

        assert "retired" not in "\n".join(r.getMessage() for r in caplog.records)

    async def test_the_process_listener_is_removed_even_if_the_loop_raises(
        self, monkeypatch, tmp_path
    ) -> None:
        """Left registered, a dead worker's ledger keeps being handed live pids."""
        from unittest.mock import AsyncMock, MagicMock

        ledger = MagicMock()
        ledger.sweep.return_value = 0
        monkeypatch.setattr(cli, "ProcessLedger", lambda _path: ledger)
        monkeypatch.setattr(
            cli,
            "build_components",
            lambda *_a: (MagicMock(), MagicMock(), MagicMock(), MagicMock(), None),
        )

        async def boom(*_args, **_kwargs):
            raise RuntimeError("queue vanished")

        monkeypatch.setattr(cli, "run_loop", boom)
        heartbeat = MagicMock()
        heartbeat.run = AsyncMock()
        heartbeat.path = tmp_path / "hb.json"
        monkeypatch.setattr(cli, "HeartbeatWriter", lambda _role, **kwargs: heartbeat)
        removed: list[object] = []
        monkeypatch.setattr(
            cli.agent, "remove_process_listener", lambda cb: removed.append(cb)
        )

        with pytest.raises(RuntimeError, match="queue vanished"):
            await cli._run("xoxb-token")
        assert removed == [ledger.on_process]

    async def test_the_jail_is_proven_before_anything_claims_work(
        self, monkeypatch, tmp_path
    ) -> None:
        """The worker spawns jailed agents through `agent.run`, and for a while
        only the chat daemon proved that jail holds. A boundary that could not
        hold `state/` therefore stopped Slack loudly and left this worker
        draining the same queue across it."""
        from unittest.mock import AsyncMock, MagicMock

        order: list[str] = []
        ledger = MagicMock()
        ledger.sweep.side_effect = lambda: (order.append("sweep"), 0)[1]
        monkeypatch.setattr(cli, "ProcessLedger", lambda _path: ledger)
        monkeypatch.setattr(
            cli,
            "build_components",
            lambda *_a: (
                order.append("build"),
                (MagicMock(), MagicMock(), MagicMock(), MagicMock(), None),
            )[1],
        )

        async def verify(*_args, **_kwargs):
            order.append("verify_boundary")

        monkeypatch.setattr(cli.sandbox, "verify_boundary", verify)

        async def fake_run_loop(*_args, **_kwargs):
            order.append("run_loop")

        monkeypatch.setattr(cli, "run_loop", fake_run_loop)
        heartbeat = MagicMock()
        heartbeat.run = AsyncMock()
        heartbeat.path = tmp_path / "hb.json"
        monkeypatch.setattr(cli, "HeartbeatWriter", lambda _role, **kwargs: heartbeat)

        await cli._run("xoxb-token")

        assert order == ["verify_boundary", "build", "sweep", "run_loop"]


@pytest.mark.parametrize(
    "error",
    [
        cli.sandbox.SandboxBoundaryError(
            "sandbox boundary self-test failed; refusing to start autonomous work"
        ),
        cli.sandbox.SandboxModeError(
            "sandbox.mode='jial' is not one of ['off', 'env', 'jail']. "
            "Refusing to start rather than serving turns unsandboxed."
        ),
    ],
    ids=["boundary", "mode"],
)
def test_an_unproven_jail_refuses_to_start_the_worker(
    monkeypatch, capsys, error
) -> None:
    """Fatal rather than advisory, and unattended is the argument for it: the
    worker runs bypassPermissions turns against whatever a producer queued. The
    queue is durable, so a refusal costs a restart; the opposite choice cannot be
    undone once the reads have happened. Both startup refusals exit the same way:
    a typo'd `sandbox.mode` raises SandboxModeError from `verify_boundary` ->
    `preflight`, and it must not die with a traceback and exit 1."""
    monkeypatch.setattr(cli, "live_pid", lambda frontend: None)
    monkeypatch.setattr(cli, "_setup_logging", lambda: None)
    monkeypatch.setattr(cli, "check_backend", lambda: None)
    monkeypatch.setattr(
        cli.checks, "resolve_jobs_token", lambda env: ("JOBS_SLACK_TOKEN", "xoxb-t")
    )

    async def refuse(_token):
        raise error

    monkeypatch.setattr(cli, "_run", refuse)

    assert cli._cmd_run() == 2
    assert "refusing to start" in capsys.readouterr().err.lower()


def test_normalize_argv_treats_a_bare_flag_as_run_options() -> None:
    """`claude-jobs --verbose` is somebody running the worker, not a typo'd
    subcommand."""
    assert cli._normalize_argv(["--verbose"]) == ["run", "--verbose"]


def test_setup_logging_names_the_jobs_role(monkeypatch) -> None:
    """The role picks the log filename, and a wrong one puts the worker's output in
    another daemon's file where a syncer will conflict over it."""
    seen: list[str] = []
    monkeypatch.setattr(cli, "setup_daemon_logging", lambda role: seen.append(role))
    cli._setup_logging()
    assert seen == ["jobs"]


def test_running_jobs_shapes_the_in_flight_dict() -> None:
    """The heartbeat extra mirrors the orchestrator's chat `running_jobs`
    shape, so the dashboard normalizes both sources the same way."""
    import time

    from claude_on_the_fly.jobs.agent_runner import OrchestratorAgentRunner

    runner = OrchestratorAgentRunner(data_dir=Path("/tmp/x"))
    runner.in_flight["1-a"] = {
        "session_uuid": "s-1",
        "workspace": "/tmp/x/workspaces/jobs/abc",
        "key": "k1",
        "started_at_monotonic": time.monotonic() - 5,
    }

    out = cli._running_jobs(runner)

    (row,) = out["running_jobs"]
    assert row == {
        "job_id": "1-a",
        "key": "k1",
        "workspace": "/tmp/x/workspaces/jobs/abc",
        "uptime_s": 5,
        "session_uuid": "s-1",
    }


def test_running_jobs_with_nothing_in_flight_is_empty() -> None:
    from claude_on_the_fly.jobs.agent_runner import OrchestratorAgentRunner

    runner = OrchestratorAgentRunner(data_dir=Path("/tmp/x"))
    assert cli._running_jobs(runner) == {"running_jobs": []}


def test_workspace_keep_days_defaults_to_thirty() -> None:
    """Matches the log retention window, so an operator holds one number for how
    far back they can look."""
    assert cli._workspace_keep_days() == 30


def test_workspace_keep_days_reads_the_setting(monkeypatch) -> None:
    monkeypatch.setenv("JOBS_WORKSPACE_KEEP_DAYS", "7")
    assert cli._workspace_keep_days() == 7


def test_workspace_keep_days_of_zero_disables_the_sweep(monkeypatch) -> None:
    """An operator with the disk can keep every run forever."""
    monkeypatch.setenv("JOBS_WORKSPACE_KEEP_DAYS", "0")
    assert cli._workspace_keep_days() == 0


def test_workspace_keep_days_falls_back_when_unparseable(monkeypatch) -> None:
    """A typo must not delete everything by resolving to 0, nor stop the worker."""
    monkeypatch.setenv("JOBS_WORKSPACE_KEEP_DAYS", "a month")
    assert cli._workspace_keep_days() == 30
