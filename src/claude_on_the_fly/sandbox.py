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
_DENY_MOST_PROFILE = _SEATBELT_DIR / "my.deny-most.sb"
_JAIL_PROFILE = _SEATBELT_DIR / "my.jail.sb"

# SBPL has no arrays, so operator read grants are a fixed, documented cap.
_MAX_EXTRA_PATHS = 3
# Default loopback allow: every loopback port (agent dev servers/tests work).
_DEFAULT_LOOPBACK = "localhost:*"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Backend-agnostic sandbox note appended to the system prompt (see
# agent_guidance). env mode only curates the environment, so its note just warns
# that secrets are absent; jail mode enumerates the actual blocked scenarios.
_ENV_GUIDANCE = """## Sandbox

This session runs with a curated environment: API keys and platform tokens are \
not present in your environment, and model/API access is routed through a local \
broker (via *_BASE_URL) so it works without any key from you. Do not expect \
secrets in the environment or try to read them from it."""

_JAIL_GUIDANCE = """## Sandbox

This session runs under a macOS seatbelt sandbox with a credential broker. Some \
operations are blocked by policy. A policy block is not a transient error and \
cannot be worked around with chmod, sudo, or retrying: the only fix is an \
operator configuration change. When you hit one, tell the user the specific \
change needed, then continue with whatever you can still do.

Telling a policy block from a real error:
- "Operation not permitted" (EPERM) means sandbox policy (the target is outside \
your allowed set).
- "Permission denied" (EACCES) means a genuine file-permission problem, not the \
sandbox.
- A network call that cannot connect or resolve, an error tagged "[sandbox] ... \
egress policy", or an HTTP 451 means egress policy.

What is allowed:
- Read and write the workspace: {project}.
- {reads}
- {net} Your model/API access is already routed through the broker via \
*_BASE_URL, so it works without any key from you.

Common blocked scenarios and the remedy to relay to the user:
- Reading a file outside the allowed set (e.g. `cat ~/.aws/credentials`) fails \
with "Operation not permitted". Remedy: the operator adds the path to \
COTF_SANDBOX_EXTRA_PATHS.
- Writing a file outside the workspace fails with "Operation not permitted". \
Remedy: the operator widens the sandbox write profile.
- Reaching an external host (e.g. `curl https://example.com`, a git fetch, or an \
API that is not allowlisted) fails to connect or returns 451. Remedy: the \
operator adds a broker route for that host.
- Reading the keychain (e.g. `security find-generic-password`) is denied. You do \
not need it; credentials are injected by the broker."""


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


def _fs_base_profile() -> Path:
    """Base profile the jail imports. COTF_SANDBOX_FS=deny-most swaps my.sb for
    the least-privilege read profile; anything else keeps my.sb (default)."""
    if os.environ.get("COTF_SANDBOX_FS", "").lower() == "deny-most":
        return _DENY_MOST_PROFILE
    return _BASE_PROFILE


def _broker_port() -> str | None:
    """The broker's loopback port, read from whichever *_BASE_URL it published.

    The broker serves every route on one port, so any published base-url carries
    it. Returns None if no broker base-url is present.
    """
    for key, value in os.environ.items():
        if key.endswith("_BASE_URL") and value.startswith("http://127.0.0.1:"):
            port = value[len("http://127.0.0.1:") :].split("/", 1)[0]
            if port.isdigit():
                return port
    return None


def _loopback_spec() -> str:
    """remote-ip value for the jail's loopback allow. Narrows to the broker port
    when COTF_SANDBOX_BROKER_ONLY_LOOPBACK is set and the port is known; leaves
    loopback open otherwise so the broker stays reachable no matter what."""
    if os.environ.get("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "").lower() not in _TRUTHY:
        return _DEFAULT_LOOPBACK
    port = _broker_port()
    if port is None:
        logger.warning(
            "COTF_SANDBOX_BROKER_ONLY_LOOPBACK set but no broker base-url in env; "
            "leaving loopback open"
        )
        return _DEFAULT_LOOPBACK
    return f"localhost:{port}"


def _extra_read_paths() -> list[str]:
    """Operator read grants for deny-most, from COTF_SANDBOX_EXTRA_PATHS
    (colon-separated), realpath'd and capped at _MAX_EXTRA_PATHS."""
    paths = [p for p in os.environ.get("COTF_SANDBOX_EXTRA_PATHS", "").split(":") if p]
    if len(paths) > _MAX_EXTRA_PATHS:
        logger.warning(
            "COTF_SANDBOX_EXTRA_PATHS has %d entries; granting only the first %d "
            "(seatbelt has no arrays)",
            len(paths),
            _MAX_EXTRA_PATHS,
        )
    return [os.path.realpath(p) for p in paths[:_MAX_EXTRA_PATHS]]


def agent_guidance(workspace: Path | None = None) -> str:
    """Sandbox-awareness note for the agent's system prompt, agnostic across
    backends (all of them build their prompt through build_system_prompt).

    Empty when sandboxing is off. In jail mode it names what is blocked, how to
    tell a policy denial from a real error, and the operator remedy to relay, so
    the agent surfaces the fix instead of retrying or attempting chmod/sudo. The
    allowed-reads and egress lines reflect the actual COTF_SANDBOX_FS and
    COTF_SANDBOX_BROKER_ONLY_LOOPBACK settings.
    """
    current = mode()
    if current == "off":
        return ""
    if current == "env":
        return _ENV_GUIDANCE
    project = os.path.realpath(workspace) if workspace is not None else "the workspace"
    home = Path.home()
    if os.environ.get("COTF_SANDBOX_FS", "").lower() == "deny-most":
        grants = [project, f"{home}/.claude", f"{home}/.cache/uv", *_extra_read_paths()]
        reads = (
            "You can read only these paths: "
            + ", ".join(grants)
            + ". Reads elsewhere under your home directory are blocked."
        )
    else:
        reads = (
            "You can read most of the filesystem, but reads of secrets are blocked: "
            "the keychain, SSH private keys, cloud credentials (~/.aws/credentials), "
            "and token files (~/.npmrc, ~/.netrc, ~/.env)."
        )
    if os.environ.get("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "").lower() in _TRUTHY:
        net = (
            "Outbound network reaches ONLY the local broker; other local ports and "
            "external hosts are blocked."
        )
    else:
        net = (
            "Outbound network reaches only hosts the operator allowlisted in the "
            "local broker (plus local loopback); other external hosts are blocked."
        )
    return _JAIL_GUIDANCE.format(project=project, reads=reads, net=net)


def wrap(argv: list[str], workspace: Path) -> list[str]:
    """Wrap argv in the vendored seatbelt jail when COTF_SANDBOX=jail, else
    return it unchanged.

    Invokes sandbox-exec against the vendored jail profile directly; the agent's
    environment is curated separately by agent_env(). If sandbox-exec is missing
    (non-macOS), logs and returns the bare argv so jail degrades to env-only
    rather than failing the run.

    COTF_SANDBOX_FS=deny-most swaps the read-permissive base for a least-privilege
    one and forwards COTF_SANDBOX_EXTRA_PATHS grants. COTF_SANDBOX_BROKER_ONLY_LOOPBACK
    narrows egress from all loopback to just the broker port.
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
    project = os.path.realpath(workspace)
    base = _fs_base_profile()
    params = [
        "-D",
        f"_HOME={Path.home()}",
        "-D",
        f"_PROJECT_DIR={project}",
        "-D",
        f"_TMPDIR={tmpdir}",
        "-D",
        f"_BASE={base}",
        "-D",
        f"_LOOPBACK={_loopback_spec()}",
    ]
    # my.sb does not reference _EXTRA_*; only deny-most does, so only pass them
    # there. Pad unused slots with the project dir (already granted, a no-op).
    if base == _DENY_MOST_PROFILE:
        extra = _extra_read_paths()
        extra += [project] * (_MAX_EXTRA_PATHS - len(extra))
        for index, path in enumerate(extra, start=1):
            params += ["-D", f"_EXTRA_{index}={path}"]
    return ["sandbox-exec", "-f", str(_JAIL_PROFILE), *params, *argv]
