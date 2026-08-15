"""Failure-alert delivery: where a failed job's outcome goes when its origin
has no live thread to reply into.

Two daemons detect failures — the worker (a job's `Result.ok` is False) and
the cron producer (a command or producer exited non-zero) — and both deliver
through this module, so one platform sender is written once and shared. The
sinks are adapters: they read `origin` (the entry name) and `result` (the
failure text) and post a compact alert to a configured monitoring surface.

Nothing here is a delivery contract. A failed alert is logged by the caller
and forgotten; the durable record of a failure is the entry's log and the
key-state file, and the alert is only the operator's heads-up.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx
from telegram.helpers import escape_markdown

from claude_on_the_fly.jobs.core import Result
from claude_on_the_fly.jobs.slack_notifier import (
    _blocks,
    _fallback_text,
    _PostsMessages,
)
from claude_on_the_fly.slack_mrkdwn import to_mrkdwn

logger = logging.getLogger(__name__)

# How long a failed entry stays quiet before it can alert again. In-memory on
# purpose: the durable record of a failure is the entry's log and the
# key-state file, and the alert is only the operator's heads-up. A restart
# resets the memory, which is fine — the next failure alerts again.
ALERT_COOLDOWN_S = 1800.0
# The alert is a heads-up, not the report. The full failure text lives in the
# entry's log (and the origin thread, when there is one); this is the first
# lines, enough to say what broke.
ALERT_BODY_LIMIT = 500
# A dead network must not hang a daemon on an alert that is not a delivery.
TELEGRAM_API_TIMEOUT_S = 10.0


def _alert_body(origin: Mapping[str, Any], result: Result) -> str:
    """The alert's text: a header naming the entry, then the failure's first
    lines. The header is the part that must survive truncation."""
    entry = origin.get("entry") or "?"
    body = result.text.strip()
    if len(body) > ALERT_BODY_LIMIT:
        body = body[:ALERT_BODY_LIMIT].rstrip() + "…"
    return f"cron entry {entry} failed\n{body}"


class SlackAlertSink:
    """Posts a failure alert to a configured Slack channel.

    Same client shape as `SlackThreadNotifier` (a `chat_postMessage`-capable
    client), so the worker's existing client serves both.
    """

    def __init__(self, client: _PostsMessages, channel: str) -> None:
        self._client = client
        self._channel = channel

    async def alert(self, origin: Mapping[str, Any], result: Result) -> None:
        body = ":x: " + _alert_body(origin, result)
        await self._client.chat_postMessage(
            channel=self._channel,
            text=_fallback_text(body),
            blocks=_blocks(to_mrkdwn(body)),
        )


class TelegramAlertSink:
    """Posts a failure alert to a configured Telegram chat via the Bot API.

    A raw `sendMessage` call rather than a python-telegram-bot Application: the
    frontend needs the full framework (polling, callbacks), an alert only needs
    one POST. `client` is injected for tests; the factory builds the real one.
    """

    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._client = client

    async def alert(self, origin: Mapping[str, Any], result: Result) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": escape_markdown(_alert_body(origin, result), version=1),
            "parse_mode": "Markdown",
        }
        response = await self._client.post(
            f"https://api.telegram.org/bot{self._token}/sendMessage", json=payload
        )
        response.raise_for_status()


class CooldownAlertSink:
    """Suppresses repeat alerts for the same entry within `window_s`.

    A failing entry fires on its own schedule, and without this the alert
    channel would hear about the same failure at every fire. The window is
    recorded only after a successful post, so a sink that is down retries on
    the next failure instead of being silenced by its own outage.
    """

    def __init__(self, inner: Any, window_s: float = ALERT_COOLDOWN_S) -> None:
        self._inner = inner
        self._window_s = window_s
        self._last_alerted_at: dict[str, float] = {}

    async def alert(self, origin: Mapping[str, Any], result: Result) -> None:
        entry = str(origin.get("entry") or "")
        now = time.monotonic()
        if now - self._last_alerted_at.get(entry, 0.0) < self._window_s:
            return
        await self._inner.alert(origin, result)
        self._last_alerted_at[entry] = now


class MultiAlertSink:
    """Fans an alert out to every configured sink, each guarded.

    One platform being down must not lose the other's alert. If every sink
    failed, the alert did not get out, and raising is what keeps the cooldown
    from silencing the next attempt.
    """

    def __init__(self, sinks: list[Any]) -> None:
        self._sinks = sinks

    async def alert(self, origin: Mapping[str, Any], result: Result) -> None:
        failures = 0
        for sink in self._sinks:
            try:
                await sink.alert(origin, result)
            except Exception:
                failures += 1
                logger.exception("alerts: one sink failed; continuing")
        if failures == len(self._sinks):
            raise RuntimeError(f"all {failures} alert sink(s) failed")


class CronOriginAlertSink:
    """Alerts only cron-origin failures.

    A Slack-origin job's failure already replies in its thread, where the
    requester is watching; alerting it to the channel too would duplicate.
    Cron-origin failures have no live thread — the entry's log is the only
    record — so those are the ones worth a heads-up.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def alert(self, origin: Mapping[str, Any], result: Result) -> None:
        if str(origin.get("kind") or "") != "cron":
            return
        await self._inner.alert(origin, result)


def build_alert_sink(env: Mapping[str, str]) -> Any | None:
    """The configured alert sink, or None when no target is configured.

    Opt-in: with neither `slack.alert_target` nor `telegram.alert_target` set,
    nothing is built and failures stay where they always were (the entry's
    log). A target with no token is skipped with a warning rather than built
    broken — the doctor check names the same condition.
    """
    sinks: list[Any] = []
    channel = env.get("SLACK_ALERT_TARGET", "").strip()
    if channel:
        from claude_on_the_fly.checks import resolve_jobs_token

        _, token = resolve_jobs_token(env)
        if token:
            from slack_sdk.web.async_client import AsyncWebClient

            sinks.append(SlackAlertSink(AsyncWebClient(token=token), channel))
        else:
            logger.warning(
                "alerts: SLACK_ALERT_TARGET is set but no Slack token is; "
                "set JOBS_SLACK_TOKEN or SLACK_TOKEN"
            )
    chat = env.get("TELEGRAM_ALERT_TARGET", "").strip()
    if chat:
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if token:
            sinks.append(
                TelegramAlertSink(
                    token, chat, httpx.AsyncClient(timeout=TELEGRAM_API_TIMEOUT_S)
                )
            )
        else:
            logger.warning(
                "alerts: TELEGRAM_ALERT_TARGET is set but TELEGRAM_BOT_TOKEN is not"
            )
    if not sinks:
        return None
    return CooldownAlertSink(CronOriginAlertSink(MultiAlertSink(sinks)))
