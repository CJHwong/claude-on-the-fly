"""Resolve and run the commands that upgrade this installation.

There is no single answer: the same code ships three ways, and each one updates
differently. So the command is *resolved* — from `upgrade.command` when the
operator set one, otherwise from how this process was installed — and a
deployment we cannot recognise gets an error naming what to configure rather
than a guess that silently upgrades nothing.

An upgrade is up to two steps, because only one of them needs the daemons down.
`prepare` fetches what the new code needs, and runs while they are still
serving. `command` rewrites the tree they run from, so the caller stops them
first (see `tui.supervisor.stop_all`) and starts them again after. Moving the
network round trip out of the window is the difference between a window
measured in seconds and one measured in tens of seconds.

Nothing here signals a daemon: this module knows how to fetch new code and
nothing else.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_on_the_fly import settings

logger = logging.getLogger(__name__)

COMMAND_VAR = "COTF_UPGRADE_COMMAND"
PREPARE_VAR = "COTF_UPGRADE_PREPARE_COMMAND"

# uv installs a tool into `<data dir>/uv/tools/<name>`, and that venv is
# `sys.prefix` for anything it runs. The two directory names above it are the
# whole signature.
_UV_TOOLS_PARENTS = ("tools", "uv")


class UnknownInstall(Exception):
    """The install shape is not one we know how to upgrade."""


@dataclass(frozen=True)
class Plan:
    """The commands that upgrade this install, and where the choice came from.

    Two steps, split by whether the daemons have to be down for them. `prepare`
    changes nothing the running daemons read, so it runs live. `command`
    replaces the code underneath them, so it runs in the window. A plan with no
    `prepare` is the older single-step shape: everything inside the window.
    """

    command: str
    source: str
    cwd: Path | None = None
    prepare: str | None = None


def _repo_root() -> Path | None:
    """The git checkout this package is imported from, or None.

    Both markers are required. `.git` alone can be an outer repository that
    merely contains a virtualenv, and a `pyproject.toml` alone is any installed
    source tree — only together do they mean "the checkout from the README".
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").is_file():
            return parent
    return None


def _uv_tool_name() -> str | None:
    """The uv tool this process runs as, or None if it is not one."""
    prefix = Path(sys.prefix).resolve()
    parents = prefix.parents
    if len(parents) < 2:
        return None
    if (parents[0].name, parents[1].name) == _UV_TOOLS_PARENTS:
        return prefix.name
    return None


def resolve() -> Plan:
    """How to upgrade this installation.

    Raises UnknownInstall when the shape is not recognised — a `uvx` run, whose
    code is a throwaway cache that the next `uvx` invocation refreshes anyway,
    or a plain virtualenv somebody else's tooling owns.
    """
    configured = settings.get(COMMAND_VAR, "").strip()
    if configured:
        # An operator's command is opaque, so only they can say which half of
        # it is safe to run against live daemons. Unset means the single step
        # this always was, rather than a guess that splits their command.
        prepare = settings.get(PREPARE_VAR, "").strip() or None
        return Plan(command=configured, source="upgrade.command", prepare=prepare)

    repo = _repo_root()
    if repo is not None:
        # `git fetch` writes only under .git, which no daemon reads, so it is
        # the network round trip moved out of the window. The merge and the
        # sync stay in it: both rewrite files a running daemon loads. Together
        # the two steps are what `git pull --ff-only && uv sync` did, and
        # --ff-only still stops at local commits instead of opening a merge
        # nobody is watching.
        return Plan(
            command="git merge --ff-only && uv sync",
            source=f"git checkout at {repo}",
            cwd=repo,
            prepare="git fetch",
        )

    tool = _uv_tool_name()
    if tool is not None:
        # One step: uv builds the replacement tool environment itself, so
        # there is no separate fetch to take out of the window.
        return Plan(command=f"uv tool upgrade {tool}", source="uv tool install")

    raise UnknownInstall(
        "cannot tell how this copy was installed, so there is nothing safe to "
        f"run. Set upgrade.command in config.yaml (or {COMMAND_VAR}) to the "
        "command that updates it. A `uvx --from git+...` run needs no upgrade: "
        "it fetches the current code every time it starts."
    )


def _execute(
    command: str, cwd: Path | None, runner, *, capture: bool
) -> tuple[int, str]:
    """Run one command through a shell. Returns (exit code, output).

    Through a shell, because a resolved default joins two commands with `&&` and
    an operator's own command will be shell too. Every command here comes from
    this module or from the operator's own config file, never from a chat
    message, and never from an agent.

    Output comes back only when the caller asked to capture it. A caller that
    owns a terminal streams instead, so a slow fetch shows its progress.
    """
    logger.info("upgrade: running %s", command)
    if not capture:
        return runner(command, shell=True, cwd=cwd, check=False).returncode, ""
    completed = runner(
        command, shell=True, cwd=cwd, check=False, capture_output=True, text=True
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def run(plan: Plan, *, runner=subprocess.run) -> int:
    """Run the activate step, streaming its output. Returns its exit code."""
    return _execute(plan.command, plan.cwd, runner, capture=False)[0]


def run_captured(plan: Plan, *, runner=subprocess.run) -> tuple[int, str]:
    """Run the activate step with its output captured. Returns (code, output).

    For a caller that owns the terminal: the TUI cannot let git and uv write over
    its own screen, so it takes the text and puts it somewhere readable instead.
    """
    return _execute(plan.command, plan.cwd, runner, capture=True)


def run_prepare(plan: Plan, *, runner=subprocess.run) -> int:
    """Run the prepare step, streaming. 0 when the plan has no prepare step.

    This one runs with every daemon still serving, so its exit code is the
    caller's chance to abandon an upgrade that cannot succeed without having
    stopped anything. A failed fetch used to surface only once everything was
    already down.
    """
    if plan.prepare is None:
        return 0
    return _execute(plan.prepare, plan.cwd, runner, capture=False)[0]


def run_prepare_captured(plan: Plan, *, runner=subprocess.run) -> tuple[int, str]:
    """Run the prepare step with its output captured. (0, "") when there is none."""
    if plan.prepare is None:
        return 0, ""
    return _execute(plan.prepare, plan.cwd, runner, capture=True)


def relaunch_argv() -> list[str]:
    """The argv that starts this program again, for an exec after an upgrade.

    A running process keeps the code it loaded, so the TUI cannot show the new
    version without handing itself over to it.
    """
    return [sys.executable, "-m", "claude_on_the_fly.tui.app", *sys.argv[1:]]


def describe(plan: Plan) -> str:
    """One line naming the commands and why they were chosen.

    The prepare step is named because it is the one that runs before anything
    is interrupted, and an operator reading a confirmation prompt should be
    able to see which part of the upgrade costs them uptime.
    """
    if plan.prepare:
        return (
            f"{plan.prepare} (daemons stay up), then {plan.command}   [{plan.source}]"
        )
    return f"{plan.command}   [{plan.source}]"
