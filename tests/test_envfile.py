"""The one reader for the environment a daemon actually runs with.

The bug behind this module: a viewer resolved `CLAUDE_CONFIG_DIR` from its own
shell while the daemon that wrote the files got it from `DATA_DIR/.env`, so the
TUI reported "agent hasn't run a turn" over a session that was streaming. Every
test here sets those two to *different* directories on purpose. A test that let
them agree would pass no matter which one the code read, which is precisely how
the original slipped through.
"""

from __future__ import annotations

import os
from pathlib import Path

from claude_on_the_fly import checks, envfile, transcript


class TestMerged:
    def test_the_file_wins_over_the_environment(self, monkeypatch, tmp_path):
        """The spawn path's rule. A viewer that let the shell win would be
        reading its own configuration, not the daemon's."""
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_KEY=from-file\n")
        monkeypatch.setenv("SOME_KEY", "from-shell")
        assert envfile.merged(env_file)["SOME_KEY"] == "from-file"

    def test_the_environment_shows_through_where_the_file_is_silent(
        self, monkeypatch, tmp_path
    ):
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_KEY=from-file\n")
        monkeypatch.setenv("OTHER_KEY", "from-shell")
        assert envfile.merged(env_file)["OTHER_KEY"] == "from-shell"

    def test_none_means_no_file_not_the_default_one(self, monkeypatch, tmp_path):
        """`spawn` passes None for a caller who wants a bare environment."""
        (tmp_path / ".env").write_text("SOME_KEY=from-file\n")
        monkeypatch.setattr(envfile, "default_env_file", lambda: tmp_path / ".env")
        monkeypatch.setenv("SOME_KEY", "from-shell")
        assert envfile.merged(None)["SOME_KEY"] == "from-shell"

    def test_an_absent_file_is_not_an_error(self, tmp_path):
        assert isinstance(envfile.merged(tmp_path / "nope.env"), dict)

    def test_a_reparse_is_skipped_until_the_file_changes(self, monkeypatch, tmp_path):
        """Read on every TUI frame, so the parse is cached on the file's mtime."""
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_KEY=one\n")
        monkeypatch.setattr(envfile, "_parsed", None)
        parses = {"n": 0}
        real = envfile.dotenv_values

        def counting(path):
            parses["n"] += 1
            return real(path)

        monkeypatch.setattr(envfile, "dotenv_values", counting)
        assert envfile.merged(env_file)["SOME_KEY"] == "one"
        assert envfile.merged(env_file)["SOME_KEY"] == "one"
        assert parses["n"] == 1

    def test_a_changed_file_is_picked_up(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_KEY=one\n")
        monkeypatch.setattr(envfile, "_parsed", None)
        assert envfile.merged(env_file)["SOME_KEY"] == "one"
        os.utime(env_file, (0, 0))
        env_file.write_text("SOME_KEY=two\n")
        assert envfile.merged(env_file)["SOME_KEY"] == "two"

    def test_a_later_environment_change_is_still_visible(self, monkeypatch, tmp_path):
        """The parse is cached; the merge never is."""
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_KEY=from-file\n")
        assert "LATE_KEY" not in envfile.merged(env_file)
        monkeypatch.setenv("LATE_KEY", "arrived")
        assert envfile.merged(env_file)["LATE_KEY"] == "arrived"


class TestClaudeConfigDir:
    def test_it_follows_the_env_file_over_the_shell(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(f"CLAUDE_CONFIG_DIR={tmp_path / 'A'}\n")
        monkeypatch.setattr(envfile, "default_env_file", lambda: env_file)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "B"))
        assert envfile.claude_config_dir() == tmp_path / "A"

    def test_a_supplied_mapping_is_used_as_given(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "B"))
        resolved = envfile.claude_config_dir({"CLAUDE_CONFIG_DIR": str(tmp_path / "C")})
        assert resolved == tmp_path / "C"

    def test_it_falls_back_to_claudes_own_default(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert envfile.claude_config_dir({}) == Path.home() / ".claude"


class TestTheViewerAgreesWithTheDaemon:
    """The reported bug, at the two call sites that showed it."""

    def _split_brain(self, monkeypatch, tmp_path) -> Path:
        """`.env` says A (what the daemon gets), the shell says B."""
        daemon_config = tmp_path / "claude-A"
        env_file = tmp_path / ".env"
        env_file.write_text(f"CLAUDE_CONFIG_DIR={daemon_config}\n")
        monkeypatch.setattr(envfile, "default_env_file", lambda: env_file)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-B"))
        return daemon_config

    def test_the_projects_dir_is_the_one_the_daemon_writes_to(
        self, monkeypatch, tmp_path
    ):
        daemon_config = self._split_brain(monkeypatch, tmp_path)
        assert transcript.claude_projects_dir() == daemon_config / "projects"

    def test_a_streaming_session_is_found_not_reported_as_absent(
        self, monkeypatch, tmp_path
    ):
        """End to end: the watch pane's own lookup over a real JSONL.

        `resolve_session_log` returning None is what renders "agent hasn't run
        a turn", so this asserts the exact input to that branch.
        """
        from claude_on_the_fly import agent

        daemon_config = self._split_brain(monkeypatch, tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        session_uuid = "11111111-2222-3333-4444-555555555555"
        session_dir = (
            daemon_config / "projects" / transcript._workspace_to_claude_hash(workspace)
        )
        session_dir.mkdir(parents=True)
        jsonl = session_dir / f"{session_uuid}.jsonl"
        jsonl.write_text('{"type":"user","message":{"content":"hi"}}\n')

        assert agent.resolve_session_log(workspace, session_uuid) == jsonl
        assert transcript.extract_claude(workspace, session_uuid) is not None

    @staticmethod
    def _pty_shim(tmp_path: Path) -> Path:
        """A script that looks like pty's envelope shim by what it acts on."""
        shim = tmp_path / "cotf-pty-shim"
        shim.write_text(
            "#!/bin/sh\n"
            f'out="${checks.PTY_ENVELOPE_MARKER}"\n'
            f'touch "$out{checks.PTY_TRIGGER_MARKER}"\n'
        )
        return shim

    @staticmethod
    def _settings_with_hook(config_dir: Path, command: str) -> None:
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "settings.json").write_text(
            '{"hooks": {"PostCompact": [{"hooks": [{"type": "command",'
            f' "command": "{command}"}}]}}]}}}}'
        )

    def test_a_check_reads_the_config_dir_it_was_handed(self, monkeypatch, tmp_path):
        """`check_all` threads a resolved mapping through; this used to take no
        parameter and read `os.environ` anyway, so no caller could redirect
        it."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "shell-dir"))
        handed = {"CLAUDE_CONFIG_DIR": str(tmp_path / "handed-dir")}
        result = checks.check_pty_hooks(handed)
        assert "handed-dir" in result.detail
        assert "shell-dir" not in result.detail

    def test_the_hook_path_check_reads_the_handed_dir_too(self, monkeypatch, tmp_path):
        """Discriminating on purpose: an unreadable settings.json yields "—",
        so only a check that found the handed dir can report a wired count."""
        handed_dir = tmp_path / "handed-dir"
        self._settings_with_hook(handed_dir, str(self._pty_shim(tmp_path)))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "shell-dir"))

        assert checks.check_pty_hook_paths({}).detail == "—"
        handed = {"CLAUDE_CONFIG_DIR": str(handed_dir)}
        assert checks.check_pty_hook_paths(handed).detail == "1 wired, all present"

    def test_the_compaction_gate_reads_the_daemons_config(self, monkeypatch, tmp_path):
        """Called on the compaction path itself, where a wrong answer hangs the
        turn rather than merely misreporting it."""
        daemon_config = self._split_brain(monkeypatch, tmp_path)
        self._settings_with_hook(daemon_config, str(self._pty_shim(tmp_path)))
        assert checks.pty_postcompact_hook_wired() is True


class TestDefaultEnvFile:
    """The autouse `isolate_env_file` fixture replaces this everywhere else."""

    def test_it_is_the_env_file_in_the_operators_data_dir(self, monkeypatch, tmp_path):
        import importlib

        monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", tmp_path)
        # Reach the real implementation, not the fixture's stand-in.
        real = importlib.reload(envfile).default_env_file
        try:
            assert real() == tmp_path / ".env"
        finally:
            importlib.reload(envfile)

    def test_a_path_that_cannot_be_stat_ed_reads_as_no_file(self, tmp_path):
        """`merged` guards with `is_file()`, so this is only reachable if the
        file goes away between the two calls. Answer empty rather than raise:
        a vanished env file is a deployment with no env file."""
        assert envfile._file_values(tmp_path / "gone" / ".env") == {}
