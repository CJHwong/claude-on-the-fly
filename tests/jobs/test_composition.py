"""L2 composition test — drive the REAL worker wiring end to end.

The unit suite passed while `claude-jobs run` crashed on startup, because no test
exercised the run/composition path: `_cmd_run`'s prologue and `build_components`'
real queue → runner → notifier wiring. These two tests close that gap.

`test_run_once_drives_real_composition` builds the components exactly as the
daemon does (real `FileInboxQueue`, real `OrchestratorAgentRunner`, real
`SlackThreadNotifier`), stubbing four seams — the Slack client, `agent.run`,
`agent.DATA_DIR` and `current_backend_key` — then runs
one worker iteration, so a mis-constructed notifier or a token that never
reaches the client would fail it. `test_cmd_run_calls_setup_logging_and_wires_token`
guards the rest of `_cmd_run`'s prologue: a wrong `_setup_logging` arity (the bug
that crashed startup) or a token that never reaches `_run`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_on_the_fly import agent, logs
from claude_on_the_fly.agent import Response
from claude_on_the_fly.jobs import cli
from claude_on_the_fly.jobs.core import Job
from claude_on_the_fly.jobs.worker import run_once


class _FakeSlackClient:
    """Stand-in for `AsyncWebClient` that records every post. Same
    `(*, channel, **kwargs)` shape as the real client, so it satisfies the
    notifier's `_PostsMessages` port."""

    def __init__(self, **_: Any) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, *, channel: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"channel": channel, **kwargs})
        return {"ok": True, "ts": "1.0"}


async def test_run_once_drives_real_composition(monkeypatch, tmp_path: Path) -> None:
    # Real FileInboxQueue + real OrchestratorAgentRunner, rooted under a temp DATA_DIR.
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.delenv("JOBS_QUEUE_KIND", raising=False)

    # Real notifier, but its Slack client is our recording fake — patched at the
    # source module so build_components' local import picks it up.
    fake_client = _FakeSlackClient()
    monkeypatch.setattr(
        "slack_sdk.web.async_client.AsyncWebClient",
        lambda **kwargs: fake_client,
    )

    jobs_root = tmp_path / "jobs"

    # Canned agent reply; assert the new→cur transition is real while in-flight.
    async def _canned_run(**kwargs: Any) -> Response:
        assert list((jobs_root / "cur").glob("*.json")), "job should be in cur/ mid-run"
        assert not list((jobs_root / "new").glob("*.json")), "new/ empty mid-run"
        return Response(body="the canned reply")

    monkeypatch.setattr(agent, "run", _canned_run)
    monkeypatch.setattr(
        "claude_on_the_fly.jobs.agent_runner.current_backend_key",
        lambda _profile=None: "claude:native:sonnet",
    )

    queue, runner, notifier, recorder, alert_sink = cli.build_components(
        token="xoxb-test", env={}
    )

    job = Job(
        id="100-a",
        prompt="do the thing",
        origin={"channel": "C42", "thread_ts": "1699.5", "sender_id": "U9"},
    )
    queue.enqueue(job)

    did = await run_once(queue, runner, notifier, recorder, alert_sink)
    assert did is True

    # (a) The notifier posted the reply into the origin channel/thread.
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["channel"] == "C42"
    assert call["thread_ts"] == "1699.5"
    assert call["text"] == "the canned reply"

    # (b) The job moved new → cur → done.
    assert not list((jobs_root / "new").glob("*.json"))
    assert not list((jobs_root / "cur").glob("*.json"))
    assert (jobs_root / "done" / "100-a.json").exists()
    assert (jobs_root / "done" / "100-a.result.json").exists()


async def test_real_composition_alerts_a_failed_cron_job(
    monkeypatch, tmp_path: Path
) -> None:
    """The daemon's own wiring must include the alert sink: a failed cron job
    posts a heads-up to the configured alert channel."""
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.delenv("JOBS_QUEUE_KIND", raising=False)
    fake_client = _FakeSlackClient()
    monkeypatch.setattr(
        "slack_sdk.web.async_client.AsyncWebClient",
        lambda **kwargs: fake_client,
    )

    async def _fails(**kwargs: Any) -> Response:
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(agent, "run", _fails)
    monkeypatch.setattr(
        "claude_on_the_fly.jobs.agent_runner.current_backend_key",
        lambda _profile=None: "claude:native:sonnet",
    )

    queue, runner, notifier, recorder, alert_sink = cli.build_components(
        token="xoxb-test",
        env={"SLACK_ALERT_TARGET": "C99", "SLACK_TOKEN": "xoxb-test"},
    )
    assert alert_sink is not None
    queue.enqueue(
        Job(
            id="100-a",
            prompt="p",
            origin={"kind": "cron", "entry": "jira"},
            key="jira/ACE-1",
            session_key="jira/ACE-1",
            platform="cron",
        )
    )

    assert await run_once(queue, runner, notifier, recorder, alert_sink) is True

    # The failure reply went to the entry's log (no channel in origin); the
    # alert went to the configured channel.
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["channel"] == "C99"
    assert call["text"] == ":x: cron entry jira failed"


async def test_real_composition_records_a_keyed_outcome(
    monkeypatch, tmp_path: Path
) -> None:
    """The wiring the daemon uses must include the recorder, not just accept one.

    `run_once` taking a recorder is the easy half; the bug this pins is the
    producer's backoff reading a `failures` count that nothing ever incremented
    because the composition root never built one.
    """
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    monkeypatch.delenv("JOBS_QUEUE_KIND", raising=False)
    monkeypatch.setattr(
        "slack_sdk.web.async_client.AsyncWebClient",
        lambda **kwargs: _FakeSlackClient(),
    )

    async def _fails(**kwargs: Any) -> Response:
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(agent, "run", _fails)
    monkeypatch.setattr(
        "claude_on_the_fly.jobs.agent_runner.current_backend_key",
        lambda _profile=None: "claude:native:sonnet",
    )

    queue, runner, notifier, recorder, alert_sink = cli.build_components(
        token="xoxb-test", env={}
    )
    queue.enqueue(
        Job(
            id="100-a",
            prompt="p",
            origin={"kind": "cron", "entry": "jira"},
            key="jira/ACE-1",
            session_key="jira/ACE-1",
            platform="cron",
        )
    )

    assert await run_once(queue, runner, notifier, recorder, alert_sink) is True

    from claude_on_the_fly.jobs.key_state import KeyStateStore

    state = KeyStateStore(tmp_path / "jobs").load("jira/ACE-1")
    assert state.failures == 1, "a failed keyed job must leave a failure on record"
    assert state.last_failed_at > 0


def test_cmd_run_calls_setup_logging_and_wires_token(monkeypatch, tmp_path) -> None:
    """_cmd_run's prologue must call _setup_logging() with the right arity (the
    bug that crashed startup) and pass the resolved token through to _run.

    _setup_logging is left REAL (stubbing it is what would let an arity bug back
    in) but pointed at a tmp DATA_DIR, since it now installs a file handler on
    the root logger — untethered it would write into the real logs/jobs.log the
    running worker owns, and stay attached for the rest of the session."""
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-wired")
    monkeypatch.delenv("JOBS_SLACK_TOKEN", raising=False)
    monkeypatch.setattr(cli, "check_backend", lambda: None)
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)

    seen: dict[str, str] = {}

    async def _fake_run(token: str) -> None:
        seen["token"] = token

    monkeypatch.setattr(cli, "_run", _fake_run)

    root = logging.getLogger()
    before = list(root.handlers)
    try:
        rc = cli._cmd_run()
        # The handler delay-opens, so nothing exists until a record is emitted.
        logging.getLogger("test").info("hello")
        # The whole point of the local _setup_logging: a real per-day jobs log
        # for the dashboard's jobs tab to tail.
        assert (tmp_path / "logs" / logs.log_name("jobs")).is_file()
    finally:
        # `configure` replaces the root handlers wholesale, so restore whatever
        # the suite had rather than only removing what was added.
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)

    assert rc == 0
    assert seen["token"] == "xoxb-wired"
