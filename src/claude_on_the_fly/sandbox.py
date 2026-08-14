"""Spawn-time sandboxing for agent subprocesses.

Two independent protections, both gated by `sandbox.mode` (default off):

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
import contextlib
import logging
import os
import shutil
import sys
from contextvars import ContextVar, Token
from pathlib import Path

from claude_on_the_fly import sandbox_linux, sandbox_macos, settings

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
# Loopback endpoints the agent is deliberately handed. COTF_APPROVE_URL is here
# because the approval shim runs inside the sandbox and reads it to find the
# daemon; without the passthrough every gated call would fail closed on a
# missing endpoint, which looks exactly like a denial.
_PASSTHROUGH_ENDPOINTS = frozenset(
    {
        "COTF_CMD_ENDPOINT",
        "COTF_CMD_TOKEN",
        "COTF_APPROVE_URL",
        "COTF_APPROVE_NOTIFY_URL",
    }
)
# claude-pty reads this for its tmux session name. The daemon sets it so it knows
# which pane to type an approval into; claude-pty's own default is PID-based and
# therefore unpredictable from outside.
_PASSTHROUGH_PTY = frozenset({"CLAUDE_PTY_TMUX_SESSION", "CLAUDE_PTY_NO_TMUX"})
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


# The Linux relay's unix sockets for this turn, port -> host socket path. A
# ContextVar for the same reason as _SESSION_ENV: the spawn is several frames
# below the orchestrator, and a per-session value must not reach another session.
# Empty on macOS, where seatbelt reaches the host's loopback directly and there
# is nothing to bridge.
_SESSION_SOCKETS: ContextVar[dict[int, Path] | None] = ContextVar(
    "cotf_session_sockets", default=None
)


def session_env(values: dict[str, str]) -> Token[dict[str, str] | None]:
    """Layer `values` onto agent_env() for this task's turn. Reset with the token."""
    return _SESSION_ENV.set(values)


class SessionRelay:
    """Per-turn handle for the Linux netns bridge. A no-op on macOS.

    The orchestrator holds one of these across a turn so the platform difference
    stays here rather than in the turn loop: `open_session_relay` decides whether
    anything is needed at all, and `close` is always safe to call.
    """

    def __init__(
        self, relay: object | None, token: Token[dict[int, Path] | None] | None
    ):
        self._relay = relay
        self._token = token

    async def close(self) -> None:
        if self._token is not None:
            _SESSION_SOCKETS.reset(self._token)
        if self._relay is not None:
            await self._relay.stop()  # ty: ignore[unresolved-attribute]


async def open_session_relay(overrides: dict[str, str], key: str) -> SessionRelay:
    """Bridge the host's brokered loopback ports into this turn's namespace.

    Returns an inert handle unless this is a Linux jail. The ports come from the
    same `_loopback_ports` the seatbelt profile uses, computed against the
    overrides the caller is about to publish -- so a per-session egress proxy is
    included, which reading os.environ alone would miss.
    """
    if mode() != "jail" or not _platform().startswith("linux"):
        return SessionRelay(None, None)
    from claude_on_the_fly.agent import DATA_DIR
    from claude_on_the_fly.netns_relay import LoopbackRelay

    env_token = _SESSION_ENV.set(overrides)
    try:
        ports = [int(port) for port in _loopback_ports()]
    finally:
        _SESSION_ENV.reset(env_token)
    if not ports:
        # Nothing to bridge means the agent reaches no host service at all. That
        # is a working jail, not a broken one, but it is never what a deployment
        # wants, so it must not pass silently.
        logger.warning(
            "sandbox: no brokered loopback port for this turn; the agent will "
            "reach no host service from inside the namespace"
        )
        return SessionRelay(None, None)
    relay = LoopbackRelay(DATA_DIR / "relay" / key)
    sockets = await relay.start(ports)
    return SessionRelay(relay, _SESSION_SOCKETS.set(sockets))


def reset_session_env(token: Token[dict[str, str] | None]) -> None:
    _SESSION_ENV.reset(token)


# Re-exported from sandbox_macos so the seatbelt profile paths and the SBPL slot
# caps have one home. They stay reachable under their old names here because the
# shared layer still reports them: the startup log names the profile in force and
# `_extra_read_paths` defaults to the cap.
_BASE_PROFILE = sandbox_macos._BASE_PROFILE
_DENY_MOST_PROFILE = sandbox_macos._DENY_MOST_PROFILE
_JAIL_PROFILE = sandbox_macos._JAIL_PROFILE
_MAX_EXTRA_PATHS = sandbox_macos._MAX_EXTRA_PATHS
_DEFAULT_LOOPBACK = sandbox_macos._DEFAULT_LOOPBACK
_LOOPBACK_SLOTS = sandbox_macos._LOOPBACK_SLOTS
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

This session runs under {mechanism} with a credential broker. Some \
operations are blocked by policy. A policy block is not a transient error and \
cannot be worked around with chmod, sudo, or retrying: the only fix is an \
operator configuration change. When you hit one, tell the user the specific \
change needed, then continue with whatever you can still do.

Telling a policy block from a real error:
{errors}
- A network call that cannot connect or resolve, an error tagged "[sandbox] ... \
egress policy", or an HTTP 451 means egress policy. For HTTPS the message arrives \
as a proxy/tunnel error whose status line carries the reason, e.g. "403 Forbidden \
by egress policy: host permanently blocked, cannot be approved" — read that line, \
it says which of these happened and whether retrying can ever help.

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
- Reading a file outside the allowed set (e.g. `cat ~/.aws/credentials`) \
{block_read}. Remedy: the operator adds the path to `sandbox.extra_paths`.
- Writing a file outside the workspace {block_write}. Remedy: the operator \
widens the sandbox write profile.
- Reaching an external host that is not yet approved pauses while the operator \
is asked, then either succeeds or returns 403 with an "[sandbox] egress policy" \
body. A 403 means they declined: say which host you needed and why, then carry \
on with what you can. Do not retry in a loop, and do not look for another route \
to the same host.
{keychain}
- {brokered} A credentialed CLI that is not on that list will fail on its own \
config or token file, because the sandbox denies credential stores. That is \
policy, not a broken install, and the remedy is NOT a read grant: adding the \
credential path would either leave the tool broken or hand its token to this \
session. Remedy to relay: the operator adds the tool to the `commands:` section \
of {settings_path}. Report which command you needed and stop there — do not look \
for the credential yourself, and do not reach the same service by another route \
(a different host, another tool's token, a provider-side integration). Doing that \
launders the boundary rather than respecting it."""


# How a policy block actually reads, per platform. Both sets are measured against
# a live jail, not inferred: seatbelt refuses in place, so the path still exists
# and the errno is EPERM; bubblewrap denies by never mounting the path, so the
# read fails as if the file were not there, and a blocked write lands on a
# read-only mount instead. Telling the agent the macOS story on Linux would send
# it hunting for a file it was simply not shown.
_MACOS_ERRORS = """\
- "Operation not permitted" (EPERM) means sandbox policy (the target is outside \
your allowed set).
- "Permission denied" (EACCES) means a genuine file-permission problem, not the \
sandbox."""

_LINUX_ERRORS = """\
- "No such file or directory" for a path you have good reason to think exists \
means sandbox policy: files outside your allowed set are not hidden behind an \
error, they are simply absent from your view of the filesystem. Do not conclude \
the file does not exist on the machine, and do not go looking for it elsewhere.
- "Read-only file system" (EROFS) means sandbox policy: the location is outside \
your write set.
- "Permission denied" (EACCES) means a genuine file-permission problem, not the \
sandbox."""

_MACOS_KEYCHAIN = """\
- Reading the keychain (e.g. `security find-generic-password`) is denied, but it \
reports as "The specified item could not be found" rather than "Operation not \
permitted". Do not read that as "the credential does not exist" and do not go \
looking for it elsewhere: the item may well exist and you are simply not \
permitted to see it. You do not need it; credentials are injected by the broker."""

_LINUX_KEYCHAIN = """\
- The desktop credential stores (gnome-keyring, kwallet, anything via libsecret \
or `secret-tool`) are unreachable: they are reached over the D-Bus session bus, \
and this session has no bus. Expect "Cannot autolaunch D-Bus without X11" or a \
connection error rather than a permission error, and do not read it as "no \
credential is stored". You do not need one; credentials are injected by the \
broker."""


def mode() -> str:
    """Resolved `sandbox.mode`: 'off', 'env', or 'jail' (default 'off').

    An unrecognised value still resolves to 'off', because refusing to start would
    turn a typo into an outage. But it no longer does so in silence: a misspelled
    `jial` used to read as "no sandbox at all" with nothing anywhere to say so,
    which is the most expensive possible way to be wrong about this setting.
    """
    # The broker, proxy, and command service are built once. Keep the spawn
    # boundary on that same startup mode until the reported restart happens.
    raw = str(settings.startup_value("sandbox.mode", "off")).strip()
    value = raw.lower()
    if value in _MODES:
        return value
    if raw:
        logger.error(
            "sandbox.mode=%r is not one of %s; running with NO sandbox. Fix the "
            "value in config.yaml, or drop the key to choose that deliberately.",
            raw,
            list(_MODES),
        )
    return "off"


def enabled() -> bool:
    return mode() != "off"


def _is_passthrough(key: str) -> bool:
    return (
        key in _PASSTHROUGH
        or key in _PROXY_VARS
        or key in _PASSTHROUGH_ENDPOINTS
        or key in _PASSTHROUGH_PTY
        or key.startswith(_PASSTHROUGH_PREFIXES)
        or key.endswith(_PASSTHROUGH_SUFFIXES)
    )


def agent_env() -> dict[str, str] | None:
    """Environment for a spawned agent, or None to inherit the parent's unchanged.

    None only when there is nothing to add: sandboxing off *and* no per-session
    overrides. That keeps the spawn sites behaving exactly as before
    (create_subprocess_exec(env=None) inherits os.environ). When sandboxing is on,
    only the passthrough allowlist is forwarded, so secrets in the daemon env do
    not reach the agent, then any per-session overrides are layered on top.

    Sandboxing off *with* overrides still returns a dict, because the two settings
    are independent: `permissions.mode` does not imply a sandbox mode, and the
    approval service publishes its loopback endpoints through `_SESSION_ENV` like
    the egress proxy does. Returning None there dropped COTF_APPROVE_URL and
    CLAUDE_PTY_TMUX_SESSION on the floor, so the shim had nowhere to ask and
    claude-pty named its own tmux session -- a turn that parked at a permission
    dialog no one could answer.
    """
    overrides = _SESSION_ENV.get() or {}
    if not enabled():
        if not overrides:
            return None
        # Inherit and layer, not curate: nothing is being withheld in this mode,
        # so building the env from an allowlist would break every spawn that
        # needs a var the allowlist happens not to name.
        logger.debug(
            "sandbox: off, inheriting the daemon env with %d session override(s) %s",
            len(overrides),
            sorted(overrides),
        )
        return {**os.environ, **overrides}
    env = {key: value for key, value in os.environ.items() if _is_passthrough(key)}
    dropped = len(os.environ) - len(env)
    # The jail grants the claude session directory by name, derived from the
    # config dir the *daemon* resolves. CLAUDE_CONFIG_DIR is not a passthrough
    # key, so a deployment that sets it in DATA_DIR/.env had the daemon pointing
    # one way and the spawned CLI defaulting to ~/.claude -- which would leave the
    # grant on a directory the CLI never writes, and the session unpersisted with
    # nothing in the log. Stated explicitly so the two cannot disagree. Resolving
    # to claude's own default when unset is what the CLI would have done anyway.
    from claude_on_the_fly import envfile

    env["CLAUDE_CONFIG_DIR"] = str(envfile.claude_config_dir())
    env.update(overrides)
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
    command broker (`COTF_CMD_ENDPOINT`), then the approval service
    (`COTF_APPROVE_URL`, present only when permissions mode is `ask`). Duplicates
    are collapsed.
    """
    env = _spawn_env()
    found: list[str] = []
    for key in sorted(env):
        if key.endswith("_BASE_URL"):
            port = _port_from_url(env[key])
            if port is not None:
                found.append(port)
                break
    for key in ("HTTPS_PROXY", "COTF_CMD_ENDPOINT", "COTF_APPROVE_URL"):
        port = _port_from_url(env.get(key, ""))
        if port is not None:
            found.append(port)
    # dict preserves insertion order and dedupes.
    return list(dict.fromkeys(found))


def _fs_base_profile() -> Path:
    """The seatbelt base in force. Shared because the startup log and the agent's
    own guidance both name it, on either platform."""
    return sandbox_macos._fs_base_profile()


def _loopback_specs() -> tuple[str, str, str, str]:
    """This turn's loopback allows. Resolves the ports here and hands them to the
    profile builder, which stays pure."""
    return sandbox_macos._loopback_specs(_loopback_ports())


def _extra_read_paths(cap: int | None = _MAX_EXTRA_PATHS) -> list[str]:
    """Operator read grants for deny-most, from `sandbox.extra_paths`, realpath'd.

    `cap` is the seatbelt slot limit. Linux passes None: the cap exists only
    because SBPL has no arrays, and a mount namespace takes a list of any length.
    Carrying the limit onto a platform that does not have it would be inventing a
    restriction to look consistent.
    """
    paths = [p for p in settings.get("COTF_SANDBOX_EXTRA_PATHS").split(":") if p]
    if cap is not None and len(paths) > cap:
        logger.warning(
            "sandbox.extra_paths has %d entries; granting only the first %d "
            "(seatbelt has no arrays)",
            len(paths),
            cap,
        )
        paths = paths[:cap]
    return [os.path.realpath(p) for p in paths]


def _deny_most_in_force() -> bool:
    """Whether the least-privilege filesystem shape applies.

    On Linux it always does, whatever `sandbox.fs` says, because a mount
    namespace cannot express "readable except for these forty files". Reading the
    setting directly is how the agent came to be told it could read most of the
    filesystem while $HOME was an opaque tmpfs -- a prompt that contradicted its
    own error section, since it also tells the agent not to read "No such file"
    as proof a file is absent.
    """
    if _platform().startswith("linux"):
        return True
    return settings.get("COTF_SANDBOX_FS").lower() == "deny-most"


def _readable_paths(workspace: Path | str) -> list[str]:
    """The paths the agent may read, for the guidance note.

    Derived from the real grants on Linux rather than restated. The macOS side is
    still a hand-maintained mirror of fs-deny-most.sb, and under-listing there is
    not harmless: the note tells the agent not to narrow its reads, so a path
    missing here is one it will decline to try.
    """
    from claude_on_the_fly.agent import MEMORY_DIR

    if _platform().startswith("linux"):
        grants = _linux_grants(Path(workspace))
        readable = [*grants["read_only"], *grants["read_write"]]
        masked = {str(path) for path in grants["masked"]}
        return [str(path) for path in readable if str(path) not in masked]
    home = Path.home()
    return [
        str(workspace),
        str(MEMORY_DIR),
        f"{home}/.claude",
        f"{home}/.claude.json",
        f"{home}/.codex",
        f"{home}/.cache/uv",
        str(shim_dir()),
        *_extra_read_paths(),
    ]


def agent_guidance(workspace: Path | None = None) -> str:
    """Sandbox-awareness note for the agent's system prompt, agnostic across
    backends (all of them build their prompt through build_system_prompt).

    Empty when sandboxing is off. In jail mode it names what is blocked, how to
    tell a policy denial from a real error, and the operator remedy to relay, so
    the agent surfaces the fix instead of retrying or attempting chmod/sudo. The
    allowed-reads and egress lines reflect the actual `sandbox.fs` and
    `sandbox.broker_only_loopback` settings.
    """
    current = mode()
    if current == "off":
        return ""
    if current == "env":
        return _ENV_GUIDANCE
    project = os.path.realpath(workspace) if workspace is not None else "the workspace"
    # Deferred like shim_dir(): agent imports this module, so a top-level import
    # of DATA_DIR would be a cycle. commands is deferred for the same reason, one
    # hop further out (commands -> settings -> agent -> sandbox).
    from claude_on_the_fly import commands
    from claude_on_the_fly.agent import MEMORY_DIR

    shimmed = commands.shimmed_names()
    brokered = (
        "These commands run outside the sandbox through a broker, with the "
        f"operator's real credentials, and work normally: {', '.join(shimmed)}."
        if shimmed
        else "No credentialed CLI is brokered in this deployment."
    )

    writes = (
        f"the workspace ({project}), your memory ({MEMORY_DIR}), and your temp dir."
    )
    if _deny_most_in_force():
        reads = (
            "You can read only these paths: "
            + ", ".join(_readable_paths(project))
            + ", and your temp dir. Reads elsewhere under your home directory are "
            "blocked."
        )
    else:
        reads = (
            "You can read most of the filesystem, but reads of secrets are blocked: "
            "the keychain, SSH private keys, cloud credentials (~/.aws/credentials), "
            "and token files (~/.npmrc, ~/.netrc, ~/.env)."
        )
    if _platform().startswith("linux"):
        # Accurate for the namespace: no host service beyond the brokered ones is
        # reachable, but external hosts are, through the proxy. Saying "external
        # hosts are blocked" here would make the agent decline work it can do.
        net = (
            "Your network namespace reaches only the local broker services; no other "
            "port on the host is reachable. External hosts go through the local "
            "egress proxy, which gates them by destination: pre-approved hosts just "
            "work, an unknown one pauses while the operator is asked. Ports you bind "
            "yourself are private to this session and work normally."
        )
    elif settings.get("COTF_SANDBOX_BROKER_ONLY_LOOPBACK").lower() in _TRUTHY:
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
    linux = _platform().startswith("linux")
    return _JAIL_GUIDANCE.format(
        mechanism=(
            "a Linux bubblewrap sandbox in its own network namespace"
            if linux
            else "a macOS seatbelt sandbox"
        ),
        block_read=(
            'reports "No such file or directory" even though the file is there'
            if linux
            else 'fails with "Operation not permitted"'
        ),
        block_write=(
            'fails with "Read-only file system"'
            if linux
            else 'fails with "Operation not permitted"'
        ),
        reads=reads,
        writes=writes,
        net=net,
        brokered=brokered,
        errors=_LINUX_ERRORS if linux else _MACOS_ERRORS,
        keychain=_LINUX_KEYCHAIN if linux else _MACOS_KEYCHAIN,
        settings_path=settings.operator_settings(),
    )


_RUNTIME_SLOTS = 5


def _runtime_read_paths(argv: list[str]) -> list[Path]:
    """Directories the jail must read to run the thing it is jailing.

    The agent binary's directory and the interpreter behind it. Both commonly sit
    under `$HOME`, which every least-privilege profile makes opaque, so omitting
    them does not weaken the jail -- it stops the backend starting at all.

    `sys.prefix` and `sys.base_prefix` differ inside a virtualenv; both are
    needed, and outside one they collapse to the same path harmlessly. The
    package directory is listed separately because an editable install leaves it
    outside either prefix, and the Linux relay launcher imports from it.
    """
    paths: list[Path] = []
    binary = shutil.which(argv[0]) if argv else None
    if binary:
        # Two directories, because a launcher and the code it runs need not share
        # one. `claude` installs as a symlink in ~/.local/bin pointing into
        # ~/.local/share/claude/versions/<v>, and granting only the resolved
        # parent left execvp unable to read the symlink it has to resolve first:
        # measured as rc 71, "execvp() of 'claude' failed: No such file or
        # directory", which reads like a missing binary rather than a denial.
        # The npm layout this was first measured against had them in one place,
        # so one grant covered both by accident.
        #
        # The parent in each case, not the file: an npm-installed CLI is a shim
        # beside the package tree it loads. Read-only, and they hold executables
        # rather than secrets.
        paths.append(Path(binary).parent)
        paths.append(Path(os.path.realpath(binary)).parent)
    paths += [Path(sys.prefix), Path(sys.base_prefix), Path(__file__).parent]
    seen: dict[str, Path] = {}
    for path in paths:
        seen.setdefault(str(path), path)
    return list(seen.values())


def _claude_session_paths(workspace: Path) -> tuple[Path, Path, Path]:
    """(the config dir, the session store to deny, this thread's dir to grant).

    The config dir comes first because every other claude rule in both profiles is
    written against it rather than against `$HOME/.claude`: CLAUDE_CONFIG_DIR can
    move the whole tree outside `$HOME`, where a `_HOME`-derived rule matches
    nothing while the profile still loads.

    claude writes its session JSONL to `<config dir>/projects/<workspace hash>/`,
    and one workspace is one chat thread. Two paths rather than one because the
    policy is a pair: deny the store, re-grant the running thread. Granting
    without the deny leaves every other thread readable; denying without the
    grant stops the CLI persisting the session it is currently writing, and the
    turn still completes, so nothing surfaces until a resume comes back empty.

    Resolved through `transcript`, which owns the hash scheme the CLI uses, so
    the grant and the daemon's own reader cannot drift apart. Realpath'd like
    every other profile parameter: seatbelt matches the resolved path, and a
    home behind a symlink would otherwise leave the grant matching nothing.
    Neither path has to exist yet -- realpath resolves the existing prefix, and
    the grant covers the directory itself, so the CLI may create it.
    """
    from claude_on_the_fly import envfile, transcript

    return (
        Path(os.path.realpath(envfile.claude_config_dir())),
        Path(os.path.realpath(transcript.claude_projects_dir())),
        Path(os.path.realpath(transcript.claude_session_dir(workspace))),
    )


def _codex_session_paths(workspace: Path) -> tuple[Path, Path]:
    """(the shared rollout tree to deny, this thread's codex home to grant).

    codex names a rollout by date and thread id in one flat tree and picks the
    name at startup, so there is no per-workspace path to grant the way claude's
    `projects/<hash>` can be. The workspace gets its own `CODEX_HOME` instead
    (`codex_state.home_dir`), which is what makes the location predictable before
    the run, and the backend points the child at it.

    The shared tree still has to be denied: it holds every rollout written before
    per-workspace homes existed, and a jailed turn could otherwise read the raw
    turns of every other thread. 1088 of them were readable on the host this was
    measured on.
    """
    from claude_on_the_fly import codex_state, envfile

    return (
        Path(os.path.realpath(envfile.codex_home() / "sessions")),
        Path(os.path.realpath(codex_state.home_dir(workspace))),
    )


def _platform() -> str:
    """Which jail mechanism applies.

    A function rather than a module constant so a test can drive either branch on
    either OS. Coverage is enforced at 100%, and a platform read frozen at import
    would leave the other platform's jail permanently unreachable -- which is
    exactly the code you least want untested.
    """
    return sys.platform


# What a real claude turn writes under its config directory, besides its own
# session directory. Measured the way the codex list below was: two turns against
# the real CLI, one making a Bash tool call and one resuming with `--continue`,
# diffing the config tree either side. A grant missing from here is a capability
# the agent silently loses under the jail; a grant here it does not need is attack
# surface, so the list is the measurement and not a guess.
#
# None of these decides what the agent executes or is told, which is the line that
# keeps them separable from settings.json, hooks, commands, skills, agents and the
# plugins/ root. Those stay denied: they are read on later invocations, so a write
# there outlives the session.
#
# Deliberately NOT here:
#   projects/         conversation-bearing, granted per thread instead
#   history.jsonl     cross-project prompt history, and a read leak of its own
#   todos/, statsig/  no real turn was observed writing them
_CLAUDE_RUNTIME_WRITE_DIRS = (
    # Observed being written by a real turn against the operator's own config
    # directory, and kept for that reason -- but a jailed `claude -p` turn that
    # actually executed a Bash tool call wrote none, on a fresh config directory
    # or an existing one, so this grant is not what makes tool use work. The path
    # production uses is claude-pty, an interactive shell, which is where a shell
    # snapshot plausibly is written and which is not yet measured under the jail.
    # Kept rather than dropped because removing it would trade attack surface for
    # the risk of breaking the one path that has not been tested.
    "shell-snapshots",
    "session-env",
    # Distinct from projects/, despite the name.
    "sessions",
    # cache/ only, so a manifest at the plugins/ root stays denied.
    "plugins/cache",
)

# Split from the directories rather than derived from the name, the same
# distinction _CODEX_PROTECTED_DIRS makes and for the same reason: creating a
# mount source with mkdir when the target is a file leaves a *directory* called
# policy-limits.json, and the CLI then cannot write its own state.
_CLAUDE_RUNTIME_WRITE_FILES = ("policy-limits.json",)

_CLAUDE_RUNTIME_WRITES = (
    *_CLAUDE_RUNTIME_WRITE_DIRS,
    *_CLAUDE_RUNTIME_WRITE_FILES,
)

# Files under the claude config directory a turn must not read, named one by one
# the way the credential denies are. history.jsonl is every prompt typed in every
# project on the host, so it crosses threads exactly like projects/ does, and no
# measured turn writes it.
_CLAUDE_READ_DENIED = ("history.jsonl",)


# ~/.codex is writable on Linux, with the dangerous entries mounted read-only back
# over it. That inverts the seatbelt profile, which denies the directory and
# re-grants a measured list, and the inversion is forced rather than chosen.
#
# Seatbelt can write `(deny file-write* (regex ".../[^/]*\\.sqlite(-wal|-shm)?$"))`
# and have it cover a file that does not exist yet. A mount namespace has nothing
# to mount over an absent path, so the re-grant list cannot. Measured under a live
# jail: codex opens `~/.codex/state_5.sqlite` with O_CREAT on a fresh install and
# exits with "failed to initialize in-process app-server client: Read-only file
# system" when it cannot. The `5` is a schema version, so pre-creating the file by
# name is a race against the next codex release.
#
# What must NOT become writable is anything deciding what codex executes or is
# told, because those outlive the session. Each is mounted read-only whether or
# not it exists (an absent one gets a placeholder), so a jailed turn cannot create
# an AGENTS.md to leave itself standing orders for the next run.
_CODEX_PROTECTED = (
    "config.toml",
    "hooks.json",
    "AGENTS.md",
    "rules",
    "plugins",
    "agents",
)

# Which of the entries above are directories. Named rather than derived: the
# extension-less ones are a mix of both kinds, so a stand-in created for an
# absent target has to be told which it is.
_CODEX_PROTECTED_DIRS = ("rules", "plugins", "agents")


# Written inside an area the agent may otherwise write, so each needs an explicit
# read-only mount back over it. Everything else on macOS's write-deny list
# (~/.ssh, ~/.gnupg, ~/.gitconfig, the shell rc files) needs no equivalent here:
# $HOME is a read-only tmpfs under this profile, so those are already unwritable.
_PROJECT_WRITE_DENIES = (
    ".git/hooks",
    ".git/config",
    ".mcp.json",
    ".vscode",
    ".idea",
    # A shell rc or git identity inside the workspace is read by the next command
    # run there, so it persists instructions the same way .git/hooks does.
    ".bashrc",
    ".zshrc",
    ".gitconfig",
)

# Same distinction as _CODEX_PROTECTED_DIRS. `.vscode` and `.bashrc` are both
# extension-less and only one of them is a directory, so guessing from the name
# put a directory called `.bashrc` in the operator's workspace and a directory at
# `.git/config`, which makes `git init` there fail outright.
_PROJECT_WRITE_DENY_DIRS = (".git/hooks", ".vscode", ".idea")


def _project_write_denies(project: Path, names: tuple[str, ...]) -> list[Path]:
    """`names` under `project`, minus the `.git/` ones for a linked worktree.

    In a worktree or a submodule `.git` is a *file* naming a gitdir that lives
    outside the workspace, and so is already read-only under `--ro-bind / /`.
    Mounting over `<project>/.git/hooks` there is not merely redundant: creating
    the mount point raises NotADirectoryError and takes the whole turn with it.
    """
    dot_git = project / ".git"
    if dot_git.exists() and not dot_git.is_dir():
        names = tuple(name for name in names if not name.startswith(".git/"))
    return [project / name for name in names]


def _session_mount_sources(workspace: Path) -> tuple[list[Path], list[Path]]:
    """(directories, files) bubblewrap binds read-write, which must exist first.

    Named separately from the grants so the Linux wrap can create them without
    `_linux_grants` acquiring a side effect: `_readable_paths` calls it purely to
    build the agent's guidance note, and that must not touch the filesystem.
    """
    claude_config, _, claude_project = _claude_session_paths(workspace)
    codex_sessions, codex_home = _codex_session_paths(workspace)
    directories = [
        claude_project,
        codex_home,
        # Where codex actually writes rollouts. `codex_state.ensure_home` creates it
        # too, but only when the codex backend is the one spawning; the jail must not
        # depend on which backend ran first, and the preflight probe writes here.
        codex_home / "sessions",
        # Not writable, and created for exactly that reason: the shared rollout
        # tree is masked with a tmpfs, and a mask can only be mounted over a path
        # that exists. Absent, it was left unmasked, and because Linux binds
        # ~/.codex read-write a jailed turn could then create the tree itself and
        # write into it -- working but unisolated, where macOS refuses outright.
        # Measured on a host that had never run codex.
        codex_sessions,
        *(claude_config / name for name in _CLAUDE_RUNTIME_WRITE_DIRS),
    ]
    files = [claude_config / name for name in _CLAUDE_RUNTIME_WRITE_FILES]
    return directories, files


def _ensure_session_mount_sources(workspace: Path) -> None:
    """Materialise every per-thread mount source, before the grants are computed.

    Two reasons a source has to exist, and they pull in opposite directions, which
    is why this cannot be skipped when a path is absent:

      - A read-write bind needs something to bind, or bwrap fails the whole spawn
        with "Can't mkdir parents ... Read-only file system".
      - A mask needs something to mount over. An absent one is silently left
        unmasked, and for the shared codex tree that is a real hole, because Linux
        binds ~/.codex read-write.
    """
    directories, files = _session_mount_sources(workspace)
    for source in directories:
        try:
            source.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Never silent: a source that cannot be created is either a mount the
            # turn then lacks, or a mask that then does not apply, and the second
            # one is a boundary quietly going missing.
            logger.warning("sandbox: could not create mount source %s: %s", source, exc)
    for source in files:
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            source.touch(exist_ok=True)
        except OSError as exc:
            logger.warning("sandbox: could not create mount source %s: %s", source, exc)


def _linux_grants(workspace: Path) -> dict[str, list[Path]]:
    """The deny-most contract as mount lists. Mirrors fs-deny-most.sb."""
    from claude_on_the_fly.agent import DATA_DIR, MEMORY_DIR

    home = Path(os.path.realpath(Path.home()))
    data_dir = Path(os.path.realpath(DATA_DIR))
    project = Path(os.path.realpath(workspace))
    tmpdir = Path(os.path.realpath(os.environ.get("TMPDIR", "/tmp")))
    codex = home / ".codex"
    claude_config, claude_projects, claude_project = _claude_session_paths(workspace)
    codex_sessions, codex_home = _codex_session_paths(workspace)
    read_write = [
        project,
        Path(os.path.realpath(MEMORY_DIR)),
        # The running thread's claude session directory. Deeper than the opaque
        # projects/ tmpfs below, so depth ordering restores exactly this one --
        # the same trade the plugins/cache entry makes further down. Without it
        # the CLI cannot write the session file resume reads, and the turn still
        # succeeds, so the loss is silent.
        claude_project,
        # The measured runtime scratch. Deeper than the read-only ~/.claude mount,
        # so depth ordering restores exactly these. They are created on the host
        # before the wrap for the same reason the session dirs are: bwrap cannot
        # make a mount point under a read-only root.
        *(claude_config / name for name in _CLAUDE_RUNTIME_WRITES),
        tmpdir,
        home / ".claude.json",
        home / ".cache/uv",
        home / ".ollama",
        home / ".Trash",
        # Writable so codex can create new state files it names itself; the
        # entries below take the dangerous parts back. plugins/cache is deeper
        # than the read-only plugins/ mount, so depth ordering restores it.
        codex,
        codex / "plugins/cache",
        codex / "plugins/.remote-plugin-install-staging",
        # This thread's codex home, where its rollouts and sqlite state go. Under
        # the data dir, which is opaque, so without this the directory the backend
        # just pointed the child at would not exist inside the namespace.
        codex_home,
    ]
    return {
        # $HOME opaque, and the data dir too so a redirected COTF_DATA_DIR outside
        # $HOME gets the same treatment. memory/ and shims/ are granted back below
        # at greater depth, so the sibling .env and logs/ stay hidden either way.
        # projects/ is opaque rather than merely read-only: the read-only mount of
        # ~/.claude below would otherwise expose every other thread's verbatim
        # turns, which is worse in kind than the credentials this profile denies.
        # An empty tmpfs also keeps the directory listable, so a CLI that stats it
        # sees a plausible store rather than a missing one.
        #
        # Each is masked only if it is actually there. bwrap creates its mount
        # points inside a `--ro-bind / /` root, so naming an absent one fails the
        # whole spawn with "Can't mkdir parents ... Read-only file system" rather
        # than being ignored. A tree that does not exist holds no other thread's
        # transcripts either, so there is nothing to hide. The read-write sources
        # above are different: those are created on the host before the wrap,
        # because the turn needs them whether or not anything made them yet.
        "opaque": [
            home,
            data_dir,
            *(path for path in (claude_projects, codex_sessions) if path.is_dir()),
        ],
        "read_only": [
            home / ".claude",
            codex,
            data_dir / "shims",
            *(Path(p) for p in _extra_read_paths(cap=None)),
        ],
        "read_write": read_write,
        "write_denied": [
            *_project_write_denies(project, _PROJECT_WRITE_DENIES),
            *_codex_protected(codex),
        ],
        "write_denied_dirs": [
            *_project_write_denies(project, _PROJECT_WRITE_DENY_DIRS),
            *(codex / name for name in _CODEX_PROTECTED_DIRS),
        ],
        # history.jsonl joins the ssh-agent socket and the stray .env files: a file
        # a coarser grant exposes, named individually. ~/.claude is a read-only
        # mount here rather than a global read allow, so hiding one file inside it
        # needs a mount over that file, which is what masked does.
        "masked": [
            *_linux_masked(data_dir),
            *(
                path
                for name in _CLAUDE_READ_DENIED
                if (path := claude_config / name).exists()
            ),
        ],
    }


def _linux_masked(data_dir: Path) -> list[Path]:
    """Paths a coarser grant would otherwise expose, named individually.

    Two of them, and neither has a macOS counterpart because seatbelt expresses
    both with a rule rather than a mount.

    The ssh-agent socket is the sharper one. `SSH_AUTH_SOCK` is forwarded to the
    agent on both platforms, and on macOS the socket behind it is unusable
    because the jail profile permits no unix socket at all -- a decision jail.sb
    records as deliberate, since `(remote unix)` is not path-scopeable. A mount
    namespace has the opposite default: `--ro-bind / /` reaches every socket on
    the machine, so a jailed turn could connect to the agent and sign as the
    operator. Found by running one contract against both jails; reading either
    profile would not have shown it.

    The `.env` sweep covers the daemon's own secrets landing beneath a granted
    subtree. The data dir is opaque, but `memory/` is granted back read-write, so
    a `.env` inside it reappears. Seatbelt covers this with one regex over the
    whole data dir; there is no pattern matching here, so the files are resolved
    now. Glob rather than an exact name because the macOS regex is unanchored at
    the end and so already covers `.env.bak` and friends -- which is the shape a
    backup taken before an edit actually has.
    """
    masked: list[Path] = []
    auth_sock = os.environ.get("SSH_AUTH_SOCK")
    if auth_sock:
        masked.append(Path(os.path.realpath(auth_sock)))
    for granted in ("memory", "shims"):
        base = data_dir / granted
        if base.is_dir():
            masked += sorted(base.rglob(".env*"))
    return masked


def _codex_protected(codex: Path) -> list[Path]:
    """Entries under ~/.codex to mount read-only over the writable directory.

    The named ones, existing or not, so the file cannot be created either.

    An earlier version also swept in every other entry already at the root, on
    the theory that a config file a future codex release introduces should be
    protected the day it appears. That is the safer-sounding rule and it is
    wrong: codex creates new *state* under this directory as it runs, and the
    sweep froze it. Measured on the second live run, where the first run's own
    `thread-writer-locks/` had appeared and codex then died with "failed to open
    thread writer coordination lock ... Read-only file system", unable to resume
    any conversation.

    So the list is deliberately named-only. It covers what actually carries the
    threat -- config, hooks, standing instructions, plugin and rule loading, all
    of which outlive the session -- and lets codex own the rest of its directory.
    Extending it is a one-line change when a release adds another such file.
    """
    return [codex / name for name in _CODEX_PROTECTED]


def _linux_wrap(argv: list[str], workspace: Path) -> list[str]:
    """Wrap argv in a bubblewrap jail. Raises if bwrap is not installed."""
    from claude_on_the_fly.agent import DATA_DIR

    if not shutil.which("bwrap"):
        raise SandboxBoundaryError(
            "sandbox.mode is jail but bubblewrap is not installed, so there is no "
            "jail to run in. Install it (apt install bubblewrap) or set "
            f"sandbox.mode to env or off in {settings.operator_settings()}."
        )
    grants = _linux_grants(workspace)
    # The jail has to be able to read the thing it is jailing. $HOME is an opaque
    # tmpfs here, so a backend installed under it -- an npm global into
    # ~/.local/bin, a release binary dropped in ~/bin -- disappears, and the spawn
    # dies with "No such file or directory: codex" before any policy is exercised.
    # A package manager install into /usr/local/bin happens to be unaffected,
    # which is exactly why this is worth handling rather than leaving to whoever
    # installs it somewhere else.
    #
    # The parent directory rather than the file: an npm-installed CLI is a shim
    # next to the package tree it loads. Read-only, and it holds executables
    # rather than secrets.
    grants["read_only"] += _runtime_read_paths(argv)
    # Same reason `ensure_write_deny_targets` materialises its targets: a mount
    # source has to exist on the host, because bwrap cannot create one inside the
    # read-only root. Both are this turn's own session directories, so creating
    # them is what the turn was going to do anyway; a claude turn gets an empty
    # codex home and vice versa, which costs one directory and keeps the wrap
    # independent of which backend is running.
    placeholders = sandbox_linux.prepare_placeholders(DATA_DIR / "jail")
    sandbox_linux.ensure_write_deny_targets(
        grants["write_denied"], placeholders, grants["write_denied_dirs"]
    )
    sockets = _SESSION_SOCKETS.get() or {}
    if not sockets:
        # Reached by any caller that spawns without opening a relay first -- the
        # jobs daemon is the live example, since it runs as its own process and
        # builds no broker or proxy at all. The namespace then has no route to
        # any host service, so the agent cannot reach a brokered model endpoint.
        #
        # macOS is in the same position for the same reason (nothing in that
        # process is listening on loopback, and the profile denies the internet),
        # so this is not a Linux regression. It is only invisible there, which is
        # why it is said out loud here rather than left to look like a hang.
        logger.warning(
            "sandbox: jailing %s with no brokered loopback port. Nothing on the "
            "host is reachable from inside the namespace, so a backend needing a "
            "model endpoint will fail. Chat turns open a relay; the jobs daemon "
            "does not, and `sandbox.mode: jail` does not support it.",
            argv[0] if argv else "?",
        )
    jailed = sandbox_linux.jail_argv(
        argv,
        opaque=grants["opaque"],
        read_only=grants["read_only"],
        read_write=grants["read_write"],
        write_denied=grants["write_denied"],
        write_denied_dirs=grants["write_denied_dirs"],
        masked=grants["masked"],
        sockets=sockets,
        placeholders=placeholders,
    )
    logger.info(
        "sandbox: jailed %s under bubblewrap (project=%s, brokered ports=%s)",
        Path(argv[0]).name if argv else "?",
        os.path.realpath(workspace),
        sorted(sockets) or "none (no host service reachable)",
    )
    return jailed


def wrap(argv: list[str], workspace: Path) -> list[str]:
    """Wrap argv in the platform's jail when `sandbox.mode: jail`, else return it
    unchanged.

    macOS gets the vendored seatbelt profile, Linux gets bubblewrap plus a network
    namespace (see sandbox_linux). Both refuse to run rather than degrade: a
    configured jail that silently did not apply is the one outcome worth failing
    startup over.

    On macOS this invokes sandbox-exec against the vendored jail profile; the
    agent's environment is curated separately by agent_env(). `sandbox.fs:
    deny-most` swaps the read-permissive base for a least-privilege one and
    forwards `sandbox.extra_paths` grants. `sandbox.broker_only_loopback` narrows
    egress from all loopback to just the broker port.

    Neither of those two settings applies on Linux, and the reasons differ.
    `sandbox.fs` has no allow-reads equivalent: a mount namespace cannot express
    "readable except for these forty files", so deny-most is the only shape and
    the setting resolves to it with a log line. `broker_only_loopback` is
    permanently in force: the namespace only ever contains the relay's sockets,
    so no other host service is reachable to narrow away. Linux is at or above
    the macOS posture under either value of either setting.
    """
    if mode() != "jail":
        return argv
    # Before either platform branch, and before `_linux_grants` in particular, which
    # decides whether to mask a session store by whether it exists: creating these
    # afterwards left the shared codex tree unmasked and then created it, and since
    # Linux binds ~/.codex read-write a jailed turn could write a rollout straight
    # into it. Measured at rc 0 with the file landing on the host.
    #
    # Both platforms, not only the one that needs mount sources. A jailed process
    # creating its own directory chain hits the same wall the probes kept finding: a
    # recursive mkdir that cannot stat an ancestor walks up and tries to create it,
    # which under an opaque $HOME fails at the home directory itself. Creating the
    # chain here means the CLI only ever writes files into a directory that is
    # already there.
    _ensure_session_mount_sources(workspace)
    if _platform().startswith("linux"):
        return _linux_wrap(argv, workspace)
    if not shutil.which("sandbox-exec"):
        # Used to warn and hand back the bare argv, which on a Mac missing
        # sandbox-exec meant "run this turn unjailed" and said so only in a log
        # nobody reads until afterwards. A jail that was configured and did not
        # apply is worth failing on, and now that a second platform exists this
        # branch can no longer mean "you are simply not on macOS" either.
        raise SandboxBoundaryError(
            "sandbox.mode is jail but sandbox-exec was not found, so there is no "
            "jail to run in. Set sandbox.mode to env or off in "
            f"{settings.operator_settings()} to run without one deliberately."
        )
    # Deferred like shim_dir(): agent imports this module, so a top-level import
    # of DATA_DIR would be a cycle.
    from claude_on_the_fly.agent import DATA_DIR

    base = _fs_base_profile()
    claude_config, claude_projects, claude_project = _claude_session_paths(workspace)
    codex_sessions, codex_home = _codex_session_paths(workspace)
    return sandbox_macos.jail_argv(
        argv,
        **sandbox_macos.realpaths(workspace, DATA_DIR),
        claude_config=claude_config,
        claude_projects=claude_projects,
        claude_project=claude_project,
        codex_sessions=codex_sessions,
        codex_home=codex_home,
        base=base,
        profile=_JAIL_PROFILE,
        runtime_paths=[str(path) for path in _runtime_read_paths(argv)],
        loopback=sandbox_macos._loopback_specs(_loopback_ports()),
        extra_paths=_extra_read_paths() if base == _DENY_MOST_PROFILE else [],
    )


# Credential stores the profile is expected to deny. Probed at startup so the
# log carries a positive record that each deny was in force for this run.
# Every entry must be a *file*, never a directory: `cat` on a directory fails
# with "is a directory" on an unjailed host, which this would classify as ABSENT
# and quietly under-report. A file gives a clean three-way split between denied,
# missing, and readable.
#
# The cotf .env probe is the default location's. When COTF_DATA_DIR redirects
# the daemon's data dir, the default directory is *another daemon's* and the
# boundary to that one still has to hold, so the probe stays. The daemon's own
# .env sits wherever DATA_DIR points and is appended by `_deny_probe_specs`,
# because a literal probe cannot know that path at import.
_DEFAULT_COTF_ENV = "~/.claude-on-the-fly/.env"

_DENY_PROBES = (
    "~/.config/gh/hosts.yml",
    "~/.aws/credentials",
    "~/.ssh/id_rsa",
    "~/.docker/config.json",
    "~/.config/gcloud/credentials.db",
    "~/.sentryclirc",
    # cotf's own frontend tokens. Probed because the rule covering them is a regex
    # rather than a subpath -- the directory around it is deliberately readable -- and
    # a regex is the kind of rule that can be narrowed by accident and stay silent
    # about it. macOS cannot report a seatbelt denial, so this is the only place the
    # boundary gets checked instead of assumed.
    _DEFAULT_COTF_ENV,
)


def _deny_probe_specs() -> tuple[str, ...]:
    """The static probe list plus this daemon's own `.env`.

    The own `.env` hangs off DATA_DIR, which COTF_DATA_DIR can point anywhere;
    a literal probe would target the default directory and prove nothing about
    the file holding this daemon's tokens. Deduped against the default location
    so the common case probes the file once.
    """
    from claude_on_the_fly.agent import DATA_DIR

    own = str(DATA_DIR / ".env")
    if os.path.realpath(Path(_DEFAULT_COTF_ENV).expanduser()) == os.path.realpath(own):
        return _DENY_PROBES
    return (*_DENY_PROBES, own)


def _probe_workspace() -> Path:
    """Throwaway workspace for the startup probes, under the data dir."""
    from claude_on_the_fly.agent import DATA_DIR

    path = DATA_DIR / "jail" / "probe"
    path.mkdir(parents=True, exist_ok=True)
    return path


DENIED = "denied"
ABSENT = "absent"
READABLE = "READABLE"
BROKEN = "BROKEN"


class SandboxBoundaryError(RuntimeError):
    """The live jail did not enforce one of its credential-read denies."""


async def _probe_deny(spec: str, workspace: Path) -> str | None:
    """Attempt one expected-denied read under the live profile. Outcome, or None
    if the probe itself never ran and so says nothing either way."""
    path = os.path.expanduser(spec)
    # Settle absent-versus-denied from OUTSIDE the jail, before probing.
    #
    # The message alone cannot do it on Linux. bubblewrap hides a path by not
    # mounting it, so a successfully denied read reports "No such file or
    # directory" -- character for character what a genuinely missing file
    # reports. Classifying on the message would file every hidden credential as
    # ABSENT, and ABSENT is the outcome that proves nothing, so a jail working
    # perfectly would report a boundary it had never tested. That is worse than
    # the bug it hides, because it reads as success.
    #
    # Existence out here is not a guess about the mechanism, so it works the same
    # on both platforms and leaves the probe below one question: given that this
    # file is really there, could the agent read it?
    if not os.path.exists(path):
        logger.info("sandbox: probe %s not present, deny untested", spec)
        return ABSENT
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
    if "sandbox-exec:" in message or "bwrap:" in message:
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
    # The file is on this machine (checked above) and the jailed process could
    # not read it, so the boundary held. Which errno says so is the platform's
    # business: seatbelt refuses in place and says "Operation not permitted",
    # bubblewrap never mounts it and says "No such file or directory". Requiring
    # a particular wording here would make the check pass on one platform and
    # silently under-report on the other.
    logger.info(
        "sandbox: probe %s denied by the profile (%s)",
        spec,
        message.strip().splitlines()[0]
        if message.strip()
        else f"rc={probe.returncode}",
    )
    return DENIED


# Attempted from inside the jail at startup. Any external host would do; this one
# is a well-known anycast resolver, so a *success* here means egress is open
# rather than that one host happened to be up.
_EGRESS_PROBE = (
    "import socket,sys\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 443), 5).close()\n"
    "    sys.stdout.write('REACHED')\n"
    "except OSError as exc:\n"
    "    sys.stdout.write('BLOCKED:%s' % exc)\n"
)


async def _run_jailed(argv: list[str], workspace: Path, timeout: int = 20):
    """Run argv under the live jail. Returns (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *wrap(argv, workspace),
        env=agent_env() or {},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, (out + err).decode("utf-8", "replace")


# What bubblewrap says when the kernel let it make the namespace and then refused
# it netlink inside. Confirmed on a GitHub runner: apparmor in the LSM list and
# kernel.apparmor_restrict_unprivileged_userns=1, which is the stock posture on
# Ubuntu 23.10 and later. Worth matching because it is the failure most operators
# on a current Ubuntu will hit, and the message alone points at networking rather
# than at the setting that actually caused it.
_USERNS_SIGNATURE = "rtm_newaddr"
_USERNS_HINT = (
    " -- this host restricts unprivileged user namespaces, which is the default on "
    "Ubuntu 23.10 and later. Allow them with "
    "`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (persist it in "
    "/etc/sysctl.d/), or set sandbox.mode to env to run without a jail deliberately."
)


def _userns_hint(output: str) -> str:
    """The remedy, appended only when the failure actually looks like that one."""
    return _USERNS_HINT if _USERNS_SIGNATURE in output.lower() else ""


def _log_inert_settings() -> None:
    """Say so when a setting the operator wrote cannot take effect here.

    Neither of these weakens anything (Linux is at or above the macOS posture
    under either value), but an operator who set one and got no behaviour change
    deserves to learn that from a log rather than from reading the source.
    """
    if not _platform().startswith("linux"):
        return
    if settings.get("COTF_SANDBOX_FS"):
        logger.info(
            "sandbox: sandbox.fs has no effect on Linux; a mount namespace cannot "
            "express allow-reads, so deny-most is always in force"
        )
    if settings.get("COTF_SANDBOX_BROKER_ONLY_LOOPBACK"):
        logger.info(
            "sandbox: sandbox.broker_only_loopback has no effect on Linux; the "
            "namespace only ever contains the brokered services, so it is always on"
        )


async def preflight() -> None:
    """Prove the jail starts and holds its egress deny, before serving anything.

    `verify_denials` cannot stand in for this. It now settles absent-versus-denied
    outside the jail, so on a machine that happens to have none of the probed
    credential files it spawns nothing at all -- and a jail that never starts
    would go unnoticed precisely because there was nothing to notice.

    Three checks, all cheap and all startup-fatal:

      1. The jail runs a trivial command. Catches a missing mechanism, a profile
         that will not parse, and on Linux the case this cannot be reasoned about
         from config alone: bubblewrap installed but unprivileged user namespaces
         disabled, which is the default posture on some distributions.
      2. An external connection from inside is refused. This is the load-bearing
         claim of the whole design -- that the egress proxy cannot be bypassed --
         and until now nothing checked it on either platform. A jail whose network
         rules silently did not apply looks exactly like one whose did.
      3. A jailed process can write the session directory the agent CLI persists
         to. The only positive check of the three, and it exists because the
         failure it catches is invisible: the CLI *is* the jailed process, so a
         profile that denies its session file still completes every turn and only
         shows up as a resume that has forgotten the conversation, or a memory
         that silently stopped being kept. That shipped once. The other two
         checks would not have caught it, because nothing was denied that the
         jail was asked to deny.
    """
    if mode() != "jail":
        return
    _log_inert_settings()
    # First, and before anything is spawned: this is a layout problem, so paying
    # for two jail probes to discover it afterwards is waste.
    _preflight_protected_symlinks()
    workspace = _probe_workspace()
    try:
        code, output = await _run_jailed(["/bin/echo", "cotf"], workspace)
    except (OSError, TimeoutError) as exc:
        raise SandboxBoundaryError(
            f"sandbox preflight could not run the jail: {exc}"
        ) from exc
    if code != 0 or "cotf" not in output:
        raise SandboxBoundaryError(
            "sandbox preflight failed: the jail could not run a trivial command "
            f"(rc={code}): {output.strip()[:400]}{_userns_hint(output)}"
        )
    try:
        _code, output = await _run_jailed(
            [sys.executable, "-c", _EGRESS_PROBE], workspace
        )
    except (OSError, TimeoutError) as exc:
        raise SandboxBoundaryError(
            f"sandbox egress preflight could not run: {exc}"
        ) from exc
    if "REACHED" in output:
        raise SandboxBoundaryError(
            "sandbox preflight failed: a jailed process reached the internet "
            "directly, so the egress proxy can be bypassed and every host "
            "allowlist is advisory. Refusing to start autonomous work."
        )
    if "BLOCKED" not in output:
        # Neither answer means the probe never got far enough to be evidence,
        # most likely because the interpreter is not readable inside the jail.
        raise SandboxBoundaryError(
            f"sandbox egress preflight was inconclusive: {output.strip()[:400]}"
        )
    await _preflight_session_write(workspace)
    logger.info(
        "sandbox: preflight ok, jail starts, external egress is refused, and the "
        "agent can persist its session"
    )


def _preflight_protected_symlinks() -> None:
    """Report execution-control paths that are symlinks, before a turn hits them.

    These are the entries the jail protects because they decide what the agent
    executes or is told: config, hooks, standing instructions, rules, plugins,
    agents. A symlink there breaks each platform differently, and neither failure
    announces itself as "your instruction files are not protected":

      - Linux cannot mount over it. bwrap reports "Can't create file at <path>:
        No such file or directory" and the turn dies, which reads like a missing
        file rather than a layout it refuses. Measured with a `~/.codex/AGENTS.md`
        symlinked to `~/.claude/CLAUDE.md`, which is an ordinary way to keep one
        set of instructions for both backends.
      - macOS resolves the path before matching, so a deny written against the
        link covers the link and not the file behind it. The profile loads, the
        log says jailed, and the target stays writable.

    Fatal only on Linux, where the turn would fail anyway. On macOS this is a real
    weakening but an established layout, so it warns rather than refusing to serve
    a deployment that has been working.
    """
    codex = Path(os.path.realpath(Path.home())) / ".codex"
    linked = [path for path in _codex_protected(codex) if path.is_symlink()]
    if not linked:
        return
    names = ", ".join(str(path) for path in linked)
    if _platform().startswith("linux"):
        raise SandboxBoundaryError(
            "sandbox preflight failed: these execution-control paths are symlinks, "
            f"and a mount namespace cannot mount read-only over one: {names}. "
            "Replace each with a real file or directory, or move the content and "
            "drop the link, then restart."
        )
    logger.warning(
        "sandbox: %d execution-control path(s) are symlinks: %s. Seatbelt matches "
        "the resolved path, so each write deny protects the link and not the file "
        "behind it, and a jailed turn could rewrite instructions the next run "
        "reads. Replace them with real files to close that.",
        len(linked),
        names,
    )


_SESSION_PROBE_NAME = ".cotf-preflight"


async def _preflight_session_write(workspace: Path) -> None:
    """Prove a jailed process can write the store the agent CLI persists to.

    Probes the same path `wrap` grants for this workspace, so it exercises the real
    grant rather than a stand-in. The probe workspace is the daemon's own, so the
    directory this creates is not a live thread's.

    Both backends are checked because the two stores are granted by different
    mechanisms -- claude by a path derived from the workspace, codex by a per-thread
    home the backend also has to publish -- and either can be broken alone.
    """
    _, _, claude_project = _claude_session_paths(workspace)
    _, codex_home = _codex_session_paths(workspace)
    for label, directory in (
        ("claude session", claude_project),
        ("codex home", codex_home / "sessions"),
    ):
        target = directory / _SESSION_PROBE_NAME
        try:
            # No mkdir: `wrap` created the chain on the host, and a recursive mkdir
            # from inside would walk up into the opaque $HOME and fail there
            # instead, reporting a denial that says nothing about this grant.
            code, output = await _run_jailed(
                ["/bin/sh", "-c", f"printf ok > {target}"], workspace
            )
        except (OSError, TimeoutError) as exc:
            raise SandboxBoundaryError(
                f"sandbox session preflight could not run: {exc}"
            ) from exc
        wrote = target.is_file()
        with contextlib.suppress(OSError):
            target.unlink()
        if code != 0 or not wrote:
            raise SandboxBoundaryError(
                f"sandbox preflight failed: a jailed process cannot write its "
                f"{label} directory ({directory}), so the agent would complete "
                "turns and silently lose its conversation and memory. "
                f"(rc={code}): {output.strip()[:400]}"
            )


async def verify_denials(workspace: Path | None = None) -> dict[str, str]:
    """Probe each expected deny under the live profile; return path -> outcome.

    macOS cannot report a seatbelt denial: a bare `deny` writes nothing to the
    unified log, `(with report)` is rejected for deny actions, and three separate
    log predicates over a real violation return nothing. Verified, not assumed.
    So the agent's own blocked reads are unobservable from this side, permanently.
    A broken profile or readable credential path is therefore a startup failure,
    not a warning after which autonomous work continues.

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
    # Concurrently: these are independent subprocesses, each with its own 15s
    # ceiling, and they sit on the daemon's startup path. Run in sequence the
    # worst case was a minute and a half of a daemon that had not begun serving.
    specs = _deny_probe_specs()
    # A scratch directory rather than the caller's cwd. These probes only read
    # paths well outside any workspace, and the Linux jail materialises write-deny
    # placeholders (.mcp.json, .vscode/) in whatever workspace it is handed -- so
    # defaulting to cwd would leave those in whichever directory the daemon
    # happened to start in, usually somebody's checkout.
    probe_workspace = workspace or _probe_workspace()
    outcomes = await asyncio.gather(
        *(_probe_deny(spec, probe_workspace) for spec in specs)
    )
    results: dict[str, str] = {
        spec: outcome
        for spec, outcome in zip(specs, outcomes, strict=True)
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
    if broken or leaked:
        details = ", ".join([*broken, *leaked])
        raise SandboxBoundaryError(
            "sandbox boundary self-test failed; refusing to start autonomous "
            f"work ({details})"
        )
    return results
