"""The env-diff modal: what it shows after $EDITOR exits, and what it restarts.

The screen exists because editing `.env` is only half the job — the running daemons
still hold the old values. Two things matter: a token must never appear in
plaintext on screen, and the restart must be offered only for daemons that are
actually running and actually affected.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button

from claude_on_the_fly.tui import supervisor
from claude_on_the_fly.tui.env_editor import EnvDiff
from claude_on_the_fly.tui.screens import env_diff as env_diff_mod
from claude_on_the_fly.tui.screens.env_diff import EnvDiffScreen, _diff_table, _redact


class TestRedact:
    def test_a_short_value_is_shown_as_is(self):
        """Below the threshold there is nothing to hide, and a redacted "1" is just
        confusing."""
        assert _redact("dm") == "dm"
        assert _redact("123456") == "123456"

    def test_a_token_shows_only_its_ends_and_its_length(self):
        """Enough to recognise which token it is, not enough to use it. The length is
        what catches a paste that picked up a trailing newline."""
        out = _redact("xoxb-1234567890-abcdefghij")
        assert out.startswith("xox")
        assert out.endswith("hij (26 chars)")
        assert "1234567890" not in out


class TestDiffTable:
    def test_every_change_kind_gets_a_row(self):
        table = _diff_table(
            EnvDiff(
                added={"NEW_KEY": "v"},
                removed={"OLD_KEY": "v"},
                changed={"MOVED": ("before-value-long", "after-value-long")},
            )
        )
        assert table.row_count == 3

    def test_changed_values_are_redacted_on_both_sides(self):
        """The old value is a live credential until every daemon restarts, so it is
        no safer to print than the new one."""
        table = _diff_table(
            EnvDiff(
                changed={"SLACK_TOKEN": ("xoxb-old-secret-value", "xoxb-new-secret")}
            )
        )
        rendered = "".join(
            str(cell) for column in table.columns for cell in column._cells
        )
        assert "old-secret-value" not in rendered
        assert "new-secret" not in rendered

    def test_an_empty_diff_makes_an_empty_table(self):
        assert _diff_table(EnvDiff()).row_count == 0


class _Host(App):
    """Minimal app to push the modal onto, so the screen runs against real Textual
    rather than a mocked widget tree."""

    def compose(self) -> ComposeResult:
        return []


async def _screen(diff: EnvDiff):
    app = _Host()
    async with app.run_test() as pilot:
        screen = EnvDiffScreen(diff)
        await app.push_screen(screen)
        await pilot.pause()
        yield app, screen, pilot


class TestAffectedLine:
    async def test_a_diff_touching_no_declared_var_says_there_is_nothing_to_do(self):
        screen = EnvDiffScreen(EnvDiff(added={"MY_OWN_NOTE": "x"}))
        assert "nothing to restart" in str(screen._affected_line())

    async def test_affected_but_not_running_says_so(self, monkeypatch):
        """Restarting a stopped daemon is not what the operator asked for, and
        offering it reads as though it were running."""
        monkeypatch.setattr(supervisor, "is_running", lambda _n: False)
        screen = EnvDiffScreen(EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}))
        line = str(screen._affected_line())
        assert "slack" in line
        assert "none running" in line

    async def test_a_running_affected_daemon_is_named_for_restart(self, monkeypatch):
        monkeypatch.setattr(supervisor, "is_running", lambda n: n == "slack")
        screen = EnvDiffScreen(EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}))
        line = str(screen._affected_line())
        assert "Would restart: slack" in line
        # jobs also declares SLACK_TOKEN but is not running, so it is not offered.
        assert "jobs" not in line


class TestComposeOffersRestartOnlyWhenItCanHelp:
    async def test_no_restart_button_when_nothing_is_affected(self):
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(EnvDiffScreen(EnvDiff(added={"MY_OWN_NOTE": "x"})))
            await pilot.pause()
            ids = {b.id for b in app.screen.query(Button)}
        assert ids == {"cancel"}

    async def test_a_restart_button_appears_when_a_daemon_is_affected(
        self, monkeypatch
    ):
        monkeypatch.setattr(supervisor, "is_running", lambda _n: True)
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(
                EnvDiffScreen(EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}))
            )
            await pilot.pause()
            ids = {b.id for b in app.screen.query(Button)}
        assert ids == {"restart", "cancel"}


class TestRestartAction:
    async def test_cancel_dismisses_without_restarting(self, monkeypatch):
        restarts: list[str] = []
        monkeypatch.setattr(supervisor, "is_running", lambda _n: True)
        monkeypatch.setattr(
            supervisor, "restart", lambda name, **_kw: restarts.append(name)
        )
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(
                EnvDiffScreen(EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}))
            )
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
        assert restarts == []

    async def test_the_cancel_button_dismisses_without_restarting(self, monkeypatch):
        """Separate from the key binding: the button goes through on_button_pressed,
        which routes anything that is not the restart button to a plain dismiss."""
        restarts: list[str] = []
        monkeypatch.setattr(supervisor, "is_running", lambda _n: True)
        monkeypatch.setattr(
            supervisor, "restart", lambda name, **_kw: restarts.append(name)
        )
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(
                EnvDiffScreen(EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}))
            )
            await pilot.pause()
            await pilot.click("#cancel")
            await pilot.pause()
        assert restarts == []

    async def test_pressing_y_restarts_every_running_affected_daemon(self, monkeypatch):
        restarts: list[str] = []
        monkeypatch.setattr(supervisor, "is_running", lambda _n: True)

        def restart(name, **_kw):
            restarts.append(name)
            return 4242

        monkeypatch.setattr(supervisor, "restart", restart)
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(
                EnvDiffScreen(EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}))
            )
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
        # SLACK_TOKEN is declared by the jobs notifier too, so both are affected.
        assert restarts == ["jobs", "slack"]

    async def test_the_restart_button_does_the_same_as_the_key(self, monkeypatch):
        restarts: list[str] = []
        monkeypatch.setattr(supervisor, "is_running", lambda _n: True)
        monkeypatch.setattr(
            supervisor, "restart", lambda name, **_kw: (restarts.append(name), 1)[1]
        )
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(
                EnvDiffScreen(EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}))
            )
            await pilot.pause()
            await pilot.click("#restart")
            await pilot.pause()
        assert restarts == ["jobs", "slack"]

    async def test_nothing_running_notifies_instead_of_restarting(self, monkeypatch):
        monkeypatch.setattr(supervisor, "is_running", lambda _n: False)
        screen = EnvDiffScreen(EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}))
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            await pilot.pause()
            notices: list[tuple] = []
            app.notify = lambda msg, **kw: notices.append((msg, kw))  # type: ignore[method-assign]
            screen.action_restart()
            await pilot.pause()
        assert notices
        assert "No running daemons" in notices[0][0]

    @pytest.mark.parametrize(
        ("error", "fragment"),
        [
            (
                supervisor.PreflightFailed("slack", []),
                "preflight failed",
            ),
            (
                supervisor.SpawnTimeout(
                    frontend="slack", pid=1, log_path=env_diff_mod.supervisor.STATE_DIR
                ),
                "spawn timeout",
            ),
            (RuntimeError("disk full"), "restart failed"),
        ],
    )
    async def test_a_restart_failure_is_reported_and_the_modal_still_closes(
        self, monkeypatch, error, fragment
    ):
        """One daemon failing must not leave the modal stuck: the operator has already
        answered the question it was asking."""
        monkeypatch.setattr(supervisor, "is_running", lambda _n: True)
        monkeypatch.setattr(
            supervisor,
            "restart",
            lambda _name, **_kw: (_ for _ in ()).throw(error),
        )
        screen = EnvDiffScreen(EnvDiff(changed={"SLACK_TOKEN": ("a", "b")}))
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            await pilot.pause()
            notices: list[tuple] = []
            app.notify = lambda msg, **kw: notices.append((msg, kw))  # type: ignore[method-assign]
            screen.action_restart()
            await pilot.pause()
        assert any(fragment in msg for msg, _kw in notices), notices
        assert all(kw.get("severity") == "error" for _msg, kw in notices)
