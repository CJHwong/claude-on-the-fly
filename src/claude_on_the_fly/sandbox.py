"""Spawn-time sandboxing for agent subprocesses.

Two independent protections, both gated by COTF_SANDBOX (default off):

  off  - inherit the full daemon environment, no wrapper. Current behavior,
         zero change for anyone who hasn't opted in.
  env  - curate the environment: forward only an allowlist to the agent, so a
         leaked-into-daemon API key or platform token never reaches it.
         Cross-platform.
  jail - curated env plus the vendored seatbelt jail (macOS): egress locked to
         loopback, keychain reads denied. Profiles are vendored in seatbelt/, so
         no external install is needed.

The agent reaches approved external services through the loopback broker (see
broker.py); base-urls published by the broker survive curation because they end
in _BASE_URL. The real keys never enter this process's child env.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# The only environment names forwarded to a sandboxed agent. Mirrors
# agent-seatbelt's clean-env allowlist. Everything else (every *_API_KEY,
# *_TOKEN, SLACK_*, JIRA_*, ...) is dropped by omission, so a new secret added
# later is excluded by default rather than leaking.
_PASSTHROUGH = frozenset(
    {
        "HOME",
        "PATH",
        "SHELL",
        "TERM",
        "LANG",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "SSH_AUTH_SOCK",
        "EDITOR",
        "VISUAL",
    }
)
_PASSTHROUGH_PREFIXES = ("XDG_", "LC_")
# base-urls route the agent's SDK at the broker; keys are never passed.
_PASSTHROUGH_SUFFIXES = ("_BASE_URL",)
_PROXY_VARS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)

_MODES = ("off", "env", "jail")

# Seatbelt profiles vendored from agent-seatbelt (see docs/agent/broker.md).
# The jail profile imports the base via the _BASE param.
_SEATBELT_DIR = Path(__file__).parent / "seatbelt"
_BASE_PROFILE = _SEATBELT_DIR / "my.sb"
_JAIL_PROFILE = _SEATBELT_DIR / "my.jail.sb"


def mode() -> str:
    """Resolved COTF_SANDBOX mode: 'off', 'env', or 'jail' (default 'off')."""
    value = os.environ.get("COTF_SANDBOX", "off").lower()
    return value if value in _MODES else "off"


def enabled() -> bool:
    return mode() != "off"


def _is_passthrough(key: str) -> bool:
    return (
        key in _PASSTHROUGH
        or key in _PROXY_VARS
        or key.startswith(_PASSTHROUGH_PREFIXES)
        or key.endswith(_PASSTHROUGH_SUFFIXES)
    )


def agent_env() -> dict[str, str] | None:
    """Environment for a spawned agent, or None to inherit the parent's unchanged.

    None when sandboxing is off, which makes the spawn sites behave exactly as
    before (create_subprocess_exec(env=None) inherits os.environ). When on, only
    the passthrough allowlist is forwarded, so secrets in the daemon env do not
    reach the agent.
    """
    if not enabled():
        return None
    return {key: value for key, value in os.environ.items() if _is_passthrough(key)}


def wrap(argv: list[str], workspace: Path) -> list[str]:
    """Wrap argv in the vendored seatbelt jail when COTF_SANDBOX=jail, else
    return it unchanged.

    Invokes sandbox-exec against the vendored jail profile directly; the agent's
    environment is curated separately by agent_env(). If sandbox-exec is missing
    (non-macOS), logs and returns the bare argv so jail degrades to env-only
    rather than failing the run.
    """
    if mode() != "jail":
        return argv
    if not shutil.which("sandbox-exec"):
        logger.warning(
            "COTF_SANDBOX=jail but sandbox-exec not found (macOS only); "
            "running with curated env but no seatbelt"
        )
        return argv
    tmpdir = os.path.realpath(os.environ.get("TMPDIR", "/tmp"))
    return [
        "sandbox-exec",
        "-f",
        str(_JAIL_PROFILE),
        "-D",
        f"_HOME={Path.home()}",
        "-D",
        f"_PROJECT_DIR={os.path.realpath(workspace)}",
        "-D",
        f"_TMPDIR={tmpdir}",
        "-D",
        f"_BASE={_BASE_PROFILE}",
        *argv,
    ]
