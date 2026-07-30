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

import asyncio
import logging
import os
import shutil
from contextvars import ContextVar, Token
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
# Locates the command broker for the generated shims. Not a secret: it is a
# loopback endpoint the agent is meant to reach (see commands.py).
_PASSTHROUGH_ENDPOINTS = frozenset({"COTF_CMD_ENDPOINT"})
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

# Per-session env layered over the allowlist by agent_env(). A ContextVar rather
# than a parameter because the spawn happens deep inside a backend, several calls
# below the orchestrator that knows which session this is; threading it through
# would change every backend's signature. asyncio copies the context when a task
# is created, so a value set in Orchestrator._process reaches that turn's spawn
# and no other. Its whole purpose today is giving each session its own egress
# proxy, so a grant approved for one chat cannot leak into another (see
# orchestrator.SessionEgress).
# Default is None rather than {} because a mutable ContextVar default is shared
# across every context that never calls set() (ruff B039).
_SESSION_ENV: ContextVar[dict[str, str] | None] = ContextVar(
    "cotf_session_env", default=None
)


def session_env(values: dict[str, str]) -> Token[dict[str, str] | None]:
    """Layer `values` onto agent_env() for this task's turn. Reset with the token."""
    return _SESSION_ENV.set(values)


def reset_session_env(token: Token[dict[str, str] | None]) -> None:
    _SESSION_ENV.reset(token)


# Seatbelt profiles vendored from agent-seatbelt (see docs/agent/broker.md).
# The jail profile imports the base via the _BASE param.
_SEATBELT_DIR = Path(__file__).parent / "seatbelt"
_BASE_PROFILE = _SEATBELT_DIR / "fs-allow-reads.sb"
_DENY_MOST_PROFILE = _SEATBELT_DIR / "fs-deny-most.sb"
_JAIL_PROFILE = _SEATBELT_DIR / "jail.sb"

# SBPL has no arrays, so operator read grants are a fixed, documented cap.
_MAX_EXTRA_PATHS = 3
# Default loopback allow: every loopback port (agent dev servers/tests work).
_DEFAULT_LOOPBACK = "localhost:*"
# Fixed loopback allow slots in the jail profile, since SBPL has no arrays. One
# each for the credential broker, the egress proxy, and the command broker.
_LOOPBACK_SLOTS = 3
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

Reads and writes have different scopes. Do not narrow reads to the write scope: \
refusing a read you are actually permitted to make costs the user real work.
- Reading: {reads}
- Writing: {writes} Writes elsewhere fail.
- Network: {net} Your model/API access is already routed through the broker via \
*_BASE_URL, so it works without any key from you.

Attempt an operation you believe is in scope rather than declining in advance. \
If policy blocks it you get a clear error and can report that; declining without \
trying tells the user nothing about what is actually possible.

Common blocked scenarios and the remedy to relay to the user:
- Reading a file outside the allowed set (e.g. `cat ~/.aws/credentials`) fails \
with "Operation not permitted". Remedy: the operator adds the path to \
COTF_SANDBOX_EXTRA_PATHS.
- Writing a file outside the workspace fails with "Operation not permitted". \
Remedy: the operator widens the sandbox write profile.
- Reaching an external host that is not yet approved pauses while the operator \
is asked, then either succeeds or returns 403 with an "[sandbox] egress policy" \
body. A 403 means they declined: say which host you needed and why, then carry \
on with what you can. Do not retry in a loop, and do not look for another route \
to the same host.
- Reading the keychain (e.g. `security find-generic-password`) is denied, but it \
reports as "The specified item could not be found" rather than "Operation not \
permitted". Do not read that as "the credential does not exist" and do not go \
looking for it elsewhere: the item may well exist and you are simply not \
permitted to see it. You do not need it; credentials are injected by the broker."""


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
        or key in _PASSTHROUGH_ENDPOINTS
        or key.startswith(_PASSTHROUGH_PREFIXES)
        or key.endswith(_PASSTHROUGH_SUFFIXES)
    )


def agent_env() -> dict[str, str] | None:
    """Environment for a spawned agent, or None to inherit the parent's unchanged.

    None when sandboxing is off, which makes the spawn sites behave exactly as
    before (create_subprocess_exec(env=None) inherits os.environ). When on, only
    the passthrough allowlist is forwarded, so secrets in the daemon env do not
    reach the agent, then any per-session overrides are layered on top.
    """
    if not enabled():
        return None
    env = {key: value for key, value in os.environ.items() if _is_passthrough(key)}
    dropped = len(os.environ) - len(env)
    env.update(_SESSION_ENV.get() or {})
    env = _with_shims_on_path(env)
    # Names only, never values: this is the one record that "the secret did not
    # reach the agent", so it must not itself become the leak. A dropped *count*
    # rather than dropped names for the same reason — a var named
    # SLACK_USER_TOKEN is not secret, but the set of them describes the
    # deployment, and the count is what you actually diagnose from.
    logger.debug(
        "sandbox: env curated, %d forwarded %s, %d dropped by omission",
        len(env),
        sorted(env),
        dropped,
    )
    return env


def shim_dir() -> Path:
    """Where the command broker writes its shims.

    Under DATA_DIR rather than a tmpdir on purpose: DATA_DIR is not in the
    seatbelt write allowlist, so a sandboxed agent can read and exec these but
    cannot rewrite them.
    """
    from claude_on_the_fly.agent import DATA_DIR

    return DATA_DIR / "shims"


def _with_shims_on_path(env: dict[str, str]) -> dict[str, str]:
    """Prepend the shim dir to PATH so `gh` resolves to the broker shim.

    Prepended only when the dir has shims in it, so a deployment with no command
    broker running gets its PATH untouched rather than a phantom entry.

    Note this is convenience routing, not a boundary: the agent can still invoke
    /opt/homebrew/bin/gh directly. That path is useless because the profile denies
    the credential, and *that* deny is the boundary. The shim restores capability
    under the deny; it does not create the isolation.
    """
    shims = shim_dir()
    try:
        populated = shims.is_dir() and any(shims.iterdir())
    except OSError:
        return env
    if not populated:
        return env
    current = env.get("PATH", "")
    env["PATH"] = f"{shims}:{current}" if current else str(shims)
    return env


def _fs_base_profile() -> Path:
    """Filesystem base that jail.sb imports. COTF_SANDBOX_FS=deny-most selects
    fs-deny-most.sb; anything else keeps fs-allow-reads.sb (the default)."""
    if os.environ.get("COTF_SANDBOX_FS", "").lower() == "deny-most":
        return _DENY_MOST_PROFILE
    return _BASE_PROFILE


def preapproved_hosts() -> frozenset[str]:
    """Hosts the egress proxy allows without asking, from COTF_EGRESS_ALLOW.

    Comma-separated bare hostnames, e.g. "github.com,pypi.org,files.pythonhosted.org".
    This is how an operator front-loads the hosts a job is known to need so the
    run doesn't stop for an approval it would always grant. Empty means every
    host is asked about.
    """
    raw = os.environ.get("COTF_EGRESS_ALLOW", "")
    return frozenset(host.strip().lower() for host in raw.split(",") if host.strip())


def _port_from_url(value: str) -> str | None:
    """Loopback port out of a http://127.0.0.1:<port>... URL, or None."""
    if not value.startswith("http://127.0.0.1:"):
        return None
    port = value[len("http://127.0.0.1:") :].split("/", 1)[0]
    return port if port.isdigit() else None


def _spawn_env() -> dict[str, str]:
    """What the agent will actually receive: os.environ plus session overrides.

    The loopback allows must be derived from this rather than from os.environ.
    Per-session egress proxies publish HTTPS_PROXY into the ContextVar, not the
    process environment, so reading os.environ narrowed the jail to the broker
    port alone and locked the agent out of the very proxy it was handed.
    """
    return {**os.environ, **(_SESSION_ENV.get() or {})}


def _loopback_ports() -> list[str]:
    """Loopback ports of every local service the agent is being pointed at.

    Order is stable so the emitted profile is deterministic: credential broker
    (any published `*_BASE_URL`), then the egress proxy (`HTTPS_PROXY`), then the
    command broker (`COTF_CMD_ENDPOINT`). Duplicates are collapsed.
    """
    env = _spawn_env()
    found: list[str] = []
    for key in sorted(env):
        if key.endswith("_BASE_URL"):
            port = _port_from_url(env[key])
            if port is not None:
                found.append(port)
                break
    for key in ("HTTPS_PROXY", "COTF_CMD_ENDPOINT"):
        port = _port_from_url(env.get(key, ""))
        if port is not None:
            found.append(port)
    # dict preserves insertion order and dedupes.
    return list(dict.fromkeys(found))


def _loopback_specs() -> tuple[str, str, str]:
    """The remote-ip values for the jail's loopback allows, one per slot.

    Narrows to just the local services the agent was handed when
    COTF_SANDBOX_BROKER_ONLY_LOOPBACK is set, closing the arbitrary-local-sink
    path. Every slot is always filled because SBPL has no arrays: spare slots
    repeat the first port, which is a harmless duplicate allow. If no port is
    known at all, loopback stays open rather than locking the agent out of a
    service it needs.

    A port past _LOOPBACK_SLOTS would be silently unreachable, so that case warns
    loudly instead — the same fixed-slot trade as COTF_SANDBOX_EXTRA_PATHS.
    """
    if os.environ.get("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "").lower() not in _TRUTHY:
        return _DEFAULT_LOOPBACK, _DEFAULT_LOOPBACK, _DEFAULT_LOOPBACK
    ports = _loopback_ports()
    if not ports:
        logger.warning(
            "COTF_SANDBOX_BROKER_ONLY_LOOPBACK set but no broker base-url, "
            "HTTPS_PROXY, or COTF_CMD_ENDPOINT in env; leaving loopback open"
        )
        return _DEFAULT_LOOPBACK, _DEFAULT_LOOPBACK, _DEFAULT_LOOPBACK
    if len(ports) > _LOOPBACK_SLOTS:
        logger.warning(
            "%d loopback services but only %d profile slots; %s would be "
            "unreachable. Unset COTF_SANDBOX_BROKER_ONLY_LOOPBACK or add a slot.",
            len(ports),
            _LOOPBACK_SLOTS,
            ports[_LOOPBACK_SLOTS:],
        )
    specs = [f"localhost:{port}" for port in ports[:_LOOPBACK_SLOTS]]
    specs += [specs[0]] * (_LOOPBACK_SLOTS - len(specs))
    return specs[0], specs[1], specs[2]


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
    # Deferred like shim_dir(): agent imports this module, so a top-level import
    # of DATA_DIR would be a cycle.
    from claude_on_the_fly.agent import MEMORY_DIR

    writes = (
        f"the workspace ({project}), your memory ({MEMORY_DIR}), and your temp dir."
    )
    if os.environ.get("COTF_SANDBOX_FS", "").lower() == "deny-most":
        # Must stay in step with the re-grants in fs-deny-most.sb. Under-listing
        # is not harmless: the note below tells the agent not to narrow its reads,
        # and a path missing here is one it will decline to try.
        grants = [
            project,
            str(MEMORY_DIR),
            f"{home}/.claude",
            f"{home}/.claude.json",
            f"{home}/.codex",
            f"{home}/.cache/uv",
            str(shim_dir()),
            *_extra_read_paths(),
        ]
        reads = (
            "You can read only these paths: "
            + ", ".join(grants)
            + ", and your temp dir. Reads elsewhere under your home directory are "
            "blocked."
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
            "Outbound HTTPS goes through a local egress proxy that gates it by "
            "destination host. Pre-approved hosts just work; an unknown host "
            "pauses the request while the operator is asked to approve it, so a "
            "first call to a new host may take up to a minute."
        )
    return _JAIL_GUIDANCE.format(reads=reads, writes=writes, net=net)


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
    loopback, loopback_2, loopback_3 = _loopback_specs()
    params = [
        "-D",
        # realpath, like _TMPDIR and _PROJECT_DIR below. Seatbelt matches the
        # resolved path, so an unresolved param silently matches nothing: on any
        # host whose home is behind a symlink (network homes, a relocated macOS
        # home, /home/x -> /System/Volumes/Data/home/x) every credential deny in
        # the base profile would no-op while the profile still loaded and the log
        # still said "jailed". The write grants under $HOME would fail the same
        # way, which is what makes this a correctness bug and not only a leak.
        f"_HOME={os.path.realpath(Path.home())}",
        "-D",
        f"_PROJECT_DIR={project}",
        "-D",
        f"_TMPDIR={tmpdir}",
        "-D",
        f"_BASE={base}",
        "-D",
        f"_LOOPBACK={loopback}",
        "-D",
        f"_LOOPBACK_ALT={loopback_2}",
        "-D",
        f"_LOOPBACK_ALT2={loopback_3}",
    ]
    # fs-allow-reads.sb does not reference _EXTRA_*; only fs-deny-most.sb does,
    # so only pass them there. Pad unused slots with the project dir (a no-op).
    if base == _DENY_MOST_PROFILE:
        extra = _extra_read_paths()
        extra += [project] * (_MAX_EXTRA_PATHS - len(extra))
        for index, path in enumerate(extra, start=1):
            params += ["-D", f"_EXTRA_{index}={path}"]
    # The one positive record that the jail was applied. Without it a run with
    # COTF_SANDBOX unset produces a log indistinguishable from a jailed one:
    # both are simply free of denials, and no denials also reads as success.
    logger.info(
        "sandbox: jailed %s (fs=%s, loopback=%s, project=%s)",
        Path(argv[0]).name,
        base.name,
        [loopback, loopback_2, loopback_3],
        project,
    )
    logger.debug("sandbox: seatbelt params %s", params)
    return ["sandbox-exec", "-f", str(_JAIL_PROFILE), *params, *argv]


# Credential stores the profile is expected to deny. Probed at startup so the
# log carries a positive record that each deny was in force for this run.
# Every entry must be a *file*, never a directory: `cat` on a directory fails
# with "is a directory" on an unjailed host, which this would classify as ABSENT
# and quietly under-report. A file gives a clean three-way split between denied,
# missing, and readable.
_DENY_PROBES = (
    "~/.config/gh/hosts.yml",
    "~/.aws/credentials",
    "~/.ssh/id_rsa",
    "~/.docker/config.json",
    "~/.config/gcloud/credentials.db",
    "~/.sentryclirc",
)


DENIED = "denied"
ABSENT = "absent"
READABLE = "READABLE"
BROKEN = "BROKEN"


async def _probe_deny(spec: str, workspace: Path) -> str | None:
    """Attempt one expected-denied read under the live profile. Outcome, or None
    if the probe itself never ran and so says nothing either way."""
    path = os.path.expanduser(spec)
    argv = wrap(["/bin/cat", path], workspace)
    try:
        probe = await asyncio.create_subprocess_exec(
            *argv,
            env=agent_env() or {},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await asyncio.wait_for(probe.communicate(), timeout=15)
    except (OSError, TimeoutError) as exc:
        logger.warning("sandbox: deny probe for %s failed to run: %s", spec, exc)
        return None
    message = err.decode("utf-8", "replace").lower()
    if "sandbox-exec:" in message:
        # The wrapper itself rejected the profile, so this probe says nothing
        # about the boundary and neither will any other. Called out as its own
        # outcome because the first version of this reported a profile that
        # would not parse as six "absent" paths, which reads as benign.
        logger.error(
            "sandbox: probe %s could not run, the profile is broken: %s",
            spec,
            message.strip().splitlines()[0] if message.strip() else "?",
        )
        return BROKEN
    if probe.returncode == 0:
        logger.error(
            "sandbox: PROBE FAIL %s is READABLE inside the jail; the profile "
            "does not deny it",
            spec,
        )
        return READABLE
    if "not permitted" in message:
        logger.info("sandbox: probe %s denied by the profile", spec)
        return DENIED
    # Most often "No such file or directory". Reported plainly rather than
    # counted as a win, so an empty machine cannot look like a tested boundary.
    logger.info(
        "sandbox: probe %s not present, deny untested (%s)",
        spec,
        message.strip() or f"rc={probe.returncode}",
    )
    return ABSENT


async def verify_denials(workspace: Path | None = None) -> dict[str, str]:
    """Probe each expected deny under the live profile; return path -> outcome.

    macOS cannot report a seatbelt denial: a bare `deny` writes nothing to the
    unified log, `(with report)` is rejected for deny actions, and three separate
    log predicates over a real violation return nothing. Verified, not assumed.
    So the agent's own blocked reads are unobservable from this side, permanently.

    What *is* observable is whether the boundary was in force, which this answers
    by attempting the reads itself under the same profile the agent gets. It does
    not catch what the agent tried; it shows what the agent could not have
    reached.

    Three outcomes, not two, and the distinction is the whole point. An absent
    path is *not* evidence of anything: this machine simply has no credential
    there, and folding it into "denied" would let a run where every store happens
    to be missing report a boundary it never tested. Only DENIED is proof.
    """
    if mode() != "jail":
        return {}
    # Concurrently: these are six independent subprocesses, each with its own
    # 15s ceiling, and they sit on the daemon's startup path. Run in sequence the
    # worst case was a minute and a half of a daemon that had not begun serving.
    outcomes = await asyncio.gather(
        *(_probe_deny(spec, workspace or Path.cwd()) for spec in _DENY_PROBES)
    )
    results: dict[str, str] = {
        spec: outcome
        for spec, outcome in zip(_DENY_PROBES, outcomes, strict=True)
        if outcome is not None
    }
    broken = [spec for spec, outcome in results.items() if outcome == BROKEN]
    leaked = [spec for spec, outcome in results.items() if outcome == READABLE]
    denied = [spec for spec, outcome in results.items() if outcome == DENIED]
    if broken:
        logger.error(
            "sandbox: %s did not load; every agent spawn this run will fail the "
            "same way. Fix the profile before trusting this session.",
            _JAIL_PROFILE.name,
        )
    elif leaked:
        logger.error(
            "sandbox: %d credential path(s) READABLE inside the jail: %s",
            len(leaked),
            leaked,
        )
    else:
        logger.info(
            "sandbox: %d/%d probed credential paths confirmed denied under %s "
            "(%d absent, untested)",
            len(denied),
            len(results),
            _fs_base_profile().name,
            len(results) - len(denied),
        )
    return results
