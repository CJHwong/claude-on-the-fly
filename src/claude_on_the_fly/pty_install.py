"""Phase 8 — first-run consent + auto-install for `claude-pty`.

Triggered from preflight when `CLAUDE_MODE=pty` and the binary is missing.
Non-TTY callers see a hard error with the manual install command; TTY
callers see a one-shot y/N prompt.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from claude_on_the_fly import settings

logger = logging.getLogger(__name__)

# Immutable ref, reviewed when this dependency was updated. Never execute a
# mutable branch URL from daemon preflight: a repository force-push must not turn
# a routine hook refresh into arbitrary code execution.
PTY_INSTALL_COMMIT = "323796ca5052127352d00a3e5c68eb403001a8b8"
INSTALL_URL = (
    "https://raw.githubusercontent.com/CJHwong/claude-interactive-p/"
    f"{PTY_INSTALL_COMMIT}/install.sh"
)
MANUAL_HINT = f"curl -fsSL {INSTALL_URL} | bash"


@dataclass(frozen=True)
class InstallOutcome:
    installed: bool
    message: str


def is_pty_installed() -> bool:
    """True iff claude-pty resolves on PATH OR via the standard project
    install location (matches the existing resolve_pty_binary semantics)."""
    if shutil.which("claude-pty"):
        return True
    try:
        from claude_on_the_fly.backends.claude import resolve_pty_binary
    except ImportError:
        return False
    return resolve_pty_binary() is not None


def _stdin_is_tty() -> bool:
    """Best-effort TTY detection. Returns False under daemonized / piped runs."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except Exception:
        return False


def prompt_consent(
    *,
    auto_yes: bool = False,
    is_tty: bool | None = None,
    input_fn=input,
) -> bool:
    """Ask the user whether to install. Returns True to proceed.

    `auto_yes` short-circuits the prompt (set via env / CLI). `input_fn` is
    injectable for tests. `is_tty` overrides the auto-detect so tests can
    simulate both branches without touching real stdin/stderr.
    """
    if auto_yes:
        return True
    tty = _stdin_is_tty() if is_tty is None else is_tty
    if not tty:
        return False
    sys.stderr.write(
        "claude-pty is not installed but CLAUDE_MODE=pty. "
        f"Install via curl from {INSTALL_URL}? [y/N] "
    )
    sys.stderr.flush()
    try:
        answer = (input_fn() or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def run_installer(
    *,
    runner=subprocess.run,
    extra_env: Mapping[str, str] | None = None,
    what: str = "claude-pty installed",
) -> tuple[bool, str]:
    """Pipe the canonical install script through bash. Returns (ok, message).

    `runner` is injectable for tests so we can avoid actually shelling out.
    `extra_env` layers over the current environment, which is how the
    hooks-only refresh reaches install.sh's `CLAUDE_PTY_NO_STATUSLINE` switch.
    """
    if not shutil.which("curl"):
        return False, "curl not on PATH"
    if not shutil.which("bash"):
        return False, "bash not on PATH"
    try:
        proc = runner(
            f"curl -fsSL {INSTALL_URL} | bash",
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, **(extra_env or {})},
        )
    except subprocess.TimeoutExpired:
        return False, "installer timed out after 180s"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return False, f"installer failed: {detail}"
    return True, what


# install.sh switches for a hooks-only run. NO_STATUSLINE is the important one:
# a full run rewrites `statusLine.command` to its own copy, and more than one
# tool vendors these shims, so a daemon doing that at every startup would take
# the key off whichever tool wired it last — and that tool would take it back on
# its own next start. Splicing only the hooks leaves ownership where it is.
# YES skips the confirm prompt, which reads /dev/tty and so would otherwise be
# skipped anyway under a daemon; setting it makes that explicit rather than
# incidental.
HOOKS_ONLY_ENV = {"CLAUDE_PTY_NO_STATUSLINE": "1", "CLAUDE_PTY_YES": "1"}
# Set to 0/false/no to stop preflight touching settings.json. On by default:
# this only runs when the hook set is already incomplete, i.e. when pty
# compaction is already broken.
AUTO_REFRESH_VAR = "COTF_PTY_AUTO_REFRESH"


def auto_refresh_enabled() -> bool:
    """Whether preflight may re-splice pty's hooks when they're incomplete."""
    return settings.get(AUTO_REFRESH_VAR, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def refresh_hooks(*, runner=subprocess.run) -> tuple[bool, str]:
    """Re-run the installer for its hooks only, leaving statusLine alone.

    For an install whose binary is fine but whose hook set predates a hook we
    need (currently PostCompact, without which a pty compaction hangs). Never
    raises and never blocks: the caller logs the outcome and carries on, because
    ordinary turns work either way.
    """
    return run_installer(
        runner=runner,
        extra_env=HOOKS_ONLY_ENV,
        what="claude-pty hooks re-spliced (statusLine left untouched)",
    )


def ensure_pty_installed(
    *,
    auto_yes: bool | None = None,
    is_tty: bool | None = None,
    input_fn=input,
    runner=subprocess.run,
) -> InstallOutcome:
    """End-to-end: check, prompt, install, re-check.

    Returns the outcome — caller (preflight) raises SystemExit on failure
    with the appropriate hint.
    """
    if is_pty_installed():
        return InstallOutcome(installed=True, message="claude-pty already installed")

    auto_yes_resolved = (
        auto_yes
        if auto_yes is not None
        else settings.get("COTF_AUTO_INSTALL_PTY").lower() in {"1", "true", "yes"}
    )

    consented = prompt_consent(
        auto_yes=auto_yes_resolved, is_tty=is_tty, input_fn=input_fn
    )
    if not consented:
        return InstallOutcome(
            installed=False,
            message=(
                f"claude-pty not installed and consent declined. "
                f"Run manually: {MANUAL_HINT}"
            ),
        )

    ok, msg = run_installer(runner=runner)
    if not ok:
        return InstallOutcome(installed=False, message=msg)

    if not is_pty_installed():
        return InstallOutcome(
            installed=False,
            message=(
                "installer reported success but claude-pty still not found on PATH. "
                f"Run manually: {MANUAL_HINT}"
            ),
        )
    return InstallOutcome(installed=True, message=msg)


# claude records per-directory workspace trust in its own project-state file.
# Location mirrors CLAUDE_CONFIG_DIR when set, but the default is *home root*,
# not inside ~/.claude — verified against claude 2.1.220.
_STATE_FILENAME = ".claude.json"
_TRUST_KEY = "hasTrustDialogAccepted"
# Bounded because the only reason to retry is another writer landing between the
# read and the replace. If it happens three times running, something other than
# an ordinary interleave is going on and silently looping does not help.
_TRUST_WRITE_ATTEMPTS = 3


def claude_state_file(env: Mapping[str, str] | None = None) -> Path:
    """claude's project-state file, which holds the workspace trust store.

    Read through `envfile` rather than `os.environ`: the daemon spawning claude
    receives `DATA_DIR/.env`, so a deployment that sets `CLAUDE_CONFIG_DIR`
    there must not have this resolve against the viewing shell instead.
    """
    from claude_on_the_fly import envfile

    resolved = envfile.daemon_environment() if env is None else env
    config_dir = (resolved.get("CLAUDE_CONFIG_DIR") or "").strip()
    if config_dir:
        return Path(config_dir) / _STATE_FILENAME
    # Not `claude_config_dir() / _STATE_FILENAME`: with the variable unset the
    # file sits beside ~/.claude, not in it.
    return Path.home() / _STATE_FILENAME


def cotf_owns_workspace(workspace: Path) -> bool:
    """Whether `workspace` is one cotf created under `DATA_DIR/workspaces`.

    The bound on auto-trust. cotf can vouch for a directory it made itself; it
    has no business marking an operator's own checkout as trusted because a
    session happened to be pointed at it.
    """
    from claude_on_the_fly.agent import DATA_DIR

    try:
        workspace.resolve().relative_to((DATA_DIR / "workspaces").resolve())
    except (OSError, ValueError):
        return False
    return True


# Workspaces confirmed trusted in this process. The state file reaches megabytes
# on a machine with many projects, and a daemon runs many turns against the same
# workspace; re-reading it every turn buys nothing. Only ever populated after a
# positive result, so a miss costs one read rather than skipping the check.
_trusted_workspaces: set[str] = set()


def ensure_workspace_trusted(workspace: Path) -> bool:
    """Trust a cotf-owned workspace if claude has not seen it before.

    Called on the pty spawn path, where an untrusted directory does not fail --
    it stops on a dialog and burns the whole turn timeout.
    """
    key = str(workspace)
    if key in _trusted_workspaces:
        return True
    if not cotf_owns_workspace(workspace):
        logger.debug("pty: %s is not a cotf workspace; not touching trust", key)
        return False
    if trust_workspace(workspace):
        _trusted_workspaces.add(key)
        return True
    return False


def workspace_is_trusted(workspace: Path, env: Mapping[str, str] | None = None) -> bool:
    """Whether claude would skip its trust dialog for `workspace`."""
    try:
        data = json.loads(claude_state_file(env).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    entry = (data.get("projects") or {}).get(str(workspace))
    return bool(isinstance(entry, dict) and entry.get(_TRUST_KEY))


def trust_workspace(workspace: Path, env: Mapping[str, str] | None = None) -> bool:
    """Record `workspace` as trusted so an interactive claude does not stop.

    Why this exists: claude skips the trust dialog only in non-interactive mode
    (`-p`, or a non-TTY stdout). `claude-pty` exists to give claude a real TTY,
    so every pty turn in a directory claude has not seen before stops on
    "Is this a project you created or one you trust?" and waits until the turn
    times out. There is no flag for it and no CLI command that grants trust, so
    the state file is the only lever.

    It grants nothing: cotf created this workspace and already runs claude with
    `--permission-mode bypassPermissions`. The seatbelt jail and the approval
    gate are what bound the agent, and neither is affected.

    Writes only this one key, and re-reads if the file changed underneath —
    claude rewrites it on its own schedule (session metrics, costs), and a
    read-modify-write that ignored that would discard whatever it just recorded.
    Returns whether the workspace ends up trusted.
    """
    path = claude_state_file(env)
    target = str(workspace)
    for _attempt in range(_TRUST_WRITE_ATTEMPTS):
        try:
            stat_before = path.stat().st_mtime_ns
            data = json.loads(path.read_text())
        except FileNotFoundError:
            stat_before = None
            data = {}
        except (OSError, json.JSONDecodeError) as exc:
            # Never rewrite a state file we could not parse: claude owns it, and
            # replacing it with our own idea of its contents would lose the lot.
            logger.warning("pty: cannot read %s to trust %s (%s)", path, target, exc)
            return False
        if not isinstance(data, dict):
            logger.warning("pty: %s is not a JSON object; leaving it alone", path)
            return False
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            logger.warning("pty: %s has no usable projects map", path)
            return False
        entry = projects.setdefault(target, {})
        if not isinstance(entry, dict):
            logger.warning("pty: %s has an unusable entry for %s", path, target)
            return False
        if entry.get(_TRUST_KEY):
            return True
        entry[_TRUST_KEY] = True
        try:
            if stat_before is not None and path.stat().st_mtime_ns != stat_before:
                continue  # claude wrote while we were reading; start over
            _atomic_write_json(path, data)
        except OSError as exc:
            logger.warning("pty: could not trust %s in %s (%s)", target, path, exc)
            return False
        logger.info("pty: trusted %s for claude (%s)", target, path)
        return True
    logger.warning(
        "pty: gave up trusting %s after %d attempts; %s keeps changing",
        target,
        _TRUST_WRITE_ATTEMPTS,
        path,
    )
    return False


def _atomic_write_json(path: Path, data: dict) -> None:
    """Replace `path` with `data`, never leaving a half-written state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
