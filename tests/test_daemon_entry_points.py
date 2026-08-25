"""The three daemon `main()` functions.

These are what `claude-cron`, `claude-telegram` and `claude-slack` actually run, so
a wiring mistake here is a daemon that will not start — invisible to every other
test in the suite. The long-running part is stubbed; the argument parsing, the
config validation, and the exit codes are not.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from claude_on_the_fly import cron as cron_mod
from claude_on_the_fly import slack as slack_mod
from claude_on_the_fly import slack_manifest
from claude_on_the_fly import telegram as telegram_mod


@pytest.fixture
def no_dotenv(monkeypatch):
    """`load_dotenv` is imported inside each main(), so patch it at the source."""
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: True)


@pytest.fixture
def no_daemon_logging(monkeypatch):
    from claude_on_the_fly import preflight

    monkeypatch.setattr(preflight, "setup_daemon_logging", lambda _role: None)


class TestCronMain:
    def _config(self, tmp_path: Path) -> Path:
        path = tmp_path / "cron.yaml"
        path.write_text(
            yaml.safe_dump(
                {"entries": [{"name": "a", "cron": "* * * * *", "prompt": "x"}]}
            )
        )
        return path

    def test_a_missing_config_refuses_to_start(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging
    ):
        """Starting with no schedule would look like a running daemon that never
        fires, which is the hardest failure of all to notice."""
        monkeypatch.setattr(
            "sys.argv", ["claude-cron", "--config", str(tmp_path / "gone.yaml")]
        )
        with pytest.raises(SystemExit, match="config not found"):
            cron_mod.main()

    def test_a_broken_config_names_the_error(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging
    ):
        bad = tmp_path / "cron.yaml"
        bad.write_text("entries: [unclosed")
        monkeypatch.setattr("sys.argv", ["claude-cron", "--config", str(bad)])
        with pytest.raises(SystemExit, match="config error"):
            cron_mod.main()

    def test_a_good_config_starts_the_daemon_and_exits_zero(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging
    ):
        config = self._config(tmp_path)
        monkeypatch.setattr("sys.argv", ["claude-cron", "--config", str(config)])
        from claude_on_the_fly.jobs import registry

        monkeypatch.setattr(registry, "make_queue", lambda: MagicMock())
        ran: list[str] = []

        def fake_asyncio_run(coro):
            coro.close()
            ran.append("ran")

        monkeypatch.setattr(cron_mod.asyncio, "run", fake_asyncio_run)
        assert cron_mod.main() == 0
        assert ran == ["ran"]

    def test_a_keyboard_interrupt_is_a_clean_exit(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging
    ):
        """Ctrl-C on a foreground daemon is how it is normally stopped; a traceback
        there would look like a crash."""
        config = self._config(tmp_path)
        monkeypatch.setattr("sys.argv", ["claude-cron", "--config", str(config)])
        from claude_on_the_fly.jobs import registry

        monkeypatch.setattr(registry, "make_queue", lambda: MagicMock())

        def interrupted(coro):
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(cron_mod.asyncio, "run", interrupted)
        assert cron_mod.main() == 0

    def test_a_legacy_config_is_migrated_once_and_reported(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging, capsys
    ):
        """Only on the default path: an operator who passed --config chose that file
        and must not have a different one rewritten under them."""
        monkeypatch.setattr("sys.argv", ["claude-cron"])
        config = self._config(tmp_path)
        monkeypatch.setattr(
            cron_mod, "migrate_legacy_config", lambda: "moved jobs.yaml -> cron.yaml"
        )
        monkeypatch.setattr(cron_mod, "resolve_config_path", lambda _a: config)
        from claude_on_the_fly.jobs import registry

        monkeypatch.setattr(registry, "make_queue", lambda: MagicMock())
        monkeypatch.setattr(cron_mod.asyncio, "run", lambda coro: coro.close())
        assert cron_mod.main() == 0
        assert "moved jobs.yaml" in capsys.readouterr().err

    def test_an_explicit_config_skips_the_migration(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging
    ):
        config = self._config(tmp_path)
        monkeypatch.setattr("sys.argv", ["claude-cron", "--config", str(config)])
        migrations: list[int] = []
        monkeypatch.setattr(
            cron_mod,
            "migrate_legacy_config",
            lambda: (migrations.append(1), None)[1],
        )
        from claude_on_the_fly.jobs import registry

        monkeypatch.setattr(registry, "make_queue", lambda: MagicMock())
        monkeypatch.setattr(cron_mod.asyncio, "run", lambda coro: coro.close())
        cron_mod.main()
        assert migrations == []

    def test_nothing_to_migrate_prints_nothing(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging, capsys
    ):
        monkeypatch.setattr("sys.argv", ["claude-cron"])
        config = self._config(tmp_path)
        monkeypatch.setattr(cron_mod, "migrate_legacy_config", lambda: None)
        monkeypatch.setattr(cron_mod, "resolve_config_path", lambda _a: config)
        from claude_on_the_fly.jobs import registry

        monkeypatch.setattr(registry, "make_queue", lambda: MagicMock())
        monkeypatch.setattr(cron_mod.asyncio, "run", lambda coro: coro.close())
        cron_mod.main()
        assert "cron:" not in capsys.readouterr().err


class TestTelegramMain:
    def test_the_preflight_result_is_what_the_frontend_is_built_from(
        self, monkeypatch, no_dotenv
    ):
        """Preflight resolves the token and the allowed user; building the frontend
        from anything else would let an unvalidated config start."""
        from claude_on_the_fly import preflight

        monkeypatch.setattr(preflight, "run_telegram", lambda: ("tok-123", 4242))
        built: list[tuple] = []
        monkeypatch.setattr(
            telegram_mod,
            "TelegramFrontend",
            lambda *, token, allowed_user_id: built.append((token, allowed_user_id)),
        )
        monkeypatch.setattr(telegram_mod.asyncio, "run", lambda coro: coro.close())
        telegram_mod.main()
        assert built == [("tok-123", 4242)]

    def test_a_failed_preflight_stops_before_the_frontend_is_built(
        self, monkeypatch, no_dotenv
    ):
        from claude_on_the_fly import preflight

        monkeypatch.setattr(
            preflight,
            "run_telegram",
            lambda: (_ for _ in ()).throw(SystemExit("TELEGRAM_BOT_TOKEN missing")),
        )
        monkeypatch.setattr(
            telegram_mod,
            "TelegramFrontend",
            lambda **_kw: pytest.fail("built a frontend from an invalid config"),
        )
        with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN missing"):
            telegram_mod.main()


class TestSlackMain:
    def _preflight(self):
        return "xapp-tok", "xoxb-tok", "U_SELF"

    def test_the_preflight_result_is_what_the_frontend_is_built_from(
        self, monkeypatch, no_dotenv
    ):
        from claude_on_the_fly import preflight

        monkeypatch.setattr("sys.argv", ["claude-slack"])
        monkeypatch.setattr(preflight, "run_slack", self._preflight)
        built: list[dict] = []
        monkeypatch.setattr(
            slack_mod, "SlackFrontend", lambda **kwargs: built.append(kwargs)
        )
        monkeypatch.setattr(slack_mod.asyncio, "run", lambda coro: coro.close())
        slack_mod.main()
        assert built[0] == {
            "app_token": "xapp-tok",
            "token": "xoxb-tok",
            "user_id": "U_SELF",
        }

    def test_the_sender_lists_are_left_unset_so_they_read_live(
        self, monkeypatch, no_dotenv
    ):
        """Passing them would pin them, and adding an allowed sender would be back to
        needing a restart."""
        from claude_on_the_fly import preflight

        monkeypatch.setattr("sys.argv", ["claude-slack"])
        monkeypatch.setattr(preflight, "run_slack", self._preflight)
        built: list[dict] = []
        monkeypatch.setattr(
            slack_mod, "SlackFrontend", lambda **kwargs: built.append(kwargs)
        )
        monkeypatch.setattr(slack_mod.asyncio, "run", lambda coro: coro.close())
        slack_mod.main()
        for key in (
            "allowed_user_ids",
            "blocked_senders",
            "allowed_bot_ids",
            "silent_sender_ids",
        ):
            assert key not in built[0], key

    def test_the_manifest_flag_generates_and_exits(self, monkeypatch, no_dotenv):
        """It must not fall through into starting a daemon: the operator asked for a
        file, not a running bot."""
        from claude_on_the_fly import preflight

        monkeypatch.setattr(
            "sys.argv",
            ["claude-slack", "--manifest", "--mode", "bot", "--name", "cof"],
        )
        monkeypatch.setattr(
            preflight,
            "run_slack",
            lambda: pytest.fail("started a daemon on a --manifest run"),
        )
        seen: list[dict] = []
        monkeypatch.setattr(
            slack_manifest, "generate", lambda **kwargs: (seen.append(kwargs), 0)[1]
        )
        with pytest.raises(SystemExit) as caught:
            slack_mod.main()
        assert caught.value.code == 0
        assert seen[0]["mode"] == "bot"
        assert seen[0]["name"] == "cof"

    def test_the_manifest_flag_passes_the_command_and_output_path_through(
        self, monkeypatch, no_dotenv, tmp_path
    ):
        monkeypatch.setattr(
            "sys.argv",
            [
                "claude-slack",
                "--manifest",
                "--command",
                "/cof-hoss",
                "--out",
                str(tmp_path / "manifest.json"),
            ],
        )
        seen: list[dict] = []
        monkeypatch.setattr(
            slack_manifest, "generate", lambda **kwargs: (seen.append(kwargs), 0)[1]
        )
        with pytest.raises(SystemExit):
            slack_mod.main()
        assert seen[0]["command"] == "/cof-hoss"
        assert seen[0]["out"].endswith("manifest.json")


class TestCronRunLoop:
    """The inner coroutine main() actually runs: signal handlers, the heartbeat, and
    the teardown that removes the heartbeat file so the TUI stops calling it live."""

    def _config(self, tmp_path: Path) -> Path:
        path = tmp_path / "cron.yaml"
        path.write_text(
            yaml.safe_dump(
                {"entries": [{"name": "a", "cron": "* * * * *", "prompt": "x"}]}
            )
        )
        return path

    def _stub_daemon(self, monkeypatch):
        from unittest.mock import AsyncMock

        daemon = MagicMock()
        daemon.run = AsyncMock()
        daemon.stop = AsyncMock()
        monkeypatch.setattr(cron_mod, "CronDaemon", lambda **_kw: daemon)
        return daemon

    def _stub_heartbeat(self, monkeypatch, tmp_path):
        from unittest.mock import AsyncMock

        from claude_on_the_fly import heartbeat as heartbeat_mod

        writer = MagicMock()
        writer.run = AsyncMock()
        writer.path = tmp_path / "cron.json"
        writer.path.write_text("{}")
        writer.remove_owned.side_effect = lambda: writer.path.unlink(missing_ok=True)
        monkeypatch.setattr(
            heartbeat_mod, "HeartbeatWriter", lambda _role, **_kwargs: writer
        )
        return writer

    def test_the_daemon_runs_and_the_heartbeat_is_cleaned_up(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging
    ):
        """A heartbeat file left behind makes `claude-tui` report a daemon that is not
        there, and the startup guard then refuses to start a real one."""
        config = self._config(tmp_path)
        monkeypatch.setattr("sys.argv", ["claude-cron", "--config", str(config)])
        from claude_on_the_fly.jobs import registry

        monkeypatch.setattr(registry, "make_queue", lambda: MagicMock())
        daemon = self._stub_daemon(monkeypatch)
        writer = self._stub_heartbeat(monkeypatch, tmp_path)

        assert cron_mod.main() == 0
        daemon.run.assert_awaited_once()
        assert not writer.path.exists(), "the heartbeat file outlived the daemon"

    def test_an_already_removed_heartbeat_is_not_an_error(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging
    ):
        config = self._config(tmp_path)
        monkeypatch.setattr("sys.argv", ["claude-cron", "--config", str(config)])
        from claude_on_the_fly.jobs import registry

        monkeypatch.setattr(registry, "make_queue", lambda: MagicMock())
        self._stub_daemon(monkeypatch)
        writer = self._stub_heartbeat(monkeypatch, tmp_path)
        writer.path.unlink()
        assert cron_mod.main() == 0

    def test_the_heartbeat_is_torn_down_even_when_the_daemon_raises(
        self, tmp_path, monkeypatch, no_dotenv, no_daemon_logging
    ):
        config = self._config(tmp_path)
        monkeypatch.setattr("sys.argv", ["claude-cron", "--config", str(config)])
        from unittest.mock import AsyncMock

        from claude_on_the_fly.jobs import registry

        monkeypatch.setattr(registry, "make_queue", lambda: MagicMock())
        daemon = self._stub_daemon(monkeypatch)
        daemon.run = AsyncMock(side_effect=RuntimeError("queue vanished"))
        writer = self._stub_heartbeat(monkeypatch, tmp_path)

        with pytest.raises(RuntimeError, match="queue vanished"):
            cron_mod.main()
        assert not writer.path.exists()
