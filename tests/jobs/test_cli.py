"""jobs.cli: argv normalization, enqueue producer, and config-from-env helpers."""

from __future__ import annotations

import json
from pathlib import Path

from claude_on_the_fly.jobs import cli


def test_normalize_argv_bare_defaults_to_run() -> None:
    assert cli._normalize_argv([]) == ["run"]


def test_normalize_argv_keeps_subcommands() -> None:
    assert cli._normalize_argv(["doctor"]) == ["doctor"]
    assert cli._normalize_argv(["enqueue", "hi"]) == ["enqueue", "hi"]
    assert cli._normalize_argv(["-h"]) == ["-h"]


def test_poll_interval_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("JOBS_POLL_INTERVAL_S", raising=False)
    assert cli._poll_interval_s() == cli.DEFAULT_POLL_INTERVAL_S
    monkeypatch.setenv("JOBS_POLL_INTERVAL_S", "0.5")
    assert cli._poll_interval_s() == 0.5
    monkeypatch.setenv("JOBS_POLL_INTERVAL_S", "notanumber")
    assert cli._poll_interval_s() == cli.DEFAULT_POLL_INTERVAL_S


def test_timeout_default_and_override(monkeypatch) -> None:
    from claude_on_the_fly import agent

    monkeypatch.delenv("JOBS_TIMEOUT", raising=False)
    assert cli._timeout_s() == agent.DEFAULT_TIMEOUT
    monkeypatch.setenv("JOBS_TIMEOUT", "120")
    assert cli._timeout_s() == 120.0


def test_timeout_non_positive_means_no_limit(monkeypatch) -> None:
    # 0 or negative JOBS_TIMEOUT = "no limit" → None, so agent.run skips wait_for
    # rather than firing an immediate/negative timeout.
    monkeypatch.setenv("JOBS_TIMEOUT", "0")
    assert cli._timeout_s() is None
    monkeypatch.setenv("JOBS_TIMEOUT", "-5")
    assert cli._timeout_s() is None


def test_enqueue_writes_job_to_queue(monkeypatch, tmp_path: Path, capsys) -> None:
    from claude_on_the_fly import agent

    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.delenv("JOBS_QUEUE_KIND", raising=False)

    rc = cli._cmd_enqueue("summarize the logs", channel="C1", thread_ts="1699.5")
    assert rc == 0

    out = capsys.readouterr().out
    assert out.startswith("queued job ")

    new_files = list((tmp_path / "jobs" / "new").glob("*.json"))
    assert len(new_files) == 1
    payload = json.loads(new_files[0].read_text())
    assert payload["prompt"] == "summarize the logs"
    assert payload["origin"] == {
        "channel": "C1",
        "thread_ts": "1699.5",
        "sender_id": "cli",
    }


def test_resolve_jobs_token_prefers_override(monkeypatch) -> None:
    name, token = cli.checks.resolve_jobs_token(
        {"JOBS_SLACK_TOKEN": "xoxb-jobs", "SLACK_TOKEN": "xoxp-frontend"}
    )
    assert name == "JOBS_SLACK_TOKEN"
    assert token == "xoxb-jobs"


def test_resolve_jobs_token_falls_back_to_slack_token() -> None:
    name, token = cli.checks.resolve_jobs_token({"SLACK_TOKEN": "xoxp-frontend"})
    assert name == "SLACK_TOKEN"
    assert token == "xoxp-frontend"


def test_loop_warning_fires_for_inherited_user_token() -> None:
    # Inheriting a user token from SLACK_TOKEN is the loop-prone default.
    assert cli._notifier_loop_warning("SLACK_TOKEN", "xoxp-abc") is not None


def test_loop_warning_silent_for_bot_token() -> None:
    assert cli._notifier_loop_warning("SLACK_TOKEN", "xoxb-abc") is None


def test_loop_warning_silent_for_explicit_override() -> None:
    # Deployer chose JOBS_SLACK_TOKEN explicitly — even a user token is their call.
    assert cli._notifier_loop_warning("JOBS_SLACK_TOKEN", "xoxp-abc") is None
