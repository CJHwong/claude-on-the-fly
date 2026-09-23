"""macOS half of `sandbox.mode: jail`, built on seatbelt.

The counterpart to sandbox_linux, and the split between them is the same one:
policy lives in sandbox.py, mechanism lives here. What "mechanism" means differs
because the two systems put the contract in different places.

On Linux the contract is a list of mounts, so sandbox_linux takes path lists and
returns argv. Here the contract is the vendored SBPL in `seatbelt/`, and the
mechanism is choosing a profile and parameterising it: every rule in those files
is written against a `-D` parameter, so nothing is enforced until the values are
resolved and passed. The two functions that resolve them read settings, which
sandbox_linux deliberately does not -- that asymmetry is real and is the reason
this module is not simply "the Linux one with different flags".

The SBPL fixed-slot constants live here too. `_MAX_EXTRA_PATHS` and
`_LOOPBACK_SLOTS` exist only because SBPL has no arrays; they describe seatbelt,
not the sandbox contract, and a mount namespace has no equivalent limit.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from claude_on_the_fly import settings

logger = logging.getLogger(__name__)

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
# Fixed loopback allow slots in the jail profile, since SBPL has no arrays. Five
# because each loopback grant is a separate parameter, so the profile has to
# declare a fixed number of slots. The services are the credential broker, the
# command broker, the CONNECT egress proxy, the approval service when
# permissions mode is `ask`, and the ollama server for an ollama turn. A sixth
# service would need a sixth slot here and in jail.sb.
_LOOPBACK_SLOTS = 5
# Runtime read slots in fs-deny-most.sb: the launcher's directory, the resolved
# binary's directory, sys.prefix, sys.base_prefix, package dir. Five because a
# launcher and the code it runs need not share a directory: `claude` is a symlink
# in ~/.local/bin pointing into ~/.local/share/claude/versions/<v>.
# Sixteen. Five silently truncated the list, and a dropped grant reads as a
# missing binary or a dead interpreter rather than as a denial, so the ceiling
# has to clear the worst case rather than the common one. Measured on the widest
# real argv, `claude-pty`, which contributes itself plus the `claude` and `tmux`
# it execs, each as written and as resolved, each with the `lib/` beside it, plus
# sys.prefix, sys.base_prefix and the package directory: eleven. The rest is
# headroom for a deeper layout, and the overflow still warns.
_RUNTIME_SLOTS = 16
# Metadata slots for the directories between $HOME and the project dir. A read
# grant on the project subpath says nothing about its parents, and an opaque
# $HOME denies even stat() on them, which breaks any tool that canonicalizes its
# cwd: git's repository discovery realpath()s every component, so `git init`,
# `git clone` and `git status` inside the workspace all died with "Operation not
# permitted" on the home directory (measured on macOS 26 with the stock profile).
# Eight covers `$HOME/.claude-on-the-fly/workspaces/<platform>/<kind>/<name>`
# with room for a data dir a few levels deeper; a longer chain is truncated from
# the top and logged, since every link is needed for the walk to succeed.
_ANCESTOR_SLOTS = 8
# Read slots for what the operator's codex home links *out* to. A grant on that
# home covers the links themselves and nothing behind them, because seatbelt
# matches the path the kernel resolves, so an entry symlinked elsewhere under the
# opaque $HOME is unreadable while the profile still claims to grant it. The
# Linux jail has always mounted these targets read-only; this is the macOS half
# of the same grant. Measured on a real home where `~/.codex/agents` points at
# `~/.agents/agents`: codex exited 1 with "Operation not permitted (os error 1)"
# and the kernel logged `deny(1) file-read-data /Users/<user>/.agents/agents`.
# Eight because the list is collapsed to its shortest roots first, which took a
# home with 54 link targets down to 3; the rest is headroom, and an overflow
# warns and names what it dropped.
_CODEX_LINK_SLOTS = 8

# The data dir's `cron.yaml` and `config.yaml`, when either is a link into a
# write grant. One slot each, so this never overflows.
_PROTECT_SLOTS = 2
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _fs_base_profile() -> Path:
    """Filesystem base that jail.sb imports. `sandbox.fs: deny-most` selects
    fs-deny-most.sb; anything else keeps fs-allow-reads.sb (the default)."""
    if settings.get("COTF_SANDBOX_FS").lower() == "deny-most":
        return _DENY_MOST_PROFILE
    return _BASE_PROFILE


def _loopback_specs(ports: list[str]) -> tuple[str, str, str, str, str]:
    """The remote-ip values for the jail's loopback allows, one per slot.

    Narrows to just the local services the agent was handed when
    `sandbox.broker_only_loopback` is set, closing the arbitrary-local-sink
    path. Every slot is always filled because SBPL has no arrays: spare slots
    repeat the first port, which is a harmless duplicate allow. If no port is
    known at all, loopback stays open rather than locking the agent out of a
    service it needs.

    A port past _LOOPBACK_SLOTS would be silently unreachable, so that case warns
    loudly instead -- the same fixed-slot trade as `sandbox.extra_paths`.
    """
    if settings.get("COTF_SANDBOX_BROKER_ONLY_LOOPBACK").lower() not in _TRUTHY:
        return (_DEFAULT_LOOPBACK,) * _LOOPBACK_SLOTS  # ty: ignore[invalid-return-type]
    if not ports:
        logger.warning(
            "sandbox.broker_only_loopback set but no broker base-url, "
            "HTTPS_PROXY, COTF_CMD_ENDPOINT, or COTF_APPROVE_URL in env; "
            "leaving loopback open"
        )
        return (_DEFAULT_LOOPBACK,) * _LOOPBACK_SLOTS  # ty: ignore[invalid-return-type]
    if len(ports) > _LOOPBACK_SLOTS:
        logger.warning(
            "%d loopback services but only %d profile slots; %s would be "
            "unreachable. Turn off sandbox.broker_only_loopback or add a slot.",
            len(ports),
            _LOOPBACK_SLOTS,
            ports[_LOOPBACK_SLOTS:],
        )
    specs = [f"localhost:{port}" for port in ports[:_LOOPBACK_SLOTS]]
    specs += [specs[0]] * (_LOOPBACK_SLOTS - len(specs))
    return specs[0], specs[1], specs[2], specs[3], specs[4]


def jail_argv(
    argv: list[str],
    *,
    home: Path | str,
    data_dir: Path | str,
    project: Path | str,
    tmpdir: Path | str,
    claude_config: Path | str,
    claude_projects: Path | str,
    claude_project: Path | str,
    codex_sessions: Path | str,
    codex_home: Path | str,
    codex_operator_home: Path | str,
    pane_socket: Path | str,
    base: Path,
    loopback: tuple[str, str, str, str, str],
    extra_paths: list[str],
    write_paths: list[str] | None = None,
    protect_paths: list[str] | None = None,
    unprotect_paths: list[str] | None = None,
    codex_link_paths: list[str] | None = None,
    runtime_paths: list[str] | None = None,
    ancestor_paths: list[str] | None = None,
    profile: Path | None = None,
    sandbox_exec: str = "sandbox-exec",
) -> list[str]:
    """Wrap `argv` in the vendored seatbelt jail. Pure: no settings reads.

    Every path parameter is realpath'd by the caller and must stay that way.
    Seatbelt matches the resolved path, so an unresolved param silently matches
    nothing: on any host whose home is behind a symlink (network homes, a
    relocated macOS home, `/home/x -> /System/Volumes/Data/home/x`) every
    credential deny in the base profile would no-op while the profile still
    loaded and the log still said "jailed". The write grants under `$HOME` would
    fail the same way, which is what makes this a correctness bug and not only a
    leak. The same contract applies to `_DATA_DIR`, whose rules keep memory
    reachable and keep another daemon's `.env` out.
    """
    # Passed in rather than read from this module's global, so the caller owns
    # which profile is loaded and there is no module-level name to patch. A
    # re-exported constant is a copy: rebinding it in the calling module would
    # leave this one pointing at the original and the substitution would silently
    # not happen.
    profile = profile or _JAIL_PROFILE
    first, second, third, fourth, fifth = loopback
    params = [
        "-D",
        f"_HOME={home}",
        "-D",
        f"_DATA_DIR={data_dir}",
        "-D",
        f"_PROJECT_DIR={project}",
        "-D",
        f"_TMPDIR={tmpdir}",
        # Both profiles reference these, unlike _EXTRA_* below, so they are always
        # passed. A missing -D makes sandbox-exec refuse the profile outright,
        # which is the failure mode worth having: the alternative is a jail that
        # loads with the session grant silently absent.
        "-D",
        f"_CLAUDE_CONFIG={claude_config}",
        "-D",
        f"_CLAUDE_PROJECTS={claude_projects}",
        "-D",
        f"_CLAUDE_PROJECT={claude_project}",
        "-D",
        f"_CODEX_SESSIONS={codex_sessions}",
        "-D",
        f"_CODEX_HOME={codex_home}",
        "-D",
        f"_CODEX_OPERATOR_HOME={codex_operator_home}",
        "-D",
        f"_PANE_SOCKET={pane_socket}",
        "-D",
        f"_BASE={base}",
        "-D",
        f"_LOOPBACK={first}",
        "-D",
        f"_LOOPBACK_ALT={second}",
        "-D",
        f"_LOOPBACK_ALT2={third}",
        "-D",
        f"_LOOPBACK_ALT3={fourth}",
        "-D",
        f"_LOOPBACK_ALT4={fifth}",
    ]
    # fs-allow-reads.sb does not reference _EXTRA_*; only fs-deny-most.sb does,
    # so only pass them there. Pad unused slots with the project dir (a no-op).
    if base == _DENY_MOST_PROFILE:
        extra = [*extra_paths]
        extra += [str(project)] * (_MAX_EXTRA_PATHS - len(extra))
        for index, path in enumerate(extra, start=1):
            params += ["-D", f"_EXTRA_{index}={path}"]
        # Operator write grants (COTF_SANDBOX_WRITE_PATHS). Same fixed-slot trade
        # as _EXTRA_*, but padded with a name under the project rather than the
        # project itself, and not with a real directory either. Both of the
        # obvious pads are wrong, and each was measured wrong:
        #
        #   _PROJECT_DIR, which every read slot pads with, is a *write* allow here
        #   sitting below the project write denies, so it re-opens `.git/hooks`,
        #   `.mcp.json`, `.vscode` and the shell rc files. That is the
        #   `_CODEX_HOME` bug in docs/agent/security-findings.md a second time;
        #   tests/test_sandbox_parity.py fails on all seven denies.
        #
        #   _TMPDIR looks inert because it is already writable, but the slots now
        #   carry a scoped `.env` write deny as well as an allow, and that deny
        #   then applies to the whole temp directory. Measured: creating a `.env`
        #   in a workspace under TMPDIR was refused.
        #
        # A path that does not exist is inert for both halves, and keeping it
        # under the project means it is inside an already-writable tree if the
        # agent ever creates it.
        writes = [*(write_paths or [])]
        unused = str(Path(project) / ".cotf-unused-write-slot")
        writes += [unused] * (_MAX_EXTRA_PATHS - len(writes))
        for index, path in enumerate(writes, start=1):
            params += ["-D", f"_WRITE_{index}={path}"]
        # Unprotect pads with the same inert name. `_UNPROTECT_*` are write allows after
        # every deny, so a real directory there would be a grant nobody made.
        # Capped like the write slots they re-open a part of.
        # The protect pad is off the project: the profile denies its ancestors
        # as nodes too, and every ancestor of `/x` is `/`, already unwritable.
        protects = [*(protect_paths or [])]
        protects += ["/.cotf-unused-protect-slot"] * (_PROTECT_SLOTS - len(protects))
        for index, path in enumerate(protects, start=1):
            params += ["-D", f"_PROTECT_{index}={path}"]
        unprotects = [*(unprotect_paths or [])][:_MAX_EXTRA_PATHS]
        unprotects += [unused] * (_MAX_EXTRA_PATHS - len(unprotects))
        for index, path in enumerate(unprotects, start=1):
            params += ["-D", f"_UNPROTECT_{index}={path}"]
        # Where the operator's codex home links out to. Caller-filtered and
        # caller-collapsed, so a full list here is a real layout rather than
        # noise, and dropping one hides an instruction file the operator
        # believes is in force.
        links = [*(codex_link_paths or [])]
        if len(links) > _CODEX_LINK_SLOTS:
            logger.warning(
                "sandbox: the codex home links out to %d places but there are "
                "only %d slots; dropping %s. codex will report those as missing "
                "rather than as denied. Name them in sandbox.extra_paths",
                len(links),
                _CODEX_LINK_SLOTS,
                links[_CODEX_LINK_SLOTS:],
            )
        links = links[:_CODEX_LINK_SLOTS]
        links += [str(project)] * (_CODEX_LINK_SLOTS - len(links))
        for index, path in enumerate(links, start=1):
            params += ["-D", f"_CODEX_LINK_{index}={path}"]
        # Without these the profile cannot exec a backend or interpreter living
        # under the opaque $HOME, which is where npm globals and uv virtualenvs
        # normally are. The set is caller-supplied and fixed, unlike operator
        # extra paths, but it is not fixed in *size*: a symlinked install
        # contributes two entries where a plain one contributes one. Silence here
        # cost a day, because a dropped grant surfaces as a dead interpreter
        # rather than as a denial.
        if len(runtime_paths or []) > _RUNTIME_SLOTS:
            logger.warning(
                "sandbox: %d runtime paths but only %d slots; dropping %s. The "
                "backend or its interpreter may fail to start with an error that "
                "does not name the sandbox",
                len(runtime_paths or []),
                _RUNTIME_SLOTS,
                [str(path) for path in (runtime_paths or [])[_RUNTIME_SLOTS:]],
            )
        runtime = [*(runtime_paths or [])][:_RUNTIME_SLOTS]
        runtime += [str(project)] * (_RUNTIME_SLOTS - len(runtime))
        for index, path in enumerate(runtime, start=1):
            params += ["-D", f"_RUNTIME_{index}={path}"]
        ancestors = [*(ancestor_paths or [])]
        if len(ancestors) > _ANCESTOR_SLOTS:
            logger.warning(
                "sandbox: the project dir sits %d levels below the home; only the "
                "deepest %d get a metadata grant, so path resolution inside the "
                "workspace may fail",
                len(ancestors),
                _ANCESTOR_SLOTS,
            )
            ancestors = ancestors[-_ANCESTOR_SLOTS:]
        ancestors += [str(project)] * (_ANCESTOR_SLOTS - len(ancestors))
        for index, path in enumerate(ancestors, start=1):
            params += ["-D", f"_ANCESTOR_{index}={path}"]
    # The one positive record that the jail was applied. Without it a run with an
    # unset sandbox mode produces a log indistinguishable from a jailed one: both
    # are simply free of denials, and no denials also reads as success.
    logger.info(
        "sandbox: jailed %s (fs=%s, loopback=%s, project=%s)",
        Path(argv[0]).name,
        base.name,
        [first, second, third],
        project,
    )
    logger.debug("sandbox: seatbelt params %s", params)
    return [sandbox_exec, "-f", str(profile), *params, *argv]


def home_ancestors(project: str, home: str) -> list[str]:
    """The directories from `home` down to the parent of `project`, both resolved.

    Empty when the project is not under the home: reads outside `$HOME` are
    allowed by the profile already, so there is nothing to re-grant. The project
    itself is excluded because its subpath grant covers it.
    """
    project_path, home_path = Path(project), Path(home)
    if not project_path.is_relative_to(home_path) or project_path == home_path:
        return []
    chain = [home_path]
    for part in project_path.relative_to(home_path).parts[:-1]:
        chain.append(chain[-1] / part)
    return [str(directory) for directory in chain]


def realpaths(workspace: Path, data_dir: Path) -> dict[str, str]:
    """The four resolved directory parameters every profile is written against."""
    return {
        "home": os.path.realpath(Path.home()),
        "data_dir": os.path.realpath(data_dir),
        "project": os.path.realpath(workspace),
        "tmpdir": os.path.realpath(os.environ.get("TMPDIR", "/tmp")),
    }
