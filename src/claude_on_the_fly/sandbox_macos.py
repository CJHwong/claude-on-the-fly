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
# Fixed loopback allow slots in the jail profile, since SBPL has no arrays. Four
# because each loopback grant is a separate parameter, so the profile has to
# declare a fixed number of slots. The services are the credential broker, the
# command broker, the CONNECT egress proxy, and -- when permissions mode is
# `ask` -- the approval service the backends ask about tool calls. A fifth
# service would need a fifth slot here and in both profiles.
_LOOPBACK_SLOTS = 4
# Runtime read slots in fs-deny-most.sb: agent binary dir, sys.prefix,
# sys.base_prefix, package dir.
_RUNTIME_SLOTS = 4
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _fs_base_profile() -> Path:
    """Filesystem base that jail.sb imports. `sandbox.fs: deny-most` selects
    fs-deny-most.sb; anything else keeps fs-allow-reads.sb (the default)."""
    if settings.get("COTF_SANDBOX_FS").lower() == "deny-most":
        return _DENY_MOST_PROFILE
    return _BASE_PROFILE


def _loopback_specs(ports: list[str]) -> tuple[str, str, str, str]:
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
    return specs[0], specs[1], specs[2], specs[3]


def jail_argv(
    argv: list[str],
    *,
    home: Path | str,
    data_dir: Path | str,
    project: Path | str,
    tmpdir: Path | str,
    base: Path,
    loopback: tuple[str, str, str, str],
    extra_paths: list[str],
    runtime_paths: list[str] | None = None,
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
    first, second, third, fourth = loopback
    params = [
        "-D",
        f"_HOME={home}",
        "-D",
        f"_DATA_DIR={data_dir}",
        "-D",
        f"_PROJECT_DIR={project}",
        "-D",
        f"_TMPDIR={tmpdir}",
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
    ]
    # fs-allow-reads.sb does not reference _EXTRA_*; only fs-deny-most.sb does,
    # so only pass them there. Pad unused slots with the project dir (a no-op).
    if base == _DENY_MOST_PROFILE:
        extra = [*extra_paths]
        extra += [str(project)] * (_MAX_EXTRA_PATHS - len(extra))
        for index, path in enumerate(extra, start=1):
            params += ["-D", f"_EXTRA_{index}={path}"]
        # Without these the profile cannot exec a backend or interpreter living
        # under the opaque $HOME, which is where npm globals and uv virtualenvs
        # normally are. Truncated rather than warned on: the caller supplies a
        # fixed, known set, unlike operator-supplied extra paths.
        runtime = [*(runtime_paths or [])][:_RUNTIME_SLOTS]
        runtime += [str(project)] * (_RUNTIME_SLOTS - len(runtime))
        for index, path in enumerate(runtime, start=1):
            params += ["-D", f"_RUNTIME_{index}={path}"]
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


def realpaths(workspace: Path, data_dir: Path) -> dict[str, str]:
    """The four resolved directory parameters every profile is written against."""
    return {
        "home": os.path.realpath(Path.home()),
        "data_dir": os.path.realpath(data_dir),
        "project": os.path.realpath(workspace),
        "tmpdir": os.path.realpath(os.environ.get("TMPDIR", "/tmp")),
    }
