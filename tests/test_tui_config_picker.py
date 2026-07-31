"""The config picker modal: pick which file to edit, or back out.

`dismiss(None)` and `dismiss(<id>)` are two different answers to the caller, so
cancelling must not look like a selection.
"""

from __future__ import annotations

from textual.app import App
from textual.widgets import OptionList

from claude_on_the_fly.tui.screens.config_picker import ConfigPickerScreen


class _Host(App):
    pass


async def test_selecting_an_option_returns_its_id():
    """The first row is .env, because a deployment cannot run without credentials and
    that is what someone opening this is most likely to want."""
    app = _Host()
    async with app.run_test() as pilot:
        answers: list[object] = []
        await app.push_screen(ConfigPickerScreen(), answers.append)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert answers == ["env"]


async def test_the_second_option_is_reachable():
    app = _Host()
    async with app.run_test() as pilot:
        answers: list[object] = []
        await app.push_screen(ConfigPickerScreen(), answers.append)
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert answers == ["sandbox"]


async def test_escape_returns_nothing():
    """Not the same as picking the highlighted option: the caller opens $EDITOR on
    whatever comes back."""
    app = _Host()
    async with app.run_test() as pilot:
        answers: list[object] = []
        await app.push_screen(ConfigPickerScreen(), answers.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert answers == [None]


async def test_the_list_takes_focus_so_arrow_keys_work():
    app = _Host()
    async with app.run_test() as pilot:
        await app.push_screen(ConfigPickerScreen())
        await pilot.pause()
        assert app.screen.query_one(OptionList).has_focus


async def test_the_last_option_is_reachable():
    app = _Host()
    async with app.run_test() as pilot:
        answers: list[object] = []
        await app.push_screen(ConfigPickerScreen(), answers.append)
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert answers == ["cron"]


async def test_the_order_follows_how_a_deployment_is_set_up():
    """Credentials, then what the agent may reach and do, then the optional scheduled
    work. Alphabetical or newest-last would both read as arbitrary."""
    app = _Host()
    async with app.run_test() as pilot:
        await app.push_screen(ConfigPickerScreen())
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        ids = [options.get_option_at_index(i).id for i in range(options.option_count)]
    assert ids == ["env", "sandbox", "cron"]


async def test_every_option_names_the_file_it_edits():
    """The picker is the only place an operator learns these files exist, so a label
    that does not name one is a dead end."""
    app = _Host()
    async with app.run_test() as pilot:
        await app.push_screen(ConfigPickerScreen())
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        labels = [
            str(options.get_option_at_index(i).prompt)
            for i in range(options.option_count)
        ]
    assert any("cron.yaml" in label for label in labels)
    assert any(".env" in label for label in labels)
    assert any("sandbox.yaml" in label for label in labels)
