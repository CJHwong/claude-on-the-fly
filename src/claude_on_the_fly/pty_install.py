"""Phase 8 — first-run consent + auto-install for `claude-pty`.

Triggered from preflight when `CLAUDE_MODE=pty` and the binary is missing.
Non-TTY callers see a hard error with the manual install command; TTY
callers see a one-shot y/N prompt.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

INSTALL_URL = (
    "https://raw.githubusercontent.com/CJHwong/claude-interactive-p/main/install.sh"
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
) -> tuple[bool, str]:
    """Pipe the canonical install script through bash. Returns (ok, message).

    `runner` is injectable for tests so we can avoid actually shelling out.
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
        )
    except subprocess.TimeoutExpired:
        return False, "installer timed out after 180s"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return False, f"installer failed: {detail}"
    return True, "claude-pty installed"


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
        else os.environ.get("SYMPHONY_AUTO_INSTALL_PTY", "").lower()
        in {"1", "true", "yes"}
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
