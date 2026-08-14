"""Resolve and run the command that upgrades this installation.

There is no single answer: the same code ships three ways, and each one updates
differently. So the command is *resolved* — from `upgrade.command` when the
operator set one, otherwise from how this process was installed — and a
deployment we cannot recognise gets an error naming what to configure rather
than a guess that silently upgrades nothing.

The caller stops the daemons first (see `tui.supervisor.stop_all`) and starts
them again after. Nothing here signals a daemon: this module knows how to fetch
new code and nothing else.
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

# uv installs a tool into `<data dir>/uv/tools/<name>`, and that venv is
# `sys.prefix` for anything it runs. The two directory names above it are the
# whole signature.
_UV_TOOLS_PARENTS = ("tools", "uv")


class UnknownInstall(Exception):
    """The install shape is not one we know how to upgrade."""


@dataclass(frozen=True)
class Plan:
    """The upgrade command and where it came from, so output can say which."""

    command: str
    source: str
    cwd: Path | None = None


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
        return Plan(command=configured, source="upgrade.command")

    repo = _repo_root()
    if repo is not None:
        # --ff-only so a checkout with local commits stops here instead of
        # opening a merge nobody is watching.
        return Plan(
            command="git pull --ff-only && uv sync",
            source=f"git checkout at {repo}",
            cwd=repo,
        )

    tool = _uv_tool_name()
    if tool is not None:
        return Plan(command=f"uv tool upgrade {tool}", source="uv tool install")

    raise UnknownInstall(
        "cannot tell how this copy was installed, so there is nothing safe to "
        f"run. Set upgrade.command in config.yaml (or {COMMAND_VAR}) to the "
        "command that updates it. A `uvx --from git+...` run needs no upgrade: "
        "it fetches the current code every time it starts."
    )


def run(plan: Plan, *, runner=subprocess.run) -> int:
    """Run the upgrade command, streaming its output. Returns its exit code.

    Through a shell, because the resolved default is two commands joined by
    `&&` and an operator's own `upgrade.command` will be shell too. The string
    comes from this module or from the operator's own config file — never from a
    chat message, and never from an agent.
    """
    logger.info("upgrade: running %s (%s)", plan.command, plan.source)
    completed = runner(plan.command, shell=True, cwd=plan.cwd, check=False)
    return completed.returncode


def run_captured(plan: Plan, *, runner=subprocess.run) -> tuple[int, str]:
    """Run the upgrade command with its output captured. Returns (code, output).

    For a caller that owns the terminal: the TUI cannot let git and uv write over
    its own screen, so it takes the text and puts it somewhere readable instead.
    """
    logger.info("upgrade: running %s (%s)", plan.command, plan.source)
    completed = runner(
        plan.command,
        shell=True,
        cwd=plan.cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode, output


def relaunch_argv() -> list[str]:
    """The argv that starts this program again, for an exec after an upgrade.

    A running process keeps the code it loaded, so the TUI cannot show the new
    version without handing itself over to it.
    """
    return [sys.executable, "-m", "claude_on_the_fly.tui.app", *sys.argv[1:]]


def describe(plan: Plan) -> str:
    """One line naming the command and why it was chosen."""
    return f"{plan.command}   [{plan.source}]"
