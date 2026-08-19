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
        return {"ok": True}


class _RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, Result]] = []

    async def alert(self, origin: dict, result: Result) -> None:
        self.calls.append((origin, result))


class _RaisingSink:
    async def alert(self, origin: dict, result: Result) -> None:
        raise RuntimeError("sink down")


class _FlakySink:
    """Fails the first post, succeeds after — for the cooldown's retry rule."""

    def __init__(self) -> None:
        self.calls = 0

    async def alert(self, origin: dict, result: Result) -> None:
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


def test_alert_body_unknown_entry_degrades() -> None:
    body = _alert_body({}, _result("boom"))
    assert body.startswith("cron entry ? failed")


# --- Slack -----------------------------------------------------------------


async def test_slack_sink_posts_one_compact_message() -> None:
    client = _FakeSlackClient()
    sink = SlackAlertSink(client, "C42")

    await sink.alert(_origin(), _result("boom"))

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["channel"] == "C42"
    assert call["text"] == ":x: cron entry jira failed"
    assert call["blocks"][0]["text"].startswith(":x: cron entry jira failed\nboom")


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


async def test_telegram_sink_raises_on_an_error_response() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    sink = TelegramAlertSink("bot-token", "123", client)

    with pytest.raises(httpx.HTTPStatusError):
        await sink.alert(_origin(), _result("boom"))


# --- cooldown --------------------------------------------------------------


async def test_cooldown_suppresses_repeats_within_the_window() -> None:
    inner = _RecordingSink()
    sink = CooldownAlertSink(inner, window_s=60.0)

    await sink.alert(_origin(), _result("boom"))
    await sink.alert(_origin(), _result("boom"))

    assert len(inner.calls) == 1


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


def test_factory_skips_a_target_without_a_token(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert build_alert_sink({"SLACK_ALERT_TARGET": "C42"}) is None
        assert build_alert_sink({"TELEGRAM_ALERT_TARGET": "123"}) is None
    assert "SLACK_ALERT_TARGET" in caplog.text
    assert "TELEGRAM_ALERT_TARGET" in caplog.text
