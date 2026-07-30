"""Phase 8 — pty auto-install consent flow."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_on_the_fly import pty_install


def test_already_installed_short_circuits() -> None:
    with patch.object(pty_install, "is_pty_installed", return_value=True):
        out = pty_install.ensure_pty_installed()
    assert out.installed is True


def test_non_tty_without_auto_yes_declines() -> None:
    with patch.object(pty_install, "is_pty_installed", return_value=False):
        out = pty_install.ensure_pty_installed(auto_yes=False, is_tty=False)
    assert out.installed is False
    assert "consent declined" in out.message
    assert "curl" in out.message


def test_tty_user_says_no_declines() -> None:
    with patch.object(pty_install, "is_pty_installed", return_value=False):
        out = pty_install.ensure_pty_installed(is_tty=True, input_fn=lambda: "n")
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
        patch.object(pty_install, "is_pty_installed", side_effect=fake_is_installed),
        patch.object(pty_install.shutil, "which", return_value="/usr/bin/x"),
    ):
        out = pty_install.ensure_pty_installed(
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
        patch.object(pty_install, "is_pty_installed", return_value=False),
        patch.object(pty_install.shutil, "which", return_value="/usr/bin/x"),
    ):
        out = pty_install.ensure_pty_installed(
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
        patch.object(pty_install, "is_pty_installed", return_value=False),
        patch.object(pty_install.shutil, "which", return_value="/usr/bin/x"),
    ):
        out = pty_install.ensure_pty_installed(
            is_tty=True,
            input_fn=lambda: "y",
            runner=fake_runner,
        )
    assert out.installed is False
    assert "still not found on PATH" in out.message


def test_auto_yes_env_var_skips_prompt() -> None:
    """COTF_AUTO_INSTALL_PTY=1 bypasses consent (useful for CI)."""
    call_count = {"i": 0}

    def fake_is_installed() -> bool:
        call_count["i"] += 1
        return call_count["i"] > 1

    fake_proc = SimpleNamespace(returncode=0, stdout="", stderr="")
    with (
        patch.dict("os.environ", {"COTF_AUTO_INSTALL_PTY": "1"}, clear=False),
        patch.object(pty_install, "is_pty_installed", side_effect=fake_is_installed),
        patch.object(pty_install.shutil, "which", return_value="/usr/bin/x"),
    ):
        # input_fn must not be called (auto_yes resolves true from env)
        called = {"prompted": False}

        def fail_input() -> str:
            called["prompted"] = True
            return "n"

        out = pty_install.ensure_pty_installed(
            is_tty=False,
            input_fn=fail_input,
            runner=lambda *_a, **_k: fake_proc,
        )
    assert out.installed is True
    assert called["prompted"] is False


def test_run_installer_requires_curl() -> None:
    with patch.object(pty_install.shutil, "which", side_effect=lambda b: None):
        ok, msg = pty_install.run_installer()
    assert ok is False
    assert "curl" in msg


def test_prompt_consent_returns_false_on_eof() -> None:
    def boom() -> str:
        raise EOFError

    assert pty_install.prompt_consent(is_tty=True, input_fn=boom) is False


class TestHooksOnlyRefresh:
    """Re-splicing hooks on an install whose binary is fine but whose hook set
    predates PostCompact. The binary-missing gate cannot see that case."""

    def test_passes_the_no_statusline_switch(self):
        """The whole point: a full install run rewrites statusLine.command, and
        more than one tool vendors these shims — a daemon doing that every
        startup would take the key off whichever tool wired it last."""
        captured = {}

        def runner(cmd, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ok, _ = pty_install.refresh_hooks(runner=runner)

        assert ok is True
        assert captured["env"]["CLAUDE_PTY_NO_STATUSLINE"] == "1"

    def test_inherits_the_rest_of_the_environment(self, monkeypatch):
        """CLAUDE_CONFIG_DIR decides which settings.json install.sh writes, so
        dropping the ambient environment would splice into the wrong config."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
        captured = {}

        def runner(cmd, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        pty_install.refresh_hooks(runner=runner)

        assert captured["env"]["CLAUDE_CONFIG_DIR"] == "/somewhere/else"

    def test_reports_installer_failure_without_raising(self):
        def runner(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

        ok, message = pty_install.refresh_hooks(runner=runner)

        assert ok is False
        assert "boom" in message

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv(pty_install.AUTO_REFRESH_VAR, raising=False)
        assert pty_install.auto_refresh_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
    def test_opt_out_values(self, monkeypatch, value):
        monkeypatch.setenv(pty_install.AUTO_REFRESH_VAR, value)
        assert pty_install.auto_refresh_enabled() is False

    def test_full_install_still_rewrites_statusline(self):
        """The refresh is the only caller that opts out; a genuine first install
        must still wire the shim or pty has no sidecar at all."""
        captured = {}

        def runner(cmd, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        pty_install.run_installer(runner=runner)

        assert "CLAUDE_PTY_NO_STATUSLINE" not in captured["env"]


class TestIsPtyInstalled:
    def test_a_binary_on_path_is_enough(self, monkeypatch):
        monkeypatch.setattr(
            pty_install.shutil, "which", lambda _n: "/usr/local/bin/claude-pty"
        )
        assert pty_install.is_pty_installed() is True

    def test_the_project_install_location_also_counts(self, monkeypatch):
        """Matches resolve_pty_binary's own semantics, so the doctor and the
        preflight agree about what "installed" means."""
        from claude_on_the_fly.backends import claude as claude_mod

        monkeypatch.setattr(pty_install.shutil, "which", lambda _n: None)
        monkeypatch.setattr(
            claude_mod, "resolve_pty_binary", lambda: "/opt/pty/bin/claude-pty"
        )
        assert pty_install.is_pty_installed() is True

    def test_neither_means_not_installed(self, monkeypatch):
        from claude_on_the_fly.backends import claude as claude_mod

        monkeypatch.setattr(pty_install.shutil, "which", lambda _n: None)
        monkeypatch.setattr(claude_mod, "resolve_pty_binary", lambda: None)
        assert pty_install.is_pty_installed() is False


class TestStdinIsTty:
    def test_both_streams_must_be_terminals(self, monkeypatch):
        """The consent prompt writes to stderr and reads stdin, so one redirected
        stream is enough to make it unanswerable."""
        monkeypatch.setattr(
            pty_install.sys, "stdin", SimpleNamespace(isatty=lambda: True)
        )
        monkeypatch.setattr(
            pty_install.sys, "stderr", SimpleNamespace(isatty=lambda: False)
        )
        assert pty_install._stdin_is_tty() is False

    def test_two_terminals_is_interactive(self, monkeypatch):
        monkeypatch.setattr(
            pty_install.sys, "stdin", SimpleNamespace(isatty=lambda: True)
        )
        monkeypatch.setattr(
            pty_install.sys, "stderr", SimpleNamespace(isatty=lambda: True)
        )
        assert pty_install._stdin_is_tty() is True

    def test_a_stream_that_raises_is_treated_as_not_a_tty(self, monkeypatch):
        """A daemonized run can have a closed stdin, where isatty() itself raises."""

        def raises():
            raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(pty_install.sys, "stdin", SimpleNamespace(isatty=raises))
        assert pty_install._stdin_is_tty() is False


class TestInstallerFailures:
    def _runner(self, **result):
        return lambda *_a, **_kw: SimpleNamespace(**result)

    def test_an_installer_that_overruns_is_reported(self, monkeypatch):
        monkeypatch.setattr(pty_install.shutil, "which", lambda _n: "/bin/x")

        def times_out(*_a, **_kw):
            raise pty_install.subprocess.TimeoutExpired("curl", 180)

        ok, message = pty_install.run_installer(runner=times_out)
        assert ok is False
        assert "timed out after 180s" in message

    def test_a_nonzero_exit_carries_the_installers_stderr(self, monkeypatch):
        """That text is the only thing the operator can act on."""
        monkeypatch.setattr(pty_install.shutil, "which", lambda _n: "/bin/x")
        ok, message = pty_install.run_installer(
            runner=self._runner(returncode=1, stderr="404 not found")
        )
        assert ok is False
        assert "404 not found" in message

    def test_a_silent_failure_falls_back_to_the_exit_code(self, monkeypatch):
        monkeypatch.setattr(pty_install.shutil, "which", lambda _n: "/bin/x")
        ok, message = pty_install.run_installer(
            runner=self._runner(returncode=7, stderr="")
        )
        assert ok is False
        assert "exit 7" in message

    def test_a_clean_run_returns_what_it_did(self, monkeypatch):
        monkeypatch.setattr(pty_install.shutil, "which", lambda _n: "/bin/x")
        ok, message = pty_install.run_installer(
            runner=self._runner(returncode=0, stderr=""), what="installed claude-pty"
        )
        assert ok is True
        assert message == "installed claude-pty"


def test_an_import_error_reaching_the_backend_reads_as_not_installed(monkeypatch):
    """`is_pty_installed` runs on the preflight path, before the backend module is
    necessarily importable, and a raise there would be a startup crash rather than an
    install prompt."""
    import builtins

    monkeypatch.setattr(pty_install.shutil, "which", lambda _n: None)
    real_import = builtins.__import__

    def fail_backend(name, *args, **kwargs):
        if name == "claude_on_the_fly.backends.claude":
            raise ImportError("slack_bolt missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_backend)
    assert pty_install.is_pty_installed() is False


def test_a_missing_bash_stops_the_installer(monkeypatch):
    """The script is piped through bash, so its absence is a different fix from a
    missing curl and has to say which."""
    monkeypatch.setattr(
        pty_install.shutil,
        "which",
        lambda name: "/bin/curl" if name == "curl" else None,
    )

    def must_not_run(*_a, **_kw):
        raise AssertionError("shelled out without bash")

    ok, message = pty_install.run_installer(runner=must_not_run)
    assert ok is False
    assert "bash not on PATH" in message
