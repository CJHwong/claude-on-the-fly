"""`claude-tui` subcommand dispatch and exit codes.

The exit code is the contract here: this is what a shell script, a launchd job, or
a `&&` chain reads. Every failure path has to be non-zero and every message has to
go to stderr, or a broken daemon start looks like a successful one.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_on_the_fly.checks import CheckResult
from claude_on_the_fly.tui import app, supervisor


class TestStatus:
    def test_the_table_form_prints_and_exits_zero(self, capsys):
        with (
            patch.object(app.state, "snapshot", return_value={"frontends": []}),
            patch.object(app.render, "render_snapshot_rich") as rich,
        ):
            assert app.cmd_status(json_output=False) == 0
        rich.assert_called_once()

    def test_the_json_form_prints_json(self, capsys):
        with (
            patch.object(app.state, "snapshot", return_value={"frontends": []}),
            patch.object(
                app.render, "render_snapshot_json", return_value='{"frontends": []}'
            ),
        ):
            assert app.cmd_status(json_output=True) == 0
        assert '"frontends"' in capsys.readouterr().out


class TestStart:
    def test_a_successful_start_reports_the_pid(self, capsys):
        with patch.object(app.supervisor, "spawn", return_value=4242):
            assert app.cmd_start("slack", None) == 0
        assert "started slack (pid 4242)" in capsys.readouterr().out

    def test_an_already_running_daemon_exits_one(self, capsys):
        """Distinct from a preflight failure: nothing is wrong, the work is done."""
        with patch.object(
            app.supervisor,
            "spawn",
            side_effect=supervisor.AlreadyRunning("slack", 1),
        ):
            assert app.cmd_start("slack", None) == 1
        assert "already running" in capsys.readouterr().err

    def test_a_preflight_failure_exits_two_with_every_hint(self, capsys):
        """The hints are the whole point of the preflight, so a failure that prints
        only statuses leaves the operator nothing to do."""
        failure = supervisor.PreflightFailed(
            "slack",
            [
                CheckResult(name="ok-one", status="ok", detail="fine"),
                CheckResult(
                    name="SLACK_TOKEN",
                    status="missing",
                    detail="not set",
                    fix_hint="add SLACK_TOKEN to .env",
                ),
                CheckResult(name="jq", status="missing", detail="not on PATH"),
            ],
        )
        with patch.object(app.supervisor, "spawn", side_effect=failure):
            assert app.cmd_start("slack", None) == 2
        err = capsys.readouterr().err
        assert "SLACK_TOKEN: missing" in err
        assert "hint: add SLACK_TOKEN to .env" in err
        # A check that passed is not worth printing.
        assert "ok-one" not in err
        # A failure with no hint still gets its line.
        assert "jq: missing" in err

    def test_a_bad_argument_exits_two(self, capsys):
        with patch.object(
            app.supervisor, "spawn", side_effect=ValueError("no such env file")
        ):
            assert app.cmd_start("slack", Path("/nope")) == 2
        assert "no such env file" in capsys.readouterr().err


class TestStop:
    def test_a_successful_stop_reports_the_pid(self, capsys):
        with patch.object(app.supervisor, "stop", return_value=4242):
            assert app.cmd_stop("slack") == 0
        assert "stopped slack (pid 4242)" in capsys.readouterr().out

    def test_stopping_something_that_is_not_running_exits_one(self, capsys):
        with patch.object(
            app.supervisor,
            "stop",
            side_effect=supervisor.NotRunning("slack is not running"),
        ):
            assert app.cmd_stop("slack") == 1
        assert "slack is not running" in capsys.readouterr().err


class TestStopAll:
    def test_nothing_running_is_still_success(self, capsys):
        """`stop-all` in a shutdown script must not fail just because the machine was
        already quiet."""
        with patch.object(app.supervisor, "stop_all", return_value=[]):
            assert app.cmd_stop_all() == 0
        assert "nothing running" in capsys.readouterr().out

    def test_each_stopped_daemon_is_named_and_resume_is_pointed_at(self, capsys):
        with patch.object(
            app.supervisor, "stop_all", return_value=[("slack", 1), ("cron", 2)]
        ):
            assert app.cmd_stop_all() == 0
        out = capsys.readouterr().out
        assert "stopped slack (pid 1)" in out
        assert "stopped cron (pid 2)" in out
        assert "2 daemon(s) stopped" in out
        assert "claude-tui resume" in out


class TestResume:
    def test_no_record_to_resume_is_still_success(self, capsys):
        with patch.object(app.supervisor, "resume", return_value=[]):
            assert app.cmd_resume(None) == 0
        assert "nothing to resume" in capsys.readouterr().out

    def test_every_daemon_starting_is_success(self, capsys):
        with patch.object(
            app.supervisor,
            "resume",
            return_value=[("slack", 1, None), ("cron", 2, None)],
        ):
            assert app.cmd_resume(None) == 0
        assert "started cron (pid 2)" in capsys.readouterr().out

    def test_a_partial_resume_exits_two_and_names_the_failure(self, capsys):
        """Exit 2 because some daemons did start: reporting success would hide the one
        that did not, and reporting a clean failure would hide the ones that did."""
        with patch.object(
            app.supervisor,
            "resume",
            return_value=[("slack", 1, None), ("cron", None, RuntimeError("no token"))],
        ):
            assert app.cmd_resume(None) == 2
        captured = capsys.readouterr()
        assert "started slack (pid 1)" in captured.out
        assert "failed cron: no token" in captured.err


class TestRestart:
    def test_a_successful_restart_reports_the_new_pid(self, capsys):
        with patch.object(app.supervisor, "restart", return_value=4242):
            assert app.cmd_restart("slack", None) == 0
        assert "restarted slack (pid 4242)" in capsys.readouterr().out

    def test_a_preflight_failure_exits_two(self, capsys):
        failure = supervisor.PreflightFailed(
            "slack",
            [CheckResult(name="SLACK_TOKEN", status="missing", detail="not set")],
        )
        with patch.object(app.supervisor, "restart", side_effect=failure):
            assert app.cmd_restart("slack", None) == 2
        assert "SLACK_TOKEN: missing" in capsys.readouterr().err

    def test_a_bad_argument_exits_two(self, capsys):
        with patch.object(
            app.supervisor, "restart", side_effect=ValueError("bad env file")
        ):
            assert app.cmd_restart("slack", None) == 2
        assert "bad env file" in capsys.readouterr().err


class TestInteractive:
    def test_the_dashboard_is_launched(self):
        run_app = MagicMock()
        with patch.dict(
            "sys.modules",
            {"claude_on_the_fly.tui.tui_app": MagicMock(run_app=run_app)},
        ):
            assert app.cmd_interactive() == 0
        run_app.assert_called_once()

    def test_a_missing_textual_falls_back_to_status(self, capsys, monkeypatch):
        """Textual is the one dependency a headless box may lack, and a traceback
        there would make `claude-tui` useless rather than merely non-interactive."""
        import builtins

        real_import = builtins.__import__

        def no_textual(name, *args, **kwargs):
            if name == "claude_on_the_fly.tui.tui_app":
                raise ImportError("No module named 'textual'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_textual)
        with (
            patch.object(app.state, "snapshot", return_value={"frontends": []}),
            patch.object(app.render, "render_snapshot_rich"),
        ):
            assert app.cmd_interactive() == 0
        assert "Interactive mode unavailable" in capsys.readouterr().err


class TestArgvDispatch:
    @pytest.mark.parametrize(
        ("argv", "target"),
        [
            (["status"], "cmd_status"),
            (["start", "slack"], "cmd_start"),
            (["stop", "slack"], "cmd_stop"),
            (["restart", "slack"], "cmd_restart"),
            (["stop-all"], "cmd_stop_all"),
            (["resume"], "cmd_resume"),
            ([], "cmd_interactive"),
        ],
    )
    def test_each_subcommand_reaches_its_handler(self, argv, target, monkeypatch):
        called: list[str] = []
        monkeypatch.setattr(app, target, lambda *a, **kw: (called.append(target), 0)[1])
        assert app.main(argv) == 0
        assert called == [target]

    def test_the_status_json_flag_is_threaded_through(self, monkeypatch):
        seen: list[bool] = []
        monkeypatch.setattr(
            app, "cmd_status", lambda *, json_output: (seen.append(json_output), 0)[1]
        )
        app.main(["status", "--json"])
        assert seen == [True]

    def test_the_env_file_default_is_the_supervisors(self, monkeypatch):
        seen: list[Path | None] = []
        monkeypatch.setattr(
            app, "cmd_start", lambda _frontend, env_file: (seen.append(env_file), 0)[1]
        )
        app.main(["start", "slack"])
        assert seen == [supervisor.DEFAULT_ENV_FILE]

    def test_an_explicit_env_file_wins(self, monkeypatch, tmp_path):
        seen: list[Path | None] = []
        monkeypatch.setattr(
            app, "cmd_start", lambda _frontend, env_file: (seen.append(env_file), 0)[1]
        )
        app.main(["start", "slack", "--env-file", str(tmp_path / "custom.env")])
        assert seen == [tmp_path / "custom.env"]

    def test_an_unknown_frontend_is_refused_by_the_parser(self):
        """argparse choices, so the error names the valid ones rather than failing
        later inside the supervisor."""
        with pytest.raises(SystemExit):
            app.main(["start", "not-a-frontend"])

    def test_the_exit_code_from_a_handler_is_returned(self, monkeypatch):
        monkeypatch.setattr(app, "cmd_stop", lambda _f: 1)
        assert app.main(["stop", "slack"]) == 1
