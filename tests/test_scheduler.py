"""Tests for claude_on_the_fly.scheduler."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from claude_on_the_fly.agent import Response
from claude_on_the_fly.scheduler import (
    EXAMPLE_YAML,
    JobSpec,
    SchedulerFrontend,
    load_config,
    next_fire,
)


def test_example_yaml_is_valid_yaml_with_jobs_key() -> None:
    """The seeded template must be parseable YAML shaped like a real config.
    `jobs` is empty by design (the loader nudges you to add one), so we don't
    assert load_config succeeds — only that the seed isn't malformed."""
    parsed = yaml.safe_load(EXAMPLE_YAML)
    assert isinstance(parsed, dict)
    assert parsed.get("jobs") == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict | str) -> Path:
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_text(yaml.safe_dump(data))
    return path


def _make_script(
    tmp_path: Path, name: str = "job.sh", body: str = "#!/bin/bash\necho ok\n"
) -> Path:
    script = tmp_path / name
    script.write_text(body)
    script.chmod(0o755)
    return script


# ---------------------------------------------------------------------------
# load_config — valid
# ---------------------------------------------------------------------------


class TestLoadConfigValid:
    def test_prompt_job(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {"name": "ping", "cron": "0 3 * * *", "prompt": "hi"},
                ]
            },
        )
        specs = load_config(cfg)
        assert len(specs) == 1
        assert specs[0].name == "ping"
        assert specs[0].prompt == "hi"
        assert specs[0].script is None
        assert specs[0].kind == "prompt"
        assert specs[0].timeout == 1800  # default

    def test_script_job(self, tmp_path: Path) -> None:
        script = _make_script(tmp_path)
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {
                        "name": "release",
                        "cron": "0 18 * * 1-5",
                        "script": str(script),
                        "args": ["--verbose"],
                        "timeout": 300,
                    }
                ]
            },
        )
        specs = load_config(cfg)
        assert specs[0].script == script
        assert specs[0].args == ("--verbose",)
        assert specs[0].timeout == 300
        assert specs[0].kind == "script"

    def test_tilde_expanded_in_script_path(self, tmp_path: Path) -> None:
        script = _make_script(tmp_path)
        # Fake ~ by using an absolute path — just verify expanduser doesn't break
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "script": str(script)}]},
        )
        specs = load_config(cfg)
        assert specs[0].script == script


# ---------------------------------------------------------------------------
# load_config — invalid
# ---------------------------------------------------------------------------


class TestLoadConfigInvalid:
    def test_empty_jobs(self, tmp_path: Path) -> None:
        cfg = _write_yaml(tmp_path / "s.yaml", {"jobs": []})
        with pytest.raises(ValueError, match="at least one entry"):
            load_config(cfg)

    def test_missing_jobs_key(self, tmp_path: Path) -> None:
        cfg = _write_yaml(tmp_path / "s.yaml", {"other": 1})
        with pytest.raises(ValueError, match="'jobs' must be a list"):
            load_config(cfg)

    def test_root_not_mapping(self, tmp_path: Path) -> None:
        cfg = _write_yaml(tmp_path / "s.yaml", "- just a list\n")
        with pytest.raises(ValueError, match="root must be a mapping"):
            load_config(cfg)

    def test_missing_name(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"cron": "* * * * *", "prompt": "hi"}]},
        )
        with pytest.raises(ValueError, match="'name' is required"):
            load_config(cfg)

    def test_invalid_name_chars(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "bad name!", "cron": "* * * * *", "prompt": "hi"}]},
        )
        with pytest.raises(ValueError, match=r"must match"):
            load_config(cfg)

    def test_duplicate_name(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {"name": "dup", "cron": "* * * * *", "prompt": "a"},
                    {"name": "dup", "cron": "* * * * *", "prompt": "b"},
                ]
            },
        )
        with pytest.raises(ValueError, match="duplicate name"):
            load_config(cfg)

    def test_invalid_cron(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "not-a-cron", "prompt": "hi"}]},
        )
        with pytest.raises(ValueError, match="invalid cron"):
            load_config(cfg)

    def test_both_prompt_and_script(self, tmp_path: Path) -> None:
        script = _make_script(tmp_path)
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {
                        "name": "x",
                        "cron": "* * * * *",
                        "prompt": "hi",
                        "script": str(script),
                    }
                ]
            },
        )
        with pytest.raises(ValueError, match="'prompt' OR 'script', not both"):
            load_config(cfg)

    def test_neither_prompt_nor_script(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *"}]},
        )
        with pytest.raises(ValueError, match="must specify 'prompt' or 'script'"):
            load_config(cfg)

    def test_script_path_missing(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {
                        "name": "x",
                        "cron": "* * * * *",
                        "script": str(tmp_path / "nope.sh"),
                    }
                ]
            },
        )
        with pytest.raises(ValueError, match="script not found"):
            load_config(cfg)

    def test_invalid_timeout(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {
                        "name": "x",
                        "cron": "* * * * *",
                        "prompt": "hi",
                        "timeout": -1,
                    }
                ]
            },
        )
        with pytest.raises(ValueError, match="timeout"):
            load_config(cfg)

    def test_job_entry_not_mapping(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": ["not-a-dict"]},
        )
        with pytest.raises(ValueError, match="must be a mapping"):
            load_config(cfg)

    def test_missing_cron(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "prompt": "hi"}]},
        )
        with pytest.raises(ValueError, match="'cron' required"):
            load_config(cfg)

    def test_empty_prompt_string(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "prompt": "   "}]},
        )
        with pytest.raises(ValueError, match="'prompt' must be a non-empty string"):
            load_config(cfg)

    def test_script_not_a_string(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "script": 42}]},
        )
        with pytest.raises(ValueError, match="'script' must be a string path"):
            load_config(cfg)

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        cfg = _write_yaml(tmp_path / "s.yaml", "jobs: [unclosed\n")
        with pytest.raises(ValueError, match="YAML parse error"):
            load_config(cfg)

    def test_args_not_list(self, tmp_path: Path) -> None:
        script = _make_script(tmp_path)
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {
                        "name": "x",
                        "cron": "* * * * *",
                        "script": str(script),
                        "args": "--not-a-list",
                    }
                ]
            },
        )
        with pytest.raises(ValueError, match="'args' must be a list"):
            load_config(cfg)


# ---------------------------------------------------------------------------
# next_fire
# ---------------------------------------------------------------------------


class TestNextFire:
    def test_next_fire_advances(self) -> None:
        now = datetime(2026, 4, 23, 12, 0, 0)
        nxt = next_fire("30 14 * * *", now)
        assert nxt == datetime(2026, 4, 23, 14, 30, 0)

    def test_next_fire_rolls_over_day(self) -> None:
        now = datetime(2026, 4, 23, 23, 0, 0)
        nxt = next_fire("0 6 * * *", now)
        assert nxt == datetime(2026, 4, 24, 6, 0, 0)


# ---------------------------------------------------------------------------
# JobSpec
# ---------------------------------------------------------------------------


class TestJobSpec:
    def test_chat_id_stable(self) -> None:
        a = JobSpec(name="same", cron="* * * * *", prompt="hi")
        b = JobSpec(name="same", cron="0 0 * * *", prompt="different")
        assert a.chat_id == b.chat_id

    def test_chat_id_differs_for_different_names(self) -> None:
        a = JobSpec(name="a", cron="* * * * *", prompt="hi")
        b = JobSpec(name="b", cron="* * * * *", prompt="hi")
        assert a.chat_id != b.chat_id

    def test_chat_id_non_negative(self) -> None:
        # crc32 can be > 2^31; we mask to keep it positive for int semantics.
        spec = JobSpec(name="x" * 200, cron="* * * * *", prompt="hi")
        assert spec.chat_id >= 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _fake_orch() -> MagicMock:
    orch = MagicMock()
    orch.reset_session = MagicMock()
    return orch


class TestFirePrompt:
    async def test_resets_session_and_calls_on_message(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "ping", "cron": "* * * * *", "prompt": "hello"}]},
        )
        orch = _fake_orch()
        on_message = AsyncMock()

        fe = SchedulerFrontend(config_path=cfg)
        fe._on_message = on_message
        fe._orch = orch
        fe._reload()

        with patch("claude_on_the_fly.scheduler.LOG_DIR", tmp_path / "logs"):
            spec = fe._state["ping"].spec
            await fe._fire_prompt(spec)

        orch.reset_session.assert_called_once_with(spec.chat_id)
        on_message.assert_awaited_once_with(spec.chat_id, "hello")
        # Log block was written
        log = (tmp_path / "logs" / "schedule-ping.log").read_text()
        assert "fire (prompt)" in log
        assert "> hello" in log

    async def test_fire_dispatch_to_prompt(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "ping", "cron": "* * * * *", "prompt": "hello"}]},
        )
        orch = _fake_orch()
        on_message = AsyncMock()

        fe = SchedulerFrontend(config_path=cfg)
        fe._on_message = on_message
        fe._orch = orch
        fe._reload()

        with patch("claude_on_the_fly.scheduler.LOG_DIR", tmp_path / "logs"):
            spec = fe._state["ping"].spec
            await fe._fire(spec)

        on_message.assert_awaited_once_with(spec.chat_id, "hello")

    async def test_fire_dispatch_to_script(self, tmp_path: Path) -> None:
        script = _make_script(tmp_path, body="#!/bin/bash\necho ok\n")
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "s", "cron": "* * * * *", "script": str(script)}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()

        with patch("claude_on_the_fly.scheduler.LOG_DIR", tmp_path / "logs"):
            spec = fe._state["s"].spec
            await fe._fire(spec)

        assert len(fe._script_tasks) > 0
        # Clean up
        await asyncio.gather(*fe._script_tasks)

    async def test_fire_prompt_early_return_no_callbacks(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        spec = fe._state["x"].spec
        # No _on_message, no _orch set; should return without error
        await fe._fire_prompt(spec)  # should not raise


class TestRunScript:
    async def test_script_runs_and_logs(self, tmp_path: Path) -> None:
        script = _make_script(
            tmp_path, body="#!/bin/bash\necho 'hello from script'\nexit 0\n"
        )
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "job", "cron": "* * * * *", "script": str(script)}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()

        with patch("claude_on_the_fly.scheduler.LOG_DIR", tmp_path / "logs"):
            spec = fe._state["job"].spec
            await fe._run_script(spec)

        log = (tmp_path / "logs" / "schedule-job.log").read_text()
        assert "hello from script" in log
        assert "fire (script)" in log
        assert "exit=0" in log

    async def test_run_script_unexpected_exception(self, tmp_path: Path) -> None:
        script = _make_script(tmp_path, body="#!/bin/bash\necho ok\n")
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "script": str(script)}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()

        with patch("claude_on_the_fly.scheduler.LOG_DIR", tmp_path / "logs"):
            spec = fe._state["x"].spec
            with patch(
                "asyncio.create_subprocess_exec",
                side_effect=Exception("spawn failed"),
            ):
                await fe._run_script(spec)

        log = (tmp_path / "logs" / "schedule-x.log").read_text()
        assert "error: spawn failed" in log

    async def test_script_timeout_kills(self, tmp_path: Path) -> None:
        script = _make_script(tmp_path, body="#!/bin/bash\nsleep 30\n")
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {
                        "name": "slow",
                        "cron": "* * * * *",
                        "script": str(script),
                        "timeout": 1,
                    }
                ]
            },
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()

        started = time.monotonic()
        with patch("claude_on_the_fly.scheduler.LOG_DIR", tmp_path / "logs"):
            spec = fe._state["slow"].spec
            await fe._run_script(spec)
        elapsed = time.monotonic() - started
        assert elapsed < 5  # should have been killed quickly

        log = (tmp_path / "logs" / "schedule-slow.log").read_text()
        assert "timed out after 1s" in log


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


class TestReload:
    def test_adds_job(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "a", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        assert set(fe._state) == {"a"}

        _write_yaml(
            cfg,
            {
                "jobs": [
                    {"name": "a", "cron": "* * * * *", "prompt": "hi"},
                    {"name": "b", "cron": "0 3 * * *", "prompt": "hi"},
                ]
            },
        )
        added, removed, modified = fe._reload()
        assert added == {"b"}
        assert removed == set()
        assert modified == set()
        assert set(fe._state) == {"a", "b"}

    def test_removes_job(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {"name": "a", "cron": "* * * * *", "prompt": "hi"},
                    {"name": "b", "cron": "* * * * *", "prompt": "hi"},
                ]
            },
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()

        _write_yaml(
            cfg,
            {"jobs": [{"name": "a", "cron": "* * * * *", "prompt": "hi"}]},
        )
        added, removed, modified = fe._reload()
        assert added == set()
        assert removed == {"b"}
        assert set(fe._state) == {"a"}

    def test_removed_job_inflight_survives(self, tmp_path: Path) -> None:
        """In-flight script task is not cancelled when its job is removed from config."""
        slow = _make_script(tmp_path, body="#!/bin/bash\nsleep 0.5\necho done\n")
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {"name": "slow", "cron": "* * * * *", "script": str(slow)},
                ]
            },
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()

        async def _go() -> bool:
            with patch("claude_on_the_fly.scheduler.LOG_DIR", tmp_path / "logs"):
                spec = fe._state["slow"].spec
                fe._spawn_script(spec)
                # Reload with job removed while script still running
                await asyncio.sleep(0.1)
                _write_yaml(
                    cfg,
                    {"jobs": [{"name": "other", "cron": "* * * * *", "prompt": "hi"}]},
                )
                fe._reload()
                assert "slow" not in fe._state
                # Wait for the in-flight task
                await asyncio.gather(*fe._script_tasks)
                log = (tmp_path / "logs" / "schedule-slow.log").read_text()
                return "done" in log

        assert asyncio.run(_go())

    def test_modifies_cron_recomputes_next_fire(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "a", "cron": "0 3 * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        before = fe._state["a"].next_fire

        _write_yaml(
            cfg,
            {"jobs": [{"name": "a", "cron": "0 15 * * *", "prompt": "hi"}]},
        )
        added, removed, modified = fe._reload()
        assert modified == {"a"}
        after = fe._state["a"].next_fire
        assert before != after

    def test_unchanged_spec_preserves_next_fire(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "a", "cron": "0 3 * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        before = fe._state["a"].next_fire

        # Re-reload with identical content
        fe._reload()
        after = fe._state["a"].next_fire
        assert before == after

    def test_invalid_reload_keeps_prior(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "a", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        original_state = dict(fe._state)
        fe._mtime = cfg.stat().st_mtime

        # Write invalid yaml
        cfg.write_text("jobs: not-a-list\n")
        # Force a different mtime
        future_time = cfg.stat().st_mtime + 10
        import os as _os

        _os.utime(cfg, (future_time, future_time))

        fe._maybe_reload()  # should log + keep prior
        assert fe._state == original_state

    def test_modify_spec_same_cron_preserves_next_fire(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "a", "cron": "0 3 * * *", "prompt": "old prompt"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        before = fe._state["a"].next_fire

        _write_yaml(
            cfg,
            {"jobs": [{"name": "a", "cron": "0 3 * * *", "prompt": "new prompt"}]},
        )
        added, removed, modified = fe._reload()
        assert modified == {"a"}
        after = fe._state["a"].next_fire
        assert before == after  # cron unchanged, so next_fire preserved

    def test_maybe_reload_oserror_stat(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "a", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()

        mock_path = MagicMock()
        mock_path.stat.side_effect = OSError("permission denied")
        fe._config_path = mock_path

        fe._maybe_reload()  # should log warning, not raise

    def test_maybe_reload_unchanged_mtime(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "a", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        # mtime already matches, _reload should not be called again
        mock_path = MagicMock()
        mock_path.stat.return_value.st_mtime = fe._mtime
        fe._config_path = mock_path

        with patch.object(fe, "_reload") as mock_reload:
            fe._maybe_reload()
            mock_reload.assert_not_called()

    def test_maybe_reload_success_log(self, tmp_path: Path, caplog) -> None:
        import logging

        caplog.set_level(logging.INFO)
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "a", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        fe._mtime = cfg.stat().st_mtime

        _write_yaml(
            cfg,
            {"jobs": [{"name": "b", "cron": "0 0 * * *", "prompt": "hi"}]},
        )
        # Force mtime bump so the real file triggers reload
        future_time = cfg.stat().st_mtime + 10
        import os as _os

        _os.utime(cfg, (future_time, future_time))

        fe._maybe_reload()
        assert "reloaded:" in caplog.text


# ---------------------------------------------------------------------------
# _print_summary
# ---------------------------------------------------------------------------


class TestPrintSummary:
    def test_prints_job_table(self, tmp_path: Path, capsys) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        with patch.object(fe, "_print_summary") as mock_print:
            fe._print_summary()
            mock_print.assert_called_once()

    def test_summary_output(self, tmp_path: Path, capsys) -> None:

        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        fe._print_summary()
        captured = capsys.readouterr()
        assert "Scheduler started" in captured.err
        assert "x" in captured.err

    def test_empty_state_no_output(self, capsys) -> None:
        fe = SchedulerFrontend(config_path=Path("/x.yaml"))
        fe._print_summary()
        captured = capsys.readouterr()
        assert "Scheduler started — 0 jobs" in captured.err


# ---------------------------------------------------------------------------
# Frontend protocol
# ---------------------------------------------------------------------------


class TestFrontendProtocol:
    async def test_send_writes_log(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "ping", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        spec = fe._state["ping"].spec

        with patch("claude_on_the_fly.scheduler.LOG_DIR", tmp_path / "logs"):
            await fe.send(
                spec.chat_id,
                Response(body="answer", cost=0.01, model="claude-sonnet-4-6"),
            )

        log = (tmp_path / "logs" / "schedule-ping.log").read_text()
        assert "answer" in log
        assert "claude-sonnet-4-6" in log
        assert "=== done ===" in log

    def test_workspace_name_uses_job_name(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "standup", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        spec = fe._state["standup"].spec
        assert fe.workspace_name(spec.chat_id) == "schedule/standup"

    def test_channel_context_includes_cron(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "30 6 * * 1-5", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        spec = fe._state["x"].spec
        assert fe.channel_context(spec.chat_id) == "cron:30 6 * * 1-5"

    def test_timeout_for_returns_spec_timeout(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {
                "jobs": [
                    {
                        "name": "x",
                        "cron": "* * * * *",
                        "prompt": "hi",
                        "timeout": 600,
                    }
                ]
            },
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        spec = fe._state["x"].spec
        assert fe.timeout_for(spec.chat_id) == 600.0

    def test_timeout_for_unknown_chat_returns_none(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()
        assert fe.timeout_for(999_999_999) is None

    def test_set_orchestrator_rejects_wrong_type(self, tmp_path: Path) -> None:
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        with pytest.raises(TypeError):
            fe.set_orchestrator("not an orchestrator")

    def test_set_orchestrator_success(self, tmp_path: Path) -> None:
        from claude_on_the_fly.orchestrator import Orchestrator

        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "x", "cron": "* * * * *", "prompt": "hi"}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        orch = MagicMock(spec=Orchestrator)
        fe.set_orchestrator(orch)
        assert fe._orch is orch

    def test_sender_name_default(self) -> None:
        fe = SchedulerFrontend(config_path=Path("/x.yaml"))
        assert fe.sender_name(42) == "scheduler"

    def test_channel_context_default(self, tmp_path: Path) -> None:
        fe = SchedulerFrontend(config_path=Path("/x.yaml"))
        assert fe.channel_context(999) == "cron"

    async def test_send_typing_returns_none(self) -> None:
        fe = SchedulerFrontend(config_path=Path("/x.yaml"))
        assert await fe.send_typing(42) is None

    async def test_notify_queued_logs_debug(self) -> None:
        fe = SchedulerFrontend(config_path=Path("/x.yaml"))
        await fe.notify_queued(42, 3)  # should not raise


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


class TestStop:
    async def test_cancels_inflight_scripts(self, tmp_path: Path) -> None:
        slow = _make_script(tmp_path, body="#!/bin/bash\nsleep 30\n")
        cfg = _write_yaml(
            tmp_path / "s.yaml",
            {"jobs": [{"name": "slow", "cron": "* * * * *", "script": str(slow)}]},
        )
        fe = SchedulerFrontend(config_path=cfg)
        fe._reload()

        with patch("claude_on_the_fly.scheduler.LOG_DIR", tmp_path / "logs"):
            spec = fe._state["slow"].spec
            fe._spawn_script(spec)
            await asyncio.sleep(0.1)
            assert len(fe._script_tasks) == 1

            started = time.monotonic()
            await fe.stop()
            elapsed = time.monotonic() - started
            assert elapsed < 5  # should not wait for the 30s sleep
