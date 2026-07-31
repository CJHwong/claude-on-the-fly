"""Tests for claude_on_the_fly.slack_manifest.

These render against the real slack_manifest.json, so template drift (a block
renamed or moved) fails here instead of at someone's Slack install.
"""

from __future__ import annotations

import json

import pytest

from claude_on_the_fly import slack_manifest
from claude_on_the_fly.slack_manifest import (
    PLACEHOLDER_COMMAND,
    command_error,
    generate,
    render,
)


class FakeStdin:
    """Queued answers for the interactive prompts."""

    def __init__(self, *answers: str, tty: bool = True):
        self._answers = list(answers)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return f"{self._answers.pop(0)}\n" if self._answers else ""


# ---------------------------------------------------------------------------
# command_error
# ---------------------------------------------------------------------------


class TestCommandError:
    def test_accepts_a_plain_command(self):
        assert command_error("/cof-hoss") is None

    def test_rejects_missing_slash(self):
        # The silent-failure case: bolt registers it and Slack routes nothing.
        assert "must start with '/'" in (command_error("cof-hoss") or "")

    def test_rejects_the_placeholder(self):
        assert "placeholder" in (command_error(PLACEHOLDER_COMMAND) or "")

    def test_rejects_bare_slash(self):
        assert command_error("/") is not None

    def test_rejects_spaces(self):
        assert "spaces" in (command_error("/cof hoss") or "")

    def test_rejects_over_32_chars(self):
        assert command_error("/" + "c" * 32) is not None


# ---------------------------------------------------------------------------
# suggested_command
# ---------------------------------------------------------------------------


class TestSuggestedCommand:
    def _as_user(self, monkeypatch, login: str) -> str | None:
        monkeypatch.setattr(slack_manifest.getpass, "getuser", lambda: login)
        return slack_manifest.suggested_command()

    def test_uses_the_login_name(self, monkeypatch):
        assert self._as_user(monkeypatch, "hoss") == "/cof-hoss"

    def test_slugs_case_and_punctuation(self, monkeypatch):
        assert self._as_user(monkeypatch, "Hoss.Wong") == "/cof-hoss-wong"

    def test_result_is_always_a_valid_command(self, monkeypatch):
        for login in ("hoss", "Hoss.Wong", "a" * 60, "user_1", "ho ss"):
            suggestion = self._as_user(monkeypatch, login)
            assert suggestion is None or command_error(suggestion) is None

    def test_long_login_stays_under_the_cap(self, monkeypatch):
        suggestion = self._as_user(monkeypatch, "a" * 60)
        assert suggestion is not None
        assert len(suggestion) <= 32
        assert not suggestion.endswith("-")

    def test_no_suggestion_when_the_login_slugs_to_nothing(self, monkeypatch):
        assert self._as_user(monkeypatch, "!!!") is None

    def test_no_suggestion_when_there_is_no_passwd_entry(self, monkeypatch):
        def boom():
            raise KeyError("no passwd entry")

        monkeypatch.setattr(slack_manifest.getpass, "getuser", boom)
        assert slack_manifest.suggested_command() is None


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


class TestRenderBotMode:
    def test_keeps_only_bot_scopes_and_events(self):
        manifest = render("bot", "COF", "/cof-hoss")
        scopes = manifest["oauth_config"]["scopes"]
        events = manifest["settings"]["event_subscriptions"]
        assert "bot" in scopes and "user" not in scopes
        assert "bot_events" in events and "user_events" not in events

    def test_names_the_app_and_the_bot(self):
        manifest = render("bot", "COF (Hoss)", "/cof-hoss")
        assert manifest["display_information"]["name"] == "COF (Hoss)"
        assert manifest["features"]["bot_user"]["display_name"] == "COF (Hoss)"

    def test_keeps_the_scopes_the_frontend_depends_on(self):
        # assistant:write backs _set_status; commands backs the slash command.
        scopes = render("bot", "COF", "/cof-hoss")["oauth_config"]["scopes"]["bot"]
        assert "assistant:write" in scopes
        assert "commands" in scopes

    def test_substitutes_the_command(self):
        manifest = render("bot", "COF", "/cof-hoss")
        commands = manifest["features"]["slash_commands"]
        assert [entry["command"] for entry in commands] == ["/cof-hoss"]

    def test_omits_slash_commands_when_none(self):
        # Opt-out: the picker still ships, reachable from the shortcut.
        manifest = render("bot", "COF", None)
        assert "slash_commands" not in manifest["features"]
        assert manifest["features"]["shortcuts"]
        assert manifest["settings"]["interactivity"]["is_enabled"] is True

    def test_refuses_an_invalid_command(self):
        with pytest.raises(ValueError, match="must start with"):
            render("bot", "COF", "cof-hoss")

    def test_refuses_the_placeholder(self):
        with pytest.raises(ValueError, match="placeholder"):
            render("bot", "COF", PLACEHOLDER_COMMAND)


class TestRenderUserMode:
    def test_keeps_only_user_scopes_and_events(self):
        manifest = render("user", "COF", None)
        scopes = manifest["oauth_config"]["scopes"]
        events = manifest["settings"]["event_subscriptions"]
        assert "user" in scopes and "bot" not in scopes
        assert "user_events" in events and "bot_events" not in events

    def test_drops_every_bot_only_feature(self):
        manifest = render("user", "COF", None)
        # A user token receives no app interactions and creates no bot to DM.
        assert "features" not in manifest
        assert "interactivity" not in manifest["settings"]

    def test_still_names_the_app(self):
        assert render("user", "Mine", None)["display_information"]["name"] == "Mine"


class TestRenderValidation:
    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError, match="mode must be"):
            render("hybrid", "COF", None)

    @pytest.mark.parametrize("mode", ["bot", "user"])
    def test_stays_socket_mode(self, mode):
        # No public URL in either shape; the whole design rides Socket Mode.
        assert render(mode, "COF", None)["settings"]["socket_mode_enabled"] is True


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


class TestGenerateFlagMode:
    def test_prints_json_to_stdout(self, capsys):
        assert generate(mode="bot", name="COF", command="/cof-x", out=None) == 0
        out, err = capsys.readouterr()
        manifest = json.loads(out)
        assert manifest["features"]["slash_commands"][0]["command"] == "/cof-x"
        # Guidance never contaminates the redirected manifest.
        assert "Next steps" in err

    def test_env_block_carries_the_chosen_command(self, capsys):
        generate(mode="bot", name="COF", command="/cof-x", out=None)
        err = capsys.readouterr().err
        # Not as an env var: it is not a credential, and printing it beside two
        # tokens taught the opposite at setup time.
        assert "SLACK_SLASH_COMMAND=" not in err
        assert "slash_command: /cof-x" in err
        assert "config.yaml" in err
        assert "SLACK_TOKEN=xoxb-" in err

    def test_user_mode_env_block_has_no_command(self, capsys):
        generate(mode="user", name="COF", command=None, out=None)
        err = capsys.readouterr().err
        assert "slash_command" not in err
        assert "SLACK_TOKEN=xoxp-" in err

    def test_writes_to_out_path(self, tmp_path, capsys):
        target = tmp_path / "m.json"
        assert generate(mode="bot", name="COF", command=None, out=str(target)) == 0
        assert json.loads(target.read_text())["display_information"]["name"] == "COF"
        assert capsys.readouterr().out == ""

    def test_invalid_command_exits_nonzero(self, capsys):
        assert generate(mode="bot", name="COF", command="nope", out=None) == 2
        assert capsys.readouterr().out == ""

    def test_requires_mode_without_a_terminal(self, capsys, monkeypatch):
        monkeypatch.setattr(slack_manifest.sys, "stdin", FakeStdin(tty=False))
        assert generate(mode=None, name=None, command=None, out=None) == 2
        assert "--mode" in capsys.readouterr().err

    def test_refuses_to_clobber_without_a_terminal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(slack_manifest.sys, "stdin", FakeStdin(tty=False))
        target = tmp_path / "m.json"
        target.write_text("keep me")
        assert generate(mode="bot", name="COF", command=None, out=str(target)) == 1
        assert target.read_text() == "keep me"


class TestGenerateInteractive:
    @pytest.fixture(autouse=True)
    def fixed_suggestion(self, monkeypatch):
        """Pin the suggested command: the real one reads the login name, so
        leaving it live would make these depend on who runs pytest."""
        monkeypatch.setattr(slack_manifest, "suggested_command", lambda: "/cof-tester")

    def test_asks_for_every_field(self, tmp_path, monkeypatch):
        target = tmp_path / "m.json"
        monkeypatch.setattr(
            slack_manifest.sys,
            "stdin",
            FakeStdin("bot", "COF (Hoss)", "/cof-hoss", str(target)),
        )
        assert generate(mode=None, name=None, command=None, out=None) == 0
        manifest = json.loads(target.read_text())
        assert manifest["display_information"]["name"] == "COF (Hoss)"
        assert manifest["features"]["slash_commands"][0]["command"] == "/cof-hoss"

    def test_explains_the_collision_before_asking(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "m.json"
        monkeypatch.setattr(
            slack_manifest.sys, "stdin", FakeStdin("bot", "COF", "", str(target))
        )
        generate(mode=None, name=None, command=None, out=None)
        err = capsys.readouterr().err
        assert "does not namespace" in err
        # The suggestion is visible as the prompt's default, not applied silently.
        assert "[/cof-tester]" in err

    def test_none_skips_the_command(self, tmp_path, monkeypatch):
        target = tmp_path / "m.json"
        monkeypatch.setattr(
            slack_manifest.sys, "stdin", FakeStdin("bot", "COF", "none", str(target))
        )
        generate(mode=None, name=None, command=None, out=None)
        assert "slash_commands" not in json.loads(target.read_text())["features"]

    def test_empty_answer_takes_the_suggestion(self, tmp_path, monkeypatch):
        target = tmp_path / "m.json"
        monkeypatch.setattr(
            slack_manifest.sys, "stdin", FakeStdin("bot", "COF", "", str(target))
        )
        generate(mode=None, name=None, command=None, out=None)
        commands = json.loads(target.read_text())["features"]["slash_commands"]
        assert commands[0]["command"] == "/cof-tester"

    def test_reprompts_on_an_invalid_command(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "m.json"
        monkeypatch.setattr(
            slack_manifest.sys,
            "stdin",
            FakeStdin("bot", "COF", "cof-hoss", "/cof-hoss", str(target)),
        )
        assert generate(mode=None, name=None, command=None, out=None) == 0
        assert "must start with '/'" in capsys.readouterr().err
        commands = json.loads(target.read_text())["features"]["slash_commands"]
        assert commands[0]["command"] == "/cof-hoss"

    def test_user_mode_never_asks_for_a_command(self, tmp_path, monkeypatch):
        target = tmp_path / "m.json"
        monkeypatch.setattr(
            slack_manifest.sys, "stdin", FakeStdin("user", "COF", str(target))
        )
        assert generate(mode=None, name=None, command=None, out=None) == 0
        assert "features" not in json.loads(target.read_text())

    def test_defaults_are_taken_on_empty_answers(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(slack_manifest.sys, "stdin", FakeStdin("", "", "", ""))
        assert generate(mode=None, name=None, command=None, out=None) == 0
        written = tmp_path / slack_manifest.DEFAULT_OUT
        manifest = json.loads(written.read_text())
        assert manifest["display_information"]["name"] == slack_manifest.DEFAULT_NAME
        assert manifest["features"]["bot_user"]  # default mode is bot

    def test_input_closed_raises(self, monkeypatch):
        monkeypatch.setattr(slack_manifest.sys, "stdin", FakeStdin())
        with pytest.raises(SystemExit):
            generate(mode=None, name=None, command=None, out=None)


class TestConfirm:
    def test_a_non_tty_never_confirms(self, monkeypatch):
        """This gates a real Slack API write, so an unattended run (a pipe, CI) must
        decline rather than proceed on nobody's behalf."""
        monkeypatch.setattr(slack_manifest.sys.stdin, "isatty", lambda: False)
        assert slack_manifest._confirm("Update the app?") is False

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [("y\n", True), ("Y\n", True), ("yes\n", True), ("n\n", False), ("\n", False)],
    )
    def test_only_an_explicit_yes_confirms(self, monkeypatch, answer, expected):
        monkeypatch.setattr(slack_manifest.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(slack_manifest.sys.stdin, "readline", lambda: answer)
        assert slack_manifest._confirm("Update the app?") is expected
