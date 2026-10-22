"""Tests for `upgrade` — resolving how this copy updates, and running it.

The resolution is the risky half: guessing wrong means running `git pull` in a
directory nobody meant, or reporting success after upgrading nothing. So each
install shape is pinned, and an unrecognised one has to raise.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_on_the_fly import upgrade


@pytest.fixture(autouse=True)
def no_configured_command(monkeypatch):
    """Default every test to "the operator set nothing"."""
    monkeypatch.setattr(upgrade.settings, "get", lambda _name, default="": default)


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    (repo / "src" / "claude_on_the_fly").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("[project]\n")
    return repo


class TestResolve:
    def test_a_configured_command_wins_over_any_detection(self, monkeypatch, tmp_path):
        """An operator whose deployment updates some other way (an image, a
        pinned tag) must not have a `git pull` chosen for them."""
        monkeypatch.setattr(
            upgrade.settings,
            "get",
            lambda name, default="": (
                "  make deploy  " if name == upgrade.COMMAND_VAR else default
            ),
        )
        monkeypatch.setattr(upgrade, "_repo_root", lambda: _fake_repo(tmp_path))

        plan = upgrade.resolve()

        assert plan.command == "make deploy"
        assert plan.source == "upgrade.command"
        assert plan.cwd is None
        # An opaque command is not split for them: only they can say which half
        # is safe to run against live daemons.
        assert plan.prepare is None

    def test_a_configured_prepare_command_is_taken(self, monkeypatch, tmp_path):
        def fake_get(name, default=""):
            return {
                upgrade.COMMAND_VAR: "make deploy",
                upgrade.PREPARE_VAR: "  make fetch  ",
            }.get(name, default)

        monkeypatch.setattr(upgrade.settings, "get", fake_get)
        monkeypatch.setattr(upgrade, "_repo_root", lambda: _fake_repo(tmp_path))

        plan = upgrade.resolve()

        assert plan.prepare == "make fetch"

    def test_a_git_checkout_fetches_outside_the_window(self, monkeypatch):
        """The fetch is the network round trip, and it changes only .git, which
        no daemon reads. Leaving it inside the window is what made an upgrade
        cost seconds it did not have to."""
        monkeypatch.setattr(upgrade, "_repo_root", lambda: Path("/src/cotf"))

        plan = upgrade.resolve()

        assert plan.prepare == "git fetch"
        # The two steps together are what `git pull --ff-only && uv sync` did.
        assert plan.command == "git merge --ff-only && uv sync"
        assert plan.cwd == Path("/src/cotf")
        assert "/src/cotf" in plan.source

    def test_a_uv_tool_install_upgrades_that_tool(self, monkeypatch):
        monkeypatch.setattr(upgrade, "_repo_root", lambda: None)
        monkeypatch.setattr(upgrade, "_uv_tool_name", lambda: "claude-on-the-fly")

        plan = upgrade.resolve()

        assert plan.command == "uv tool upgrade claude-on-the-fly"
        assert plan.cwd is None
        # One step: uv builds the replacement environment itself, so there is
        # no separate fetch to move out of the window.
        assert plan.prepare is None

    def test_an_unrecognised_install_refuses_and_names_the_setting(self, monkeypatch):
        """The failure mode this prevents: a guessed command that exits 0 while
        upgrading nothing, which reads as a successful upgrade."""
        monkeypatch.setattr(upgrade, "_repo_root", lambda: None)
        monkeypatch.setattr(upgrade, "_uv_tool_name", lambda: None)

        with pytest.raises(upgrade.UnknownInstall) as exc:
            upgrade.resolve()

        assert "upgrade.command" in str(exc.value)
        assert upgrade.COMMAND_VAR in str(exc.value)


class TestRepoRoot:
    def test_the_checkout_holding_this_module_is_found(self, monkeypatch, tmp_path):
        repo = _fake_repo(tmp_path)
        monkeypatch.setattr(
            upgrade, "__file__", str(repo / "src" / "claude_on_the_fly" / "upgrade.py")
        )

        assert upgrade._repo_root() == repo

    def test_a_git_dir_without_a_pyproject_is_not_a_checkout(
        self, monkeypatch, tmp_path
    ):
        """A virtualenv inside somebody else's repository. Pulling there would
        update *their* project, not this one."""
        outer = tmp_path / "outer"
        (outer / ".git").mkdir(parents=True)
        module = outer / ".venv" / "lib" / "claude_on_the_fly"
        module.mkdir(parents=True)
        monkeypatch.setattr(upgrade, "__file__", str(module / "upgrade.py"))

        assert upgrade._repo_root() is None


class TestUvToolName:
    def test_a_tools_prefix_names_the_tool(self, monkeypatch, tmp_path):
        prefix = tmp_path / "uv" / "tools" / "claude-on-the-fly"
        prefix.mkdir(parents=True)
        monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))

        assert upgrade._uv_tool_name() == "claude-on-the-fly"

    def test_any_other_prefix_is_not_a_uv_tool(self, monkeypatch, tmp_path):
        prefix = tmp_path / "project" / ".venv"
        prefix.mkdir(parents=True)
        monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))

        assert upgrade._uv_tool_name() is None

    def test_the_filesystem_root_is_not_a_uv_tool(self, monkeypatch):
        """`/` has no parents to inspect, and indexing them would raise."""
        monkeypatch.setattr(upgrade.sys, "prefix", "/")

        assert upgrade._uv_tool_name() is None


class TestRun:
    def test_the_command_runs_through_a_shell_in_the_plans_directory(self):
        plan = upgrade.Plan(
            command="git pull && uv sync", source="test", cwd=Path("/x")
        )
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 0))

        assert upgrade.run(plan, runner=runner) == 0

        args, kwargs = runner.call_args
        assert args[0] == "git pull && uv sync"
        assert kwargs["shell"] is True
        assert kwargs["cwd"] == Path("/x")
        # check=False: a failed upgrade is reported and recovered from, never
        # raised through a caller that still has daemons to restart.
        assert kwargs["check"] is False

    def test_a_failing_command_returns_its_code(self):
        plan = upgrade.Plan(command="false", source="test")
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 3))

        assert upgrade.run(plan, runner=runner) == 3

    def test_captured_output_joins_stdout_and_stderr(self):
        plan = upgrade.Plan(command="git pull", source="test")
        runner = MagicMock(
            return_value=subprocess.CompletedProcess([], 1, "out\n", "err\n")
        )

        code, output = upgrade.run_captured(plan, runner=runner)

        assert (code, output) == (1, "out\nerr\n")
        assert runner.call_args.kwargs["capture_output"] is True

    def test_captured_output_survives_a_runner_that_reports_none(self):
        """subprocess reports None for a stream it did not capture."""
        plan = upgrade.Plan(command="git pull", source="test")
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, None, None))

        assert upgrade.run_captured(plan, runner=runner) == (0, "")

    def test_prepare_runs_the_first_step_streaming(self):
        plan = upgrade.Plan(
            command="git merge --ff-only && uv sync",
            source="test",
            cwd=Path("/x"),
            prepare="git fetch",
        )
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 0))

        assert upgrade.run_prepare(plan, runner=runner) == 0

        args, kwargs = runner.call_args
        assert args[0] == "git fetch"
        assert kwargs["cwd"] == Path("/x")
        assert kwargs["check"] is False
        assert "capture_output" not in kwargs

    def test_a_plan_with_no_prepare_runs_nothing(self):
        """The uv-tool shape and an operator's own command have one step. The
        runner must not be reached at all, rather than reached with ""."""
        plan = upgrade.Plan(command="uv tool upgrade x", source="test")
        runner = MagicMock()

        assert upgrade.run_prepare(plan, runner=runner) == 0

        runner.assert_not_called()

    def test_prepare_returns_its_exit_code(self):
        plan = upgrade.Plan(command="x", source="test", prepare="false")
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 4))

        assert upgrade.run_prepare(plan, runner=runner) == 4

    def test_captured_prepare_returns_code_and_output(self):
        plan = upgrade.Plan(command="x", source="test", prepare="git fetch")
        runner = MagicMock(
            return_value=subprocess.CompletedProcess([], 1, "out\n", "err\n")
        )

        assert upgrade.run_prepare_captured(plan, runner=runner) == (1, "out\nerr\n")

    def test_captured_prepare_with_no_step_returns_empty(self):
        plan = upgrade.Plan(command="x", source="test")
        runner = MagicMock()

        assert upgrade.run_prepare_captured(plan, runner=runner) == (0, "")
        runner.assert_not_called()


class TestRelaunch:
    def test_the_argv_reruns_the_tui_module_with_the_same_arguments(self, monkeypatch):
        monkeypatch.setattr(upgrade.sys, "argv", ["claude-tui", "--flag"])

        argv = upgrade.relaunch_argv()

        assert argv[0] == upgrade.sys.executable
        assert argv[1:] == ["-m", "claude_on_the_fly.tui.app", "--flag"]


def test_describe_names_the_command_and_where_it_came_from():
    plan = upgrade.Plan(command="uv tool upgrade x", source="uv tool install")

    described = upgrade.describe(plan)

    assert "uv tool upgrade x" in described
    assert "uv tool install" in described


def test_describe_says_which_step_costs_uptime():
    """An operator reading the confirmation prompt has to be able to tell which
    half of the upgrade interrupts whoever is waiting on an answer."""
    plan = upgrade.Plan(
        command="git merge --ff-only && uv sync",
        source="git checkout at /src/cotf",
        prepare="git fetch",
    )

    described = upgrade.describe(plan)

    assert "git fetch (daemons stay up)" in described
    assert "git merge --ff-only && uv sync" in described
