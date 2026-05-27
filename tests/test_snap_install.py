"""Phase 8 — snap auto-install consent flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


from claude_on_the_fly import snap_install


def test_already_installed_short_circuits() -> None:
    with patch.object(snap_install, "is_snap_installed", return_value=True):
        out = snap_install.ensure_snap_installed()
    assert out.installed is True


def test_non_tty_without_auto_yes_declines() -> None:
    with patch.object(snap_install, "is_snap_installed", return_value=False):
        out = snap_install.ensure_snap_installed(auto_yes=False, is_tty=False)
    assert out.installed is False
    assert "consent declined" in out.message
    assert "curl" in out.message


def test_tty_user_says_no_declines() -> None:
    with patch.object(snap_install, "is_snap_installed", return_value=False):
        out = snap_install.ensure_snap_installed(is_tty=True, input_fn=lambda: "n")
    assert out.installed is False
    assert "consent declined" in out.message


def test_tty_user_says_yes_installs_successfully() -> None:
    call_count = {"installed": 0}

    def fake_is_installed() -> bool:
        # Pre-install: missing. After installer runs: present.
        call_count["installed"] += 1
        return call_count["installed"] > 1

    fake_proc = SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_runner(*_args, **_kwargs):
        return fake_proc

    with (
        patch.object(snap_install, "is_snap_installed", side_effect=fake_is_installed),
        patch.object(snap_install.shutil, "which", return_value="/usr/bin/x"),
    ):
        out = snap_install.ensure_snap_installed(
            is_tty=True,
            input_fn=lambda: "y",
            runner=fake_runner,
        )
    assert out.installed is True


def test_installer_failure_surfaces_message() -> None:
    fake_proc = SimpleNamespace(returncode=2, stdout="", stderr="curl: 404")

    def fake_runner(*_args, **_kwargs):
        return fake_proc

    with (
        patch.object(snap_install, "is_snap_installed", return_value=False),
        patch.object(snap_install.shutil, "which", return_value="/usr/bin/x"),
    ):
        out = snap_install.ensure_snap_installed(
            is_tty=True,
            input_fn=lambda: "y",
            runner=fake_runner,
        )
    assert out.installed is False
    assert "installer failed" in out.message
    assert "curl: 404" in out.message


def test_installer_success_but_binary_still_missing_fails() -> None:
    fake_proc = SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_runner(*_args, **_kwargs):
        return fake_proc

    # Always missing — installer "succeeded" but binary not on PATH.
    with (
        patch.object(snap_install, "is_snap_installed", return_value=False),
        patch.object(snap_install.shutil, "which", return_value="/usr/bin/x"),
    ):
        out = snap_install.ensure_snap_installed(
            is_tty=True,
            input_fn=lambda: "y",
            runner=fake_runner,
        )
    assert out.installed is False
    assert "still not found on PATH" in out.message


def test_auto_yes_env_var_skips_prompt() -> None:
    """SYMPHONY_AUTO_INSTALL_SNAP=1 bypasses consent (useful for CI)."""
    call_count = {"i": 0}

    def fake_is_installed() -> bool:
        call_count["i"] += 1
        return call_count["i"] > 1

    fake_proc = SimpleNamespace(returncode=0, stdout="", stderr="")
    with (
        patch.dict("os.environ", {"SYMPHONY_AUTO_INSTALL_SNAP": "1"}, clear=False),
        patch.object(snap_install, "is_snap_installed", side_effect=fake_is_installed),
        patch.object(snap_install.shutil, "which", return_value="/usr/bin/x"),
    ):
        # input_fn must not be called (auto_yes resolves true from env)
        called = {"prompted": False}

        def fail_input() -> str:
            called["prompted"] = True
            return "n"

        out = snap_install.ensure_snap_installed(
            is_tty=False,
            input_fn=fail_input,
            runner=lambda *_a, **_k: fake_proc,
        )
    assert out.installed is True
    assert called["prompted"] is False


def test_run_installer_requires_curl() -> None:
    with patch.object(snap_install.shutil, "which", side_effect=lambda b: None):
        ok, msg = snap_install.run_installer()
    assert ok is False
    assert "curl" in msg


def test_prompt_consent_returns_false_on_eof() -> None:
    def boom() -> str:
        raise EOFError

    assert snap_install.prompt_consent(is_tty=True, input_fn=boom) is False
