"""alerts: the failure-alert sinks, their wrappers, and the factory."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from claude_on_the_fly.jobs.alerts import (
    ALERT_BODY_LIMIT,
    CooldownAlertSink,
    CronOriginAlertSink,
    MultiAlertSink,
    SlackAlertSink,
    TelegramAlertSink,
    _alert_body,
    build_alert_sink,
)
from claude_on_the_fly.jobs.core import Result


def _origin(entry: str = "jira") -> dict:
    return {"kind": "cron", "entry": entry}


def _result(text: str = "boom") -> Result:
    return Result(ok=False, text=text)


class _FakeSlackClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, *, channel: str, **kwargs: Any) -> dict:
        self.calls.append({"channel": channel, **kwargs})
        return {"ok": True, "ts": f"1700000000.{len(self.calls):06d}"}


class _RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, Result]] = []
        self.repeats: list[bool] = []

    async def alert(
        self, origin: dict, result: Result, *, repeat: bool = False
    ) -> None:
        self.calls.append((origin, result))
        self.repeats.append(repeat)


class _RaisingSink:
    async def alert(
        self, origin: dict, result: Result, *, repeat: bool = False
    ) -> None:
        raise RuntimeError("sink down")


class _FlakySink:
    """Fails the first post, succeeds after — for the cooldown's retry rule."""

    def __init__(self) -> None:
        self.calls = 0

    async def alert(
        self, origin: dict, result: Result, *, repeat: bool = False
    ) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("down")


# --- body ------------------------------------------------------------------


def test_alert_body_names_the_entry() -> None:
    body = _alert_body(_origin("jira"), _result("boom"))
    assert body == "cron entry jira failed\nboom"


def test_alert_body_truncates_long_failures() -> None:
    text = "x" * (ALERT_BODY_LIMIT + 100)
    body = _alert_body(_origin(), _result(text))
    assert body.endswith("…")
    assert len(body) == len("cron entry jira failed\n") + ALERT_BODY_LIMIT + 1


def test_alert_body_names_a_daemon_without_the_cron_header() -> None:
    # A workflow matching `:x: cron` must not take a daemon failure for a cron one.
    body = _alert_body({"kind": "daemon", "entry": "slack handler"}, _result("boom"))
    assert body == "slack handler failed\nboom"


def test_alert_body_unknown_entry_degrades() -> None:
    body = _alert_body({}, _result("boom"))
    assert body.startswith("cron entry ? failed")


def test_alert_body_strips_terminal_escape_sequences() -> None:
    """A pane capture keeps the TUI's colors; a chat surface prints them raw."""
    text = (
        "Job failed: \x1b[39;49m\x1b[K  _Previous Behavior_\x1b[39m\x1b[49m\x1b[0m\n"
        "\x1b]0;codex\x07\x1b[r\x1b[21;3Hdone\x1b(B"
    )
    body = _alert_body(_origin(), _result(text))
    assert body == "cron entry jira failed\nJob failed:   _Previous Behavior_\ndone"


def test_alert_body_cleans_before_truncating() -> None:
    """Escape bytes must not spend the character budget."""
    text = "\x1b[39;49m\x1b[K" * 200 + "real reason"
    body = _alert_body(_origin(), _result(text))
    assert body == "cron entry jira failed\nreal reason"


_CODEX_BANNER = (
    "Reading additional input from stdin...\n"
    "OpenAI Codex v0.154.0\n"
    "--------\n"
    "workdir: /private/tmp/run\n"
    "model: some-model\n"
    "provider: openai\n"
    "approval: never\n"
    "sandbox: danger-full-access\n"
    "reasoning effort: medium\n"
    "reasoning summaries: none\n"
    "session id: 01a0a46e-c2ba-75d1-a343-2f91016d2f18\n"
    "--------\n"
    "user\n"
    "You are an autonomous assistant.\n"
    "Run the nightly report.\n"
)


def test_alert_body_drops_the_codex_banner_and_prompt_echo() -> None:
    """Shape captured from `codex exec` 0.154.0 failing: the banner leaks the
    workspace path, sandbox mode and session id, and the echoed prompt fills
    the whole budget before the error line is reached."""
    text = (
        "Job failed: "
        + _CODEX_BANNER
        + "warning: Model metadata for `some-model` not found.\n"
        + "hook: SessionStart\n"
        + 'ERROR: {"status":400,"message":"model is not supported"}\n'
        + 'ERROR: {"status":400,"message":"model is not supported"}'
    )
    body = _alert_body(_origin(), _result(text))
    assert body == (
        "cron entry jira failed\n"
        'Job failed:\nERROR: {"status":400,"message":"model is not supported"}'
    )
    for leak in ("workdir", "sandbox", "session id", "autonomous assistant"):
        assert leak not in body


def test_alert_body_keeps_codex_narration_without_an_error_line() -> None:
    text = "Job failed: " + _CODEX_BANNER + "codex\nI could not finish the run."
    body = _alert_body(_origin(), _result(text))
    assert body == (
        "cron entry jira failed\nJob failed:\ncodex\nI could not finish the run."
    )


def test_alert_body_banner_with_only_a_prompt_echo_points_at_the_log() -> None:
    body = _alert_body(_origin(), _result("Job failed: " + _CODEX_BANNER))
    assert body == "cron entry jira failed\nJob failed:\nSee the entry log."


def test_alert_body_keeps_leading_signals_ahead_of_the_banner() -> None:
    notes = "\n- no task_complete, last event was reasoning"
    text = f"Job failed:{notes}\n" + _CODEX_BANNER + "ERROR: stream closed"
    body = _alert_body(_origin(), _result(text))
    assert body == (
        "cron entry jira failed\n"
        "Job failed:\n- no task_complete, last event was reasoning\n"
        "ERROR: stream closed"
    )


# Captured from a failed hosted codex turn: the TUI redraw drops the line
# breaks, so the start screen, the workspace path and the error share one line.
_TUI_ERROR = (
    "Error: account/read failed during TUI bootstrap: account/read failed: "
    "workspace routing discovery timed out (code -32603)"
)
_TUI_SCREENS = [
    ">_ OpenAI Codex (v0.163.0)loading›Ask Codex to do anything?forshortcuts "
    "~/.claude-on-the-fly/workspaces/cron/sys-x_ACE-1-review-11-1991380",
    ">_ OpenAI Codex (v0.160.0)loading  Resuming session…›Ask Codex to do "
    "anything?forshortcuts ~/.claude-on-the-fly/workspaces/slack/channel/C0"
    "prmissions: YOLO mode  Resuming session…",
    ">_OpenAI Codex (v0.158.0)loadingWhatarewecookingup?›Ask Codex to do "
    "anything?forshortcuts ~/.claude-on-the-fly/workspaces/cron/sys-y  "
    "permissions: YOLO modeWhatarewecookingup?",
]


@pytest.mark.parametrize("screen", _TUI_SCREENS)
def test_alert_body_keeps_only_the_error_of_a_tui_capture(screen: str) -> None:
    body = _alert_body(_origin(), _result(f"Job failed: {screen}{_TUI_ERROR}"))
    assert body == f"cron entry jira failed\nJob failed:\n{_TUI_ERROR}"


@pytest.mark.parametrize("screen", _TUI_SCREENS)
def test_alert_body_tui_capture_without_an_error_points_at_the_log(
    screen: str,
) -> None:
    body = _alert_body(_origin(), _result(f"Job failed: {screen}"))
    assert body == "cron entry jira failed\nJob failed:\nSee the entry log."


@pytest.mark.parametrize(
    ("text", "error"),
    [
        # The pane tail keeps up to 40 rows, so the footer can follow the error.
        (
            ">_ OpenAI Codex (v0.163.0)\nError: account/read failed\n› Ask Codex"
            "\n  ~/.claude-on-the-fly/workspaces/cron/r1  permissions: YOLO mode",
            "Error: account/read failed",
        ),
        # An error that quotes another keeps its cause.
        (
            ">_ OpenAI Codex (v0.163.0)loading  permissions: YOLO mode"
            "Error: account/read failed: Error: 401 Unauthorized",
            "Error: account/read failed: Error: 401 Unauthorized",
        ),
        # The 2000-character tail can cut the screen's header off.
        (
            "Codex (v0.163.0)loading›Ask Codex to do anything?forshortcuts "
            "~/.claude-on-the-fly/workspaces/cron/r1  permissions: YOLO modeError: x",
            "Error: x",
        ),
        (
            ">_ OpenAI Codex (v0.163.0)loading  permissions: YOLO mode"
            "■ stream error: 429 Too Many Requests",
            "error: 429 Too Many Requests",
        ),
    ],
)
def test_alert_body_tui_capture_edge_shapes(text: str, error: str) -> None:
    body = _alert_body(_origin(), _result(f"Job failed: {text}"))
    assert body.endswith(f"\n{error}")
    for leak in ("workspaces", "YOLO", "Ask Codex"):
        assert leak not in body


def test_alert_body_keeps_leading_signals_ahead_of_a_tui_capture() -> None:
    notes = "\n- no task_complete, last event was reasoning"
    text = f"Job failed:{notes}\n{_TUI_SCREENS[0]}{_TUI_ERROR}"
    body = _alert_body(_origin(), _result(text))
    assert body == (
        "cron entry jira failed\n"
        f"Job failed:\n- no task_complete, last event was reasoning\n{_TUI_ERROR}"
    )


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("command exited 1", "Exited with code 1. See the entry log."),
        (
            "command exited -9 (timed out after 900s)",
            "Exited with code -9 (timed out after 900s). See the entry log.",
        ),
        ("Job failed: Exit code -1", "Exited with code -1. See the entry log."),
    ],
)
def test_alert_body_rewords_a_bare_exit_code(text: str, detail: str) -> None:
    body = _alert_body(_origin(), _result(text))
    assert body == f"cron entry jira failed\n{detail}"


def test_alert_body_with_no_failure_text_points_at_the_log() -> None:
    body = _alert_body(_origin(), _result("\x1b[0m  "))
    assert body == "cron entry jira failed\nSee the entry log."


def test_alert_body_leaves_an_exit_code_with_more_text_alone() -> None:
    body = _alert_body(_origin(), _result("command exited 1\nmore"))
    assert body == "cron entry jira failed\ncommand exited 1\nmore"


# --- Slack -----------------------------------------------------------------


async def test_slack_sink_posts_one_compact_message() -> None:
    client = _FakeSlackClient()
    sink = SlackAlertSink(client, "C42")

    await sink.alert(_origin(), _result("boom"))

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["channel"] == "C42"
    assert call["text"] == ":x: cron entry jira failed"
    assert call["blocks"][0]["text"] == ":x: cron entry jira failed\nboom"


async def test_slack_alert_keeps_the_plain_cron_prefix_for_workflow_matching() -> None:
    """An operator's Slack workflow matches `:x: cron` at the start of the
    alert, so nothing may sit between the emoji and the word."""
    client = _FakeSlackClient()
    sink = SlackAlertSink(client, "C42")

    await sink.alert(_origin("nightly-report"), _result("command exited 1"))

    call = client.calls[0]
    assert call["text"].startswith(":x: cron entry nightly-report failed")
    assert call["blocks"][0]["text"].startswith(":x: cron entry nightly-report failed")


async def test_slack_repeat_replies_in_the_first_alerts_thread() -> None:
    client = _FakeSlackClient()
    sink = SlackAlertSink(client, "C42")

    await sink.alert(_origin(), _result("boom"))
    await sink.alert(_origin(), _result("boom again"), repeat=True)

    first, again = client.calls
    assert "thread_ts" not in first
    assert again["channel"] == "C42"
    assert again["thread_ts"] == "1700000000.000001"
    assert again["blocks"][0]["text"] == "Failed again.\nboom again"


async def test_slack_repeat_does_not_start_with_the_alert_prefix() -> None:
    """A repeat is a reply, not a new alert: a workflow matching `:x: cron`
    must not take it for one."""
    client = _FakeSlackClient()
    sink = SlackAlertSink(client, "C42")

    await sink.alert(_origin(), _result("boom"))
    await sink.alert(_origin(), _result("boom"), repeat=True)

    assert not client.calls[1]["text"].startswith(":x:")


async def test_slack_repeat_without_a_known_thread_posts_a_full_alert() -> None:
    """After a restart the first alert's ts is gone; the repeat must still
    reach the channel, as a full alert."""
    client = _FakeSlackClient()
    sink = SlackAlertSink(client, "C42")

    await sink.alert(_origin(), _result("boom"), repeat=True)

    call = client.calls[0]
    assert "thread_ts" not in call
    assert call["text"] == ":x: cron entry jira failed"


async def test_slack_repeat_threads_per_entry() -> None:
    client = _FakeSlackClient()
    sink = SlackAlertSink(client, "C42")

    await sink.alert(_origin("jira"), _result("boom"))
    await sink.alert(_origin("prune"), _result("boom"))
    await sink.alert(_origin("jira"), _result("boom"), repeat=True)

    assert client.calls[2]["thread_ts"] == "1700000000.000001"


# --- Telegram --------------------------------------------------------------


async def test_telegram_sink_posts_to_the_bot_api() -> None:
    requests: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append({"url": str(request.url), "body": request.content})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    sink = TelegramAlertSink("bot-token", "123", client)

    await sink.alert(_origin(), _result("boom"))

    assert len(requests) == 1
    assert requests[0]["url"] == "https://api.telegram.org/botbot-token/sendMessage"
    payload = json.loads(requests[0]["body"])
    assert payload["chat_id"] == "123"
    assert payload["parse_mode"] == "Markdown"
    assert payload["text"] == "cron entry jira failed\nboom"


async def test_telegram_sink_escapes_the_entry_and_the_detail() -> None:
    requests: list[bytes] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    sink = TelegramAlertSink("bot-token", "123", client)

    await sink.alert(_origin("nightly_report"), _result("a_b"))

    payload = json.loads(requests[0])
    assert payload["text"] == "cron entry nightly\\_report failed\na\\_b"


async def test_telegram_sink_raises_on_an_error_response() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    sink = TelegramAlertSink("bot-token", "123", client)

    with pytest.raises(httpx.HTTPStatusError):
        await sink.alert(_origin(), _result("boom"))


async def test_telegram_repeat_replies_to_the_first_alert() -> None:
    payloads: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    sink = TelegramAlertSink("bot-token", "123", client)

    await sink.alert(_origin(), _result("boom"))
    await sink.alert(_origin(), _result("boom"), repeat=True)

    assert "reply_to_message_id" not in payloads[0]
    assert payloads[1]["reply_to_message_id"] == 77
    assert payloads[1]["text"] == "Failed again.\nboom"


# --- cooldown --------------------------------------------------------------


async def test_cooldown_marks_repeats_within_the_window() -> None:
    """A repeat inside the window is still delivered, marked as a repeat, so
    the sink can reply under the first alert instead of posting a new one."""
    inner = _RecordingSink()
    sink = CooldownAlertSink(inner, window_s=60.0)

    await sink.alert(_origin(), _result("boom"))
    await sink.alert(_origin(), _result("boom"))

    assert inner.repeats == [False, True]


async def test_cooldown_repeat_does_not_extend_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window runs from the last full alert. A repeat must not push it
    forward, or a job failing every 20 minutes would never alert again."""
    # The patch reaches the event loop's clock too, so it must stay callable.
    now = [1000.0]
    monkeypatch.setattr("claude_on_the_fly.jobs.alerts.time.monotonic", lambda: now[0])
    inner = _RecordingSink()
    sink = CooldownAlertSink(inner, window_s=60.0)

    for now[0] in (1000.0, 1040.0, 1070.0):
        await sink.alert(_origin(), _result("boom"))

    assert inner.repeats == [False, True, False]


async def test_cooldown_first_alert_fires_on_a_young_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh sink must alert even when the machine has been up less than
    the window. `now - 0.0 < window` would swallow the FIRST alert on a
    young host (a fresh CI VM) because a never-alerted entry must not
    compare as an ancient timestamp."""
    monkeypatch.setattr("claude_on_the_fly.jobs.alerts.time.monotonic", lambda: 120.0)
    inner = _RecordingSink()
    sink = CooldownAlertSink(inner, window_s=1800.0)

    await sink.alert(_origin(), _result("boom"))

    assert len(inner.calls) == 1


async def test_cooldown_is_per_entry() -> None:
    inner = _RecordingSink()
    sink = CooldownAlertSink(inner, window_s=60.0)

    await sink.alert(_origin("jira"), _result("boom"))
    await sink.alert(_origin("prune"), _result("boom"))

    assert len(inner.calls) == 2


async def test_cooldown_does_not_record_a_failed_post() -> None:
    """A sink that is down must not silence the next attempt: the window is
    recorded only after a successful post."""
    inner = _FlakySink()
    sink = CooldownAlertSink(inner, window_s=60.0)

    with pytest.raises(RuntimeError):
        await sink.alert(_origin(), _result("boom"))
    await sink.alert(_origin(), _result("boom"))

    assert inner.calls == 2


# --- multi -----------------------------------------------------------------


async def test_multi_fans_out_to_every_sink() -> None:
    a, b = _RecordingSink(), _RecordingSink()
    sink = MultiAlertSink([a, b])

    await sink.alert(_origin(), _result("boom"))

    assert len(a.calls) == 1
    assert len(b.calls) == 1


async def test_multi_guards_each_sink() -> None:
    a, b = _RaisingSink(), _RecordingSink()
    sink = MultiAlertSink([a, b])

    await sink.alert(_origin(), _result("boom"))

    assert len(b.calls) == 1, "one platform down must not lose the other's alert"


async def test_multi_raises_when_every_sink_failed() -> None:
    sink = MultiAlertSink([_RaisingSink(), _RaisingSink()])

    with pytest.raises(RuntimeError, match="all 2 alert sink"):
        await sink.alert(_origin(), _result("boom"))


# --- cron-only -------------------------------------------------------------


async def test_cron_origin_alerts_cron_failures_only() -> None:
    inner = _RecordingSink()
    sink = CronOriginAlertSink(inner)

    await sink.alert(_origin(), _result("boom"))
    await sink.alert({"kind": "slack", "channel": "C1"}, _result("boom"))
    await sink.alert({}, _result("boom"))

    assert len(inner.calls) == 1


async def test_wrappers_pass_the_repeat_mark_through() -> None:
    inner = _RecordingSink()
    sink = CronOriginAlertSink(MultiAlertSink([inner]))

    await sink.alert(_origin(), _result("boom"), repeat=True)

    assert inner.repeats == [True]


# --- factory ---------------------------------------------------------------


def test_factory_returns_none_without_targets() -> None:
    assert build_alert_sink({}) is None


def test_factory_builds_a_slack_sink(monkeypatch) -> None:
    built: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)

    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", _FakeClient)

    sink = build_alert_sink({"SLACK_ALERT_TARGET": "C42", "SLACK_TOKEN": "xoxb-t"})

    assert sink is not None
    assert built == [{"token": "xoxb-t"}]


def test_factory_builds_a_telegram_sink() -> None:
    sink = build_alert_sink(
        {"TELEGRAM_ALERT_TARGET": "123", "TELEGRAM_BOT_TOKEN": "bot-t"}
    )

    assert isinstance(sink, CooldownAlertSink)


def test_factory_builds_both_platforms(monkeypatch) -> None:
    monkeypatch.setattr(
        "slack_sdk.web.async_client.AsyncWebClient", lambda **kwargs: object()
    )

    sink = build_alert_sink(
        {
            "SLACK_ALERT_TARGET": "C42",
            "SLACK_TOKEN": "xoxb-t",
            "TELEGRAM_ALERT_TARGET": "123",
            "TELEGRAM_BOT_TOKEN": "bot-t",
        }
    )

    assert isinstance(sink, CooldownAlertSink)


async def test_factory_drops_non_cron_origins_by_default(monkeypatch) -> None:
    client = _FakeSlackClient()
    monkeypatch.setattr(
        "slack_sdk.web.async_client.AsyncWebClient", lambda **kwargs: client
    )
    env = {"SLACK_ALERT_TARGET": "C42", "SLACK_TOKEN": "xoxb-t"}
    daemon = {"kind": "daemon", "entry": "slack handler"}

    await build_alert_sink(env).alert(daemon, _result())
    assert client.calls == []

    await build_alert_sink(env, cron_only=False).alert(daemon, _result())
    assert [call["text"] for call in client.calls] == [":x: slack handler failed"]


def test_factory_skips_a_target_without_a_token(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert build_alert_sink({"SLACK_ALERT_TARGET": "C42"}) is None
        assert build_alert_sink({"TELEGRAM_ALERT_TARGET": "123"}) is None
    assert "SLACK_ALERT_TARGET" in caplog.text
    assert "TELEGRAM_ALERT_TARGET" in caplog.text
