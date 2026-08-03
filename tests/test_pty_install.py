"""Phase 8 — pty auto-install consent flow."""

from __future__ import annotations

import json
import stat
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Workspace trust
# ---------------------------------------------------------------------------


class TestClaudeStateFileLocation:
    """Verified against claude 2.1.220: the variable moves the file, but with it
    unset the file sits beside ~/.claude rather than inside it."""

    def test_it_follows_claude_config_dir_when_set(self, tmp_path, monkeypatch):
        env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}
        assert pty_install.claude_state_file(env) == tmp_path / "cfg" / ".claude.json"

    def test_it_defaults_to_home_root_not_the_claude_directory(self, monkeypatch):
        assert pty_install.claude_state_file({}) == Path.home() / ".claude.json"

    def test_it_reads_the_env_file_not_the_viewing_shell(self, tmp_path, monkeypatch):
        """Same split-brain as the session-log lookup: the daemon that spawns
        claude gets DATA_DIR/.env, so this must resolve the same way."""
        from claude_on_the_fly import envfile

        env_file = tmp_path / ".env"
        env_file.write_text(f"CLAUDE_CONFIG_DIR={tmp_path / 'from-file'}\n")
        monkeypatch.setattr(envfile, "default_env_file", lambda: env_file)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "from-shell"))

        assert pty_install.claude_state_file() == (
            tmp_path / "from-file" / ".claude.json"
        )


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """A claude project-state file this test owns."""
    path = tmp_path / ".claude.json"
    monkeypatch.setattr(pty_install, "claude_state_file", lambda env=None: path)
    monkeypatch.setattr(pty_install, "_trusted_workspaces", set())
    return path


class TestTrustWorkspace:
    def test_it_records_trust_for_a_workspace_claude_has_not_seen(
        self, state_file, tmp_path
    ):
        state_file.write_text(json.dumps({"projects": {}}))
        workspace = tmp_path / "ws"

        assert pty_install.trust_workspace(workspace) is True

        data = json.loads(state_file.read_text())
        assert data["projects"][str(workspace)]["hasTrustDialogAccepted"] is True

    def test_it_preserves_everything_claude_already_wrote(self, state_file, tmp_path):
        """The file is claude's. Trust is one key inside it, not a file we own."""
        workspace = tmp_path / "ws"
        state_file.write_text(
            json.dumps(
                {
                    "userID": "abc123",
                    "projects": {
                        "/some/other/project": {"lastCost": 42},
                        str(workspace): {"lastSessionId": "keep-me"},
                    },
                }
            )
        )

        pty_install.trust_workspace(workspace)

        data = json.loads(state_file.read_text())
        assert data["userID"] == "abc123"
        assert data["projects"]["/some/other/project"] == {"lastCost": 42}
        assert data["projects"][str(workspace)]["lastSessionId"] == "keep-me"
        assert data["projects"][str(workspace)]["hasTrustDialogAccepted"] is True

    def test_an_already_trusted_workspace_is_not_rewritten(self, state_file, tmp_path):
        workspace = tmp_path / "ws"
        state_file.write_text(
            json.dumps({"projects": {str(workspace): {"hasTrustDialogAccepted": True}}})
        )
        before = state_file.stat().st_mtime_ns

        assert pty_install.trust_workspace(workspace) is True
        assert state_file.stat().st_mtime_ns == before

    def test_a_missing_state_file_is_created(self, state_file, tmp_path):
        workspace = tmp_path / "ws"
        assert pty_install.trust_workspace(workspace) is True
        data = json.loads(state_file.read_text())
        assert data["projects"][str(workspace)]["hasTrustDialogAccepted"] is True

    def test_the_written_file_is_owner_only(self, state_file, tmp_path):
        pty_install.trust_workspace(tmp_path / "ws")
        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600

    @pytest.mark.parametrize(
        ("content", "why"),
        [
            pytest.param("{not json", "unparseable", id="unparseable"),
            pytest.param('"a string"', "not an object", id="scalar"),
            pytest.param(
                '{"projects": []}', "projects is not a map", id="bad-projects"
            ),
            pytest.param(
                '{"projects": {"WS": "not a dict"}}',
                "entry is not a map",
                id="bad-entry",
            ),
        ],
    )
    def test_a_state_file_it_cannot_understand_is_left_untouched(
        self, state_file, tmp_path, content, why
    ):
        """Rewriting a file claude owns, from a parse we know failed, would
        replace all of it with our own small idea of its contents."""
        workspace = tmp_path / "ws"
        state_file.write_text(content.replace("WS", str(workspace)))
        before = state_file.read_text()

        assert pty_install.trust_workspace(workspace) is False, why
        assert state_file.read_text() == before

    def test_a_write_that_fails_reports_it_and_leaves_no_temp_file(
        self, state_file, tmp_path, monkeypatch, caplog
    ):
        state_file.write_text(json.dumps({"projects": {}}))

        def boom(_src, _dst):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(pty_install.os, "replace", boom)
        with caplog.at_level("WARNING"):
            assert pty_install.trust_workspace(tmp_path / "ws") is False
        assert "could not trust" in caplog.text
        assert [p.name for p in tmp_path.iterdir()] == [".claude.json"]

    def test_a_concurrent_claude_write_is_re_read_not_clobbered(
        self, state_file, tmp_path
    ):
        """claude rewrites this file on its own schedule. A read-modify-write
        that ignored that would discard whatever it recorded in between."""
        workspace = tmp_path / "ws"
        state_file.write_text(json.dumps({"projects": {}}))
        writes = {"n": 0}
        real_stat = Path.stat

        def claude_writes_once(self, **kwargs):
            result = real_stat(self, **kwargs)
            if self == state_file and writes["n"] == 0:
                writes["n"] = 1
                # Land a change between our read and our replace.
                state_file.write_text(
                    json.dumps({"projects": {"/elsewhere": {"lastCost": 7}}})
                )
            return result

        Path.stat = claude_writes_once
        try:
            assert pty_install.trust_workspace(workspace) is True
        finally:
            Path.stat = real_stat

        data = json.loads(state_file.read_text())
        assert data["projects"]["/elsewhere"] == {"lastCost": 7}, "claude's write lost"
        assert data["projects"][str(workspace)]["hasTrustDialogAccepted"] is True

    def test_it_gives_up_rather_than_spinning_on_a_file_that_never_settles(
        self, state_file, tmp_path, monkeypatch, caplog
    ):
        workspace = tmp_path / "ws"
        state_file.write_text(json.dumps({"projects": {}}))
        real_stat = Path.stat
        bumps = {"n": 0}

        def always_changing(self, **kwargs):
            result = real_stat(self, **kwargs)
            if self == state_file:
                bumps["n"] += 1
                state_file.write_text(json.dumps({"projects": {}, "n": bumps["n"]}))
            return result

        monkeypatch.setattr(Path, "stat", always_changing)
        with caplog.at_level("WARNING"):
            assert pty_install.trust_workspace(workspace) is False
        assert "gave up trusting" in caplog.text


class TestOnlyCotfsOwnWorkspacesAreTrusted:
    def test_a_workspace_under_the_data_dir_is_ours(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", tmp_path)
        workspace = tmp_path / "workspaces" / "slack" / "dm-someone"
        workspace.mkdir(parents=True)
        assert pty_install.cotf_owns_workspace(workspace) is True

    def test_an_operators_own_checkout_is_not_ours(self, tmp_path, monkeypatch):
        """A session pointed at a real repo must not have cotf silently mark it
        trusted on the operator's behalf."""
        monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", tmp_path / "cotf")
        assert pty_install.cotf_owns_workspace(tmp_path / "my-repo") is False

    def test_a_traversal_out_of_the_workspace_root_is_not_ours(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", tmp_path / "cotf")
        (tmp_path / "cotf" / "workspaces").mkdir(parents=True)
        escaped = tmp_path / "cotf" / "workspaces" / ".." / ".." / "elsewhere"
        assert pty_install.cotf_owns_workspace(escaped) is False

    def test_ensure_refuses_a_workspace_that_is_not_ours(
        self, state_file, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", tmp_path / "cotf")
        assert pty_install.ensure_workspace_trusted(tmp_path / "my-repo") is False
        assert not state_file.exists()

    def test_ensure_trusts_ours_once_and_then_stops_reading(
        self, state_file, tmp_path, monkeypatch
    ):
        """The state file reaches megabytes; a daemon runs many turns against
        one workspace."""
        monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", tmp_path)
        workspace = tmp_path / "workspaces" / "slack" / "dm-someone"
        workspace.mkdir(parents=True)
        calls = {"n": 0}
        real = pty_install.trust_workspace

        def counting(ws, env=None):
            calls["n"] += 1
            return real(ws, env)

        monkeypatch.setattr(pty_install, "trust_workspace", counting)

        assert pty_install.ensure_workspace_trusted(workspace) is True
        assert pty_install.ensure_workspace_trusted(workspace) is True
        assert calls["n"] == 1

    def test_a_failed_trust_is_not_memoized(self, state_file, tmp_path, monkeypatch):
        """Otherwise a transient write failure would disable the check for the
        rest of the daemon's life."""
        monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", tmp_path)
        workspace = tmp_path / "workspaces" / "slack" / "dm-someone"
        workspace.mkdir(parents=True)
        monkeypatch.setattr(pty_install, "trust_workspace", lambda ws, env=None: False)
        assert pty_install.ensure_workspace_trusted(workspace) is False
        assert str(workspace) not in pty_install._trusted_workspaces


class TestWorkspaceIsTrusted:
    def test_it_reports_a_recorded_grant(self, state_file, tmp_path):
        workspace = tmp_path / "ws"
        state_file.write_text(
            json.dumps({"projects": {str(workspace): {"hasTrustDialogAccepted": True}}})
        )
        assert pty_install.workspace_is_trusted(workspace) is True

    @pytest.mark.parametrize(
        "projects",
        [
            pytest.param({}, id="no-entry"),
            pytest.param({"WS": {}}, id="entry-without-the-key"),
            pytest.param(
                {"WS": {"hasTrustDialogAccepted": False}}, id="explicit-false"
            ),
            pytest.param({"WS": "not a dict"}, id="unusable-entry"),
        ],
    )
    def test_anything_short_of_a_recorded_grant_reads_as_untrusted(
        self, state_file, tmp_path, projects
    ):
        workspace = tmp_path / "ws"
        resolved = {k.replace("WS", str(workspace)): v for k, v in projects.items()}
        state_file.write_text(json.dumps({"projects": resolved}))
        assert pty_install.workspace_is_trusted(workspace) is False

    def test_an_unreadable_state_file_reads_as_untrusted(self, state_file, tmp_path):
        assert pty_install.workspace_is_trusted(tmp_path / "ws") is False
