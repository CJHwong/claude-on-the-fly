"""The doctor screen: re-runs the checks and shows every fix hint.

The load-bearing behaviour is that it re-runs on *resume*, not only on mount. The
screen is registered in App.SCREENS, so Textual builds one instance and re-pushes
it; showing the verdict from last time is worse than showing nothing, because you
fix what it complained about, re-open it, and it tells you the fix did not work.
"""

from __future__ import annotations

from textual.app import App
from textual.widgets import Static

from claude_on_the_fly.checks import CheckResult
from claude_on_the_fly.tui.screens import doctor as doctor_mod
from claude_on_the_fly.tui.screens.doctor import DoctorScreen, _group_table


def _result(name, status, detail="", hint=""):
    return CheckResult(name=name, status=status, detail=detail, fix_hint=hint)


class _Host(App):
    CSS = "#overlay-box { height: 1fr; }"


class TestGroupTable:
    def test_every_result_gets_a_row_with_its_hint(self):
        table = _group_table(
            "slack",
            [
                _result("SLACK_TOKEN", "missing", "not set", "add it to .env"),
                _result("jq", "ok", "/usr/bin/jq"),
            ],
        )
        assert table.row_count == 2
        rendered = "".join(
            str(cell) for column in table.columns for cell in column._cells
        )
        assert "add it to .env" in rendered

    def test_a_result_with_no_hint_leaves_the_column_empty(self):
        """Better than inventing advice for a check that has none."""
        table = _group_table("slack", [_result("jq", "ok", "/usr/bin/jq")])
        fix_column = list(table.columns)[-1]
        assert list(fix_column._cells) == [""]

    def test_an_unknown_status_still_renders(self):
        """The status set can grow; an unstyled cell beats a KeyError on open."""
        table = _group_table("slack", [_result("new", "surprising")])
        assert table.row_count == 1

    def test_an_empty_group_makes_an_empty_table(self):
        assert _group_table("slack", []).row_count == 0


class TestReRunsOnResume:
    async def test_opening_the_screen_runs_the_checks(self, monkeypatch, tmp_path):
        runs = {"n": 0}

        def check_all(_env):
            runs["n"] += 1
            return {"slack": [_result("SLACK_TOKEN", "ok", "set")]}

        monkeypatch.setattr(doctor_mod.checks, "check_all", check_all)
        monkeypatch.setattr(
            doctor_mod.supervisor, "DEFAULT_ENV_FILE", tmp_path / ".env"
        )
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(DoctorScreen())
            await pilot.pause()
            content = str(app.screen.query_one("#doctor-content", Static).content)
        assert runs["n"] == 1
        assert content

    async def test_re_pushing_the_same_instance_re_runs_them(
        self, monkeypatch, tmp_path
    ):
        """The regression this exists for: one instance, re-pushed, so `on_mount`
        fires only the first time."""
        runs = {"n": 0}
        monkeypatch.setattr(
            doctor_mod.checks,
            "check_all",
            lambda _env: (runs.__setitem__("n", runs["n"] + 1), {"slack": []})[1],
        )
        monkeypatch.setattr(
            doctor_mod.supervisor, "DEFAULT_ENV_FILE", tmp_path / ".env"
        )
        screen = DoctorScreen()
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(screen)
            await pilot.pause()
            app.pop_screen()
            await pilot.pause()
            await app.push_screen(screen)
            await pilot.pause()
        assert runs["n"] == 2, "a re-opened doctor showed a stale verdict"

    async def test_r_re_runs_them_without_leaving(self, monkeypatch, tmp_path):
        runs = {"n": 0}
        monkeypatch.setattr(
            doctor_mod.checks,
            "check_all",
            lambda _env: (runs.__setitem__("n", runs["n"] + 1), {"slack": []})[1],
        )
        monkeypatch.setattr(
            doctor_mod.supervisor, "DEFAULT_ENV_FILE", tmp_path / ".env"
        )
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(DoctorScreen())
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
        assert runs["n"] == 2

    async def test_the_env_file_is_read_the_way_the_daemons_receive_it(
        self, monkeypatch, tmp_path
    ):
        """Reading os.environ alone would report a passing check for a value the
        daemon never sees, which is the opposite of what a doctor is for."""
        env_file = tmp_path / ".env"
        env_file.write_text("SLACK_TOKEN=xoxb-from-file\n")
        monkeypatch.setattr(doctor_mod.supervisor, "DEFAULT_ENV_FILE", env_file)
        seen: list[dict] = []
        monkeypatch.setattr(
            doctor_mod.checks,
            "check_all",
            lambda env: (seen.append(env), {"slack": []})[1],
        )
        app = _Host()
        async with app.run_test() as pilot:
            await app.push_screen(DoctorScreen())
            await pilot.pause()
        assert seen[0]["SLACK_TOKEN"] == "xoxb-from-file"
