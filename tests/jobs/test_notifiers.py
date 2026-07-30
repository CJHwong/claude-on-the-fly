"""Routing and log notifiers: where a finished job's reply actually goes.

Both failure paths here raise on purpose. Returning normally is what marks a result
delivered, so a result with nowhere to go has to stay in `undelivered()` — visible
as a stuck reply rather than silently discarded.
"""

from __future__ import annotations

import pytest

from claude_on_the_fly.jobs.core import Result
from claude_on_the_fly.jobs.notifiers import LogNotifier, RoutingNotifier


async def test_a_cron_result_is_appended_to_its_entry_log() -> None:
    appended: list[tuple[str, str]] = []
    notifier = LogNotifier(lambda entry, text: appended.append((entry, text)))
    await notifier.notify(
        {"kind": "cron", "entry": "nightly"}, Result(ok=True, text="fine")
    )
    entry, text = appended[0]
    assert entry == "nightly"
    assert "reply (done)" in text
    assert "fine" in text


async def test_a_failed_cron_result_is_labelled_failed() -> None:
    appended: list[tuple[str, str]] = []
    notifier = LogNotifier(lambda entry, text: appended.append((entry, text)))
    await notifier.notify(
        {"kind": "cron", "entry": "nightly"}, Result(ok=False, text="boom")
    )
    assert "reply (FAILED)" in appended[0][1]


async def test_a_cron_origin_with_no_entry_raises() -> None:
    notifier = LogNotifier(lambda _entry, _text: None)
    with pytest.raises(ValueError, match="no 'entry' to log against"):
        await notifier.notify({"kind": "cron"}, Result(ok=True, text="x"))


async def test_the_router_dispatches_by_origin_kind() -> None:
    """One worker drains both producers, so delivery fans back out by where the job
    came from."""
    seen: list[str] = []

    class Recording:
        def __init__(self, label: str) -> None:
            self._label = label

        async def notify(self, _origin: dict, _result: Result) -> None:
            seen.append(self._label)

    router = RoutingNotifier({"slack": Recording("slack"), "cron": Recording("cron")})
    await router.notify({"kind": "cron"}, Result(ok=True, text="x"))
    await router.notify({"kind": "slack"}, Result(ok=True, text="x"))
    assert seen == ["cron", "slack"]


async def test_an_unknown_kind_raises_and_names_the_known_ones() -> None:
    """A typo'd kind is then visible as a stuck reply instead of a discarded one, and
    the message says what it should have been."""
    router = RoutingNotifier({"slack": object()})
    with pytest.raises(ValueError, match="known kinds: \\['slack'\\]"):
        await router.notify({"kind": "slak"}, Result(ok=True, text="x"))
