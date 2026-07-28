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

from claude_on_the_fly import agent
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
        lambda: "claude:native:sonnet",
    )

    queue, runner, notifier = cli.build_components(token="xoxb-test")

    job = Job(
        id="100-a",
        prompt="do the thing",
        origin={"channel": "C42", "thread_ts": "1699.5", "sender_id": "U9"},
    )
    queue.enqueue(job)

    did = await run_once(queue, runner, notifier)
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
    before = set(root.handlers)
    try:
        rc = cli._cmd_run()
    finally:
        for handler in set(root.handlers) - before:
            handler.close()
            root.removeHandler(handler)

    assert rc == 0
    assert seen["token"] == "xoxb-wired"
    # The whole point of the local _setup_logging: a real logs/jobs.log for the
    # dashboard's jobs tab to tail.
    assert (tmp_path / "logs" / "jobs.log").is_file()
