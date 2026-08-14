"""The upgrade modal: what it tells the operator before they agree.

An upgrade stops every daemon at once, and the chat turns it stops are gone for
good. So the number that cannot be recovered has to be on the screen, and the
default answer has to be no.
"""

from __future__ import annotations

from textual.app import App
from textual.widgets import Button, Static

from claude_on_the_fly.tui.screens.upgrade import UpgradeScreen
from claude_on_the_fly.tui.supervisor import PendingWork
from claude_on_the_fly.upgrade import Plan

PLAN = Plan(command="git pull --ff-only && uv sync", source="git checkout at /src")


class _Host(App):
    pass


async def _pending_text(pending: list[PendingWork]) -> str:
    app = _Host()
    async with app.run_test() as pilot:
        screen = UpgradeScreen(PLAN, pending)
        await app.push_screen(screen)
        await pilot.pause()
        return str(screen.query_one("#upgrade-pending", Static).content)


async def test_the_command_and_its_source_are_both_shown():
    """The command runs in a shell. The operator sees it before it does."""
    app = _Host()
    async with app.run_test() as pilot:
        screen = UpgradeScreen(PLAN, [])
        await app.push_screen(screen)
        await pilot.pause()
        rendered = " ".join(
            str(node.content) for node in screen.query(Static).results()
        )

    assert "git pull --ff-only && uv sync" in rendered
    assert "git checkout at /src" in rendered


async def test_an_idle_deployment_says_the_upgrade_costs_nobody_an_answer():
    assert "costs nobody an answer" in await _pending_text([])


async def test_unrecoverable_work_is_counted_separately():
    """Queued jobs come back; chat turns do not. One total would hide that."""
    text = await _pending_text(
        [
            PendingWork("slack", running=1, queued=1, recoverable=False),
            PendingWork("jobs", running=1, queued=5, recoverable=True),
        ]
    )

    assert "slack: 1 running, 1 queued" in text
    assert "jobs: 1 running, 5 queued" in text
    assert "2 of those are lost for good" in text


async def test_recoverable_work_alone_raises_no_alarm():
    text = await _pending_text(
        [PendingWork("jobs", running=1, queued=0, recoverable=True)]
    )

    assert "lost for good" not in text


async def test_y_confirms_and_n_cancels():
    for key, expected in (("y", True), ("n", False)):
        app = _Host()
        async with app.run_test() as pilot:
            answers: list[bool | None] = []
            await app.push_screen(UpgradeScreen(PLAN, []), answers.append)
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()

        assert answers == [expected]


async def test_escape_cancels():
    app = _Host()
    async with app.run_test() as pilot:
        answers: list[bool | None] = []
        await app.push_screen(UpgradeScreen(PLAN, []), answers.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert answers == [False]


async def test_only_the_upgrade_button_confirms():
    for button_id, expected in (("confirm", True), ("cancel", False)):
        app = _Host()
        async with app.run_test() as pilot:
            answers: list[bool | None] = []
            screen = UpgradeScreen(PLAN, [])
            await app.push_screen(screen, answers.append)
            await pilot.pause()
            screen.query_one(f"#{button_id}", Button).press()
            await pilot.pause()

        assert answers == [expected]
