"""Account rules fail closed and preserve the initiating selection."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from claude_on_the_fly import accounts, agent, envfile, settings, transcript


@pytest.fixture
def account_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(settings, "operator_settings", lambda: path)
    monkeypatch.setattr(settings, "_STARTUP_VALUES", {})
    monkeypatch.setattr(settings, "_DOCUMENTS", {})
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path / "daemon")

    def write(document):
        path.write_text(yaml.safe_dump(document))
        return path

    return write


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"accounts": []}, "accounts: must be a mapping"),
        ({"routing": []}, "routing: must be a mapping"),
        ({"accounts": {"bad/name": {"backend": "claude"}}}, "Invalid account name"),
        ({"accounts": {"empty": {"backend": "claude", "home": " "}}}, "home"),
        ({"accounts": {"empty": {"backend": "claude", "model": " "}}}, "model"),
        ({"routing": {"surprise": {}}}, "routing: use defaults"),
        ({"routing": {"defaults": []}}, "routing must use mappings"),
        ({"routing": {"defaults": {"other": "work"}}}, "supports claude and codex"),
        ({"routing": {"defaults": {"claude": 7}}}, "must name an account"),
        ({"routing": {"defaults": {"claude": ""}}}, "must name an account"),
        ({"routing": {"defaults": {"claude": "missing"}}}, "Unknown account"),
        ({"routing": {"slack": []}}, "platform rules"),
        ({"routing": {"slack": {"typo": {}}}}, "platform rules"),
        ({"routing": {"telegram": {"channels": []}}}, "channels: must be a mapping"),
        ({"routing": {"telegram": {"users": []}}}, "users: must be a mapping"),
        ({"routing": {"slack": {"users": {"U1": []}}}}, "must use mappings"),
    ],
)
def test_validate_rejects_invalid_configuration(account_config, document, message):
    account_config(document)
    with pytest.raises(accounts.AccountError, match=message):
        accounts.validate()


def test_validate_accepts_both_platforms_and_expands_home(
    account_config, monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    account_config(
        {
            "accounts": {"work": {"backend": "claude", "home": "~/work"}},
            "routing": {
                "defaults": {"claude": "work"},
                "slack": {"channels": {"C1": {"claude": "work"}}},
                "telegram": {"users": {42: {"claude": "work"}}},
            },
        }
    )
    accounts.validate()
    assert accounts.named("work", "claude").source_home == tmp_path / "work"
    selected = accounts.select("telegram", "session", "claude", "99", "42")
    assert selected is not None and selected.name == "work"
    assert accounts.Account("default", "codex").source_home == tmp_path / ".codex"


@pytest.mark.parametrize(
    "contents", ["accounts: [", "- accounts", "accounts: {}\nrouting: ["]
)
def test_unreadable_document_fails_without_fallback(account_config, contents):
    path = account_config({})
    path.write_text(contents)
    with pytest.raises(
        accounts.AccountError, match="Cannot read account configuration"
    ):
        accounts.all_accounts()


def test_configuration_read_error_is_explicit(account_config, monkeypatch):
    account_config({})

    def refuse(path):
        raise PermissionError("configuration denied")

    monkeypatch.setattr(settings, "_cached_document", refuse)
    with pytest.raises(
        accounts.AccountError, match="Cannot read account configuration"
    ):
        accounts.validate()


def test_account_definitions_freeze_while_routing_reloads(account_config, monkeypatch):
    original = {"work": {"backend": "claude"}}
    account_config({"accounts": original})
    monkeypatch.setattr(settings, "_STARTUP_VALUES", {"accounts": original})
    account_config(
        {
            "accounts": {"new": {"backend": "claude"}},
            "routing": {"defaults": {"claude": "work"}},
        }
    )
    assert set(accounts.all_accounts()) == {"work"}
    selected = accounts.default_for("claude")
    assert selected is not None and selected.name == "work"


@pytest.mark.parametrize("block", [[], {"unexpected": {}}])
def test_selected_platform_must_be_a_mapping(account_config, block):
    account_config({"routing": {"slack": block}})
    with pytest.raises(accounts.AccountError, match=r"routing\.slack"):
        accounts.select("slack", "session", "claude", "C1", "U1")


@pytest.mark.parametrize("kind", ["channels", "users"])
def test_subject_rules_must_be_mappings(account_config, kind):
    account_config({"routing": {"slack": {kind: []}}})
    with pytest.raises(accounts.AccountError, match=f"routing.{kind}"):
        accounts.select("slack", "session", "claude", "C1", "U1")


def test_profiles_keep_explicit_choices_and_inherit_defaults(
    account_config, monkeypatch
):
    account_config(
        {
            "accounts": {
                "work": {"backend": "claude", "model": "account-model"},
                "native": {"backend": "claude", "mode": "native"},
                "code": {"backend": "codex"},
            },
            "routing": {"defaults": {"claude": "work"}},
        }
    )
    profile = agent.AgentProfile("claude", "ollama", "local-model", "low")
    assert accounts.resolve_profile(profile).model == "account-model"
    assert accounts.resolve_profile(replace(profile, account="")).account == ""
    assert (
        accounts.resolve_profile(
            accounts.apply_profile(profile, accounts.named("native", "claude"))
        ).mode
        == "native"
    )
    monkeypatch.setenv("CLAUDE_MODEL", "native-model")
    native = accounts.apply_profile(profile, accounts.named("native", "claude"))
    assert native.model == "native-model"
    assert native.effort == "low"
    with pytest.raises(accounts.AccountError, match="wrong backend"):
        accounts.apply_profile(profile, accounts.named("code", "codex"))


def test_unconfigured_profile_retains_identity(account_config):
    profile = agent.AgentProfile("codex", "native", "", "")
    assert accounts.apply_profile(profile, None) is profile
    assert accounts.resolve_profile(profile) is profile


@pytest.mark.parametrize("contents", ["not json", "null", "{}", "42"])
def test_corrupt_saved_selection_does_not_select_default(account_config, contents):
    account_config({})
    path = accounts._selection_path("slack", "session", "claude")
    path.parent.mkdir(parents=True)
    path.write_text(contents)
    with pytest.raises(accounts.AccountError, match="saved account selection"):
        accounts.select("slack", "session", "claude", "C1", "U1")


def test_saved_selection_refuses_symlinks(account_config, tmp_path):
    account_config({})
    path = accounts._selection_path("slack", "session", "claude")
    path.parent.mkdir(parents=True)
    target = tmp_path / "selection.json"
    target.write_text(json.dumps("work"))
    path.symlink_to(target)
    with pytest.raises(accounts.AccountError, match="symbolic link"):
        accounts.select("slack", "session", "claude", "C1", "U1")


def test_saved_selection_read_error_is_explicit(account_config, monkeypatch):
    account_config({})

    def refuse(path, *arguments, **keywords):
        raise PermissionError("selection denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    with pytest.raises(
        accounts.AccountError, match="Cannot read saved account selection"
    ):
        accounts.select("slack", "session", "claude", "C1", "U1")


def test_save_failure_cleans_temporary_file(account_config, monkeypatch):
    account_config(
        {
            "accounts": {"work": {"backend": "claude"}},
            "routing": {"defaults": {"claude": "work"}},
        }
    )

    def refuse(source, target):
        raise PermissionError("replace denied")

    monkeypatch.setattr(accounts.os, "replace", refuse)
    with pytest.raises(PermissionError, match="replace denied"):
        accounts.select("slack", "session", "claude", "C1", "U1", persist=True)
    directory = accounts._selection_path("slack", "session", "claude").parent
    assert list(directory.iterdir()) == []


def test_saved_legacy_selection_cannot_adopt_later_rule(account_config):
    account_config({"accounts": {"work": {"backend": "claude"}}})
    assert (
        accounts.select("slack", "session", "claude", "C1", "U1", persist=True) is None
    )
    account_config(
        {
            "accounts": {"work": {"backend": "claude"}},
            "routing": {"defaults": {"claude": "work"}},
        }
    )
    assert accounts.select("slack", "session", "claude", "C1", "U2") is None
    selected = accounts.select("slack", "new", "claude", "C1", "U2")
    assert selected is not None and selected.name == "work"


def test_claude_preparation_shares_only_workspace_and_is_idempotent(
    account_config, monkeypatch, tmp_path
):
    account_config({})
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    monkeypatch.setattr(
        envfile, "daemon_environment", lambda: {"CLAUDE_CONFIG_DIR": str(source_home)}
    )
    workspace = tmp_path / "workspace"
    account = accounts.Account("work", "claude", target_home)
    with accounts.use(account):
        accounts.prepare(workspace)
        accounts.prepare(workspace)
    digest = transcript._workspace_to_claude_hash(workspace)
    target = target_home / "projects" / digest
    assert target.is_symlink()
    assert target.resolve() == source_home / "projects" / digest
    assert list(target_home.iterdir()) == [target_home / "projects"]
    with accounts.use(accounts.Account("same", "claude", source_home)):
        accounts.prepare(workspace)
    with accounts.use(accounts.Account("code", "codex", target_home)):
        accounts.prepare(tmp_path / "other")
    accounts.prepare(tmp_path / "other")
    assert not (target_home / "sessions").exists()


@pytest.mark.parametrize("conflict", ["directory", "broken_link"])
def test_claude_preparation_refuses_separate_history(
    account_config, monkeypatch, tmp_path, conflict
):
    account_config({})
    source_home = tmp_path / "source"
    monkeypatch.setattr(
        envfile, "daemon_environment", lambda: {"CLAUDE_CONFIG_DIR": str(source_home)}
    )
    workspace = tmp_path / "workspace"
    account = accounts.Account("work", "claude", tmp_path / "target")
    target = (
        account.source_home
        / "projects"
        / transcript._workspace_to_claude_hash(workspace)
    )
    target.parent.mkdir(parents=True)
    if conflict == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "missing")
    with (
        accounts.use(account),
        pytest.raises(accounts.AccountError, match="separate history"),
    ):
        accounts.prepare(workspace)
    assert target.exists() or target.is_symlink()


async def test_concurrent_account_contexts_do_not_leak():
    first = accounts.Account("first", "claude")
    second = accounts.Account("second", "codex")
    ready = asyncio.Event()

    async def observe(account):
        with accounts.use(account):
            ready.set()
            await asyncio.sleep(0)
            assert accounts.current() == account
            with accounts.use(None):
                assert accounts.current() is None
            assert accounts.current() == account
        assert accounts.current() is None

    await asyncio.gather(observe(first), observe(second))
    assert ready.is_set()
    assert accounts.current() is None
