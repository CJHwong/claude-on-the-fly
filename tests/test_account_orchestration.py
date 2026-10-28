"""Account selection survives restart and remains local to each running turn."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from claude_on_the_fly import (
    accounts,
    agent,
    envfile,
    orchestrator,
    preflight,
    settings,
)
from claude_on_the_fly.agent import Response
from claude_on_the_fly.events import EventLog
from claude_on_the_fly.orchestrator import Orchestrator, Turn
from claude_on_the_fly.slack import SlackFrontend
from tests.test_orchestrator import StubFrontend


@pytest.fixture
def setup_accounts(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    base = tmp_path / "base"
    homes = {name: tmp_path / name for name in ("first", "second")}
    document = {
        "accounts": {
            name: {"backend": "claude", "home": str(home)}
            for name, home in homes.items()
        },
        "routing": {
            "slack": {
                "channels": {"1": {"claude": "first"}},
                "users": {"2": {"claude": "second"}},
            }
        },
    }
    config.write_text(yaml.safe_dump(document))
    monkeypatch.setattr(settings, "operator_settings", lambda: config)
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(orchestrator, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(
        envfile, "daemon_environment", lambda: {"CLAUDE_CONFIG_DIR": str(base)}
    )
    monkeypatch.setattr(orchestrator.tmux, "available", lambda: False)
    monkeypatch.setenv("AGENT_BACKEND", "claude")
    return config, document


async def test_dispatch_pins_account_across_restart_and_new_session(
    setup_accounts, tmp_path, monkeypatch
):
    config, document = setup_accounts
    frontend = StubFrontend()
    observed = []

    async def run(*args, **kwargs):
        observed.append(accounts.current().name)
        return Response(body="answer")

    monkeypatch.setattr(agent, "run", run)
    first = Orchestrator(frontend, "slack", event_log=EventLog(tmp_path / "events"))
    await first._process(1, Turn("first"))
    document["routing"]["slack"]["channels"]["1"]["claude"] = "second"
    config.write_text(yaml.safe_dump(document))
    restarted = Orchestrator(frontend, "slack", event_log=EventLog(tmp_path / "events"))
    await restarted._process(1, Turn("resume"))
    restarted.set_session_token(1, "new")
    await restarted._process(1, Turn("new"))
    assert observed == ["first", "first", "second"]
    assert accounts.current() is None


async def test_concurrent_turns_do_not_share_account_context(
    setup_accounts, tmp_path, monkeypatch
):
    frontend = StubFrontend()
    frontend.session_facts = lambda chat_id: {"sender_id": str(chat_id)}
    observed = {}

    async def run(workspace, session, text, *args, **kwargs):
        before = accounts.current().name
        await asyncio.sleep(0)
        observed[text.split("\n", 1)[0]] = (before, accounts.current().name)
        return Response(body="answer")

    monkeypatch.setattr(agent, "run", run)
    instance = Orchestrator(frontend, "slack", event_log=EventLog(tmp_path / "events"))
    await asyncio.gather(
        instance._process(1, Turn("channel")), instance._process(2, Turn("user"))
    )
    assert observed == {"channel": ("first", "first"), "user": ("second", "second")}


async def test_selection_error_reports_without_dispatch(
    setup_accounts, tmp_path, monkeypatch
):
    config, document = setup_accounts
    document["routing"]["slack"]["channels"]["1"]["claude"] = "missing"
    config.write_text(yaml.safe_dump(document))
    frontend = StubFrontend()
    run = AsyncMock()
    monkeypatch.setattr(agent, "run", run)
    instance = Orchestrator(frontend, "slack", event_log=EventLog(tmp_path / "events"))
    await instance._process(1, Turn("question"))
    run.assert_not_called()
    assert "Account selection failed" in frontend.sent[0][1].body
    # The message still leaves the queue and loses its marker. Otherwise the next
    # turn's start takes this message's place and reacts on the wrong one.
    assert frontend.start_notifications == [1]
    assert frontend.complete_notifications == [1]


def test_slack_subjects_use_channel_and_initiating_user():
    frontend = SlackFrontend("app", "token", "operator")
    frontend._sessions[1] = ("C1", "thread")
    frontend._session_sender_ids[1] = "latest-user"
    frontend._account_senders[1] = "first-user"
    assert frontend.account_subjects(1) == ("C1", "first-user")
    assert frontend.account_subjects(2) == ("", "")


def test_preflight_checks_configured_backend_and_ollama(setup_accounts, monkeypatch):
    config, document = setup_accounts
    document["accounts"]["second"].update(mode="ollama", model="local-model")
    document["accounts"]["code"] = {"backend": "codex"}
    config.write_text(yaml.safe_dump(document))
    checked = []
    monkeypatch.setattr(
        preflight,
        "_check_agent_cli",
        lambda profile: checked.append((profile.backend, profile.mode)),
    )
    preflight.check_backend()
    assert set(checked) == {
        ("claude", "native"),
        ("claude", "ollama"),
        ("codex", "native"),
    }


def test_preflight_refuses_invalid_routing(setup_accounts, monkeypatch):
    config, document = setup_accounts
    document["routing"]["defaults"] = {"codex": "first"}
    config.write_text(yaml.safe_dump(document))
    with pytest.raises(SystemExit, match="backend"):
        preflight.check_backend()


async def test_agent_run_and_compaction_use_selected_account(
    setup_accounts, tmp_path, monkeypatch
):
    profile = accounts.apply_profile(
        agent.resolve_profile(), accounts.named("first", "claude")
    )
    observed = []

    class Backend:
        async def run(self, *args, **kwargs):
            observed.append(accounts.current().name)
            return Response(body="answer")

        async def compact(self, *args, **kwargs):
            observed.append(accounts.current().name)
            return None

    monkeypatch.setattr(agent, "get_backend", lambda profile: Backend())
    await agent.run(
        tmp_path / "workspace", "session", "question", "test", profile=profile
    )
    await agent.compact(tmp_path / "workspace", "session", profile=profile)
    assert observed == ["first", "first"]
    assert accounts.current() is None


@pytest.mark.parametrize(
    "homes",
    [
        {"first": str(Path.home())},
        {"first": "/", "second": "/tmp/account"},
        {"first": "/tmp/account", "second": "/tmp/account/nested"},
    ],
)
def test_unsafe_profile_roots_fail_before_dispatch(setup_accounts, homes):
    config, document = setup_accounts
    document["accounts"] = {
        name: {"backend": "claude", "home": home} for name, home in homes.items()
    }
    config.write_text(yaml.safe_dump(document))
    with pytest.raises(accounts.AccountError, match="home"):
        accounts.all_accounts()


def test_account_defaults_do_not_replace_conversation_model_pin(setup_accounts):
    config, document = setup_accounts
    document["accounts"]["first"]["model"] = "account-model"
    config.write_text(yaml.safe_dump(document))
    selected = accounts.apply_profile(
        agent.resolve_profile(), accounts.named("first", "claude")
    )
    pinned = agent.apply_override(selected, {"model": "conversation-model"})
    backend = agent.get_backend(pinned)
    assert backend.model == "conversation-model"


@pytest.mark.parametrize("after_startup", ["delete", "malformed", "scalar"])
def test_account_registry_stays_at_startup_until_restart(setup_accounts, after_startup):
    config, _ = setup_accounts
    settings.check_operator_settings()
    if after_startup == "delete":
        config.unlink()
    elif after_startup == "malformed":
        config.write_text("accounts: [")
    else:
        config.write_text("accounts: wrong-type")
    assert set(accounts.all_accounts()) == {"first", "second"}


def test_empty_template_sections_preserve_default_behavior(setup_accounts):
    config, _ = setup_accounts
    config.write_text("accounts:\nrouting:\n")
    accounts.validate()
    assert accounts.all_accounts() == {}


def test_invalid_account_section_fails_at_startup(setup_accounts):
    config, _ = setup_accounts
    config.write_text("accounts: wrong-type")
    with pytest.raises(accounts.AccountError, match="mapping"):
        settings.check_operator_settings()


async def test_inherited_ollama_mode_does_not_require_chatgpt_login(
    setup_accounts, tmp_path, monkeypatch
):
    from claude_on_the_fly import codex_auth

    config, document = setup_accounts
    document["accounts"]["code"] = {
        "backend": "codex",
        "home": str(tmp_path / "no-login"),
    }
    config.write_text(yaml.safe_dump(document))
    profile = accounts.apply_profile(
        agent.AgentProfile("codex", "ollama", "local-model", "low"),
        accounts.named("code", "codex"),
    )

    class Backend:
        async def run(self, *args, **kwargs):
            assert accounts.current().mode == "ollama"
            assert codex_auth.published_url() is None
            return Response(body="answer")

    monkeypatch.setattr(agent, "get_backend", lambda profile: Backend())
    monkeypatch.setattr(codex_auth, "enabled", lambda: True)
    response = await agent.run(
        tmp_path / "workspace", "session", "question", "test", profile=profile
    )
    assert response.body == "answer"


def test_account_home_cannot_nest_inside_daemon_profile(setup_accounts, tmp_path):
    config, document = setup_accounts
    document["accounts"]["first"]["home"] = str(tmp_path / "base" / "nested")
    config.write_text(yaml.safe_dump(document))
    with pytest.raises(accounts.AccountError, match="contain another"):
        accounts.all_accounts()


async def test_fallback_updates_idle_profile_to_successful_account(
    setup_accounts, tmp_path, monkeypatch
):
    config, document = setup_accounts
    document["routing"]["slack"]["channels"]["1"]["claude"] = ["first", "second"]
    document["accounts"]["second"]["model"] = "replacement-model"
    config.write_text(yaml.safe_dump(document))
    instance = Orchestrator(
        StubFrontend(), "slack", event_log=EventLog(tmp_path / "events")
    )
    observed = []

    async def run(workspace, session, prompt, platform, **kwargs):
        assert kwargs["profile"].account_pool == ("first", "second")
        accounts.remember(platform, session, "claude", "second", ("first", "second"))
        return Response(body="answer")

    monkeypatch.setattr(agent, "run", run)
    monkeypatch.setattr(
        instance, "_note_idle", lambda chat, response, profile: observed.append(profile)
    )
    await instance._process(1, Turn("question"))
    assert len(observed) == 1
    assert observed[0].account == "second"
    assert observed[0].model == "replacement-model"


@pytest.mark.parametrize(
    ("account_model", "expected"), [("account-model", "account-model"), (None, "haiku")]
)
def test_a_channel_profile_sits_under_the_routed_account(
    setup_accounts, tmp_path, account_model, expected
):
    """The documented layering: channel profile, then account, then pin. A
    routed account that sets a model wins over the channel's; one that does not
    leaves the channel's model, and effort always comes from the profile."""
    config, document = setup_accounts
    if account_model is not None:
        document["accounts"]["first"]["model"] = account_model
    document["agent"] = {
        "claude": {"model": "opus", "effort": "high"},
        "profiles": {"triage": {"claude": {"model": "haiku", "effort": "low"}}},
        "channel_profiles": {"slack": {"1": "triage"}},
    }
    config.write_text(yaml.safe_dump(document))
    instance = Orchestrator(
        StubFrontend(), "slack", event_log=EventLog(tmp_path / "events")
    )

    profile = instance.current_profile(1)

    assert (profile.account, profile.model, profile.effort) == (
        "first",
        expected,
        "low",
    )
    instance.on_model(1, ["sonnet"])
    assert instance.current_profile(1).model == "sonnet"
