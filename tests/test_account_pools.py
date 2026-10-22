"""Ordered account pools are explicit and remain frozen per conversation."""

import json

import pytest
import yaml

from claude_on_the_fly import accounts, agent, settings


@pytest.fixture
def pool_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(settings, "operator_settings", lambda: path)
    monkeypatch.setattr(settings, "_STARTUP_VALUES", {})
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path / "daemon")
    document = {
        "accounts": {
            name: {"backend": "codex", "home": str(tmp_path / name)}
            for name in ("first", "second", "third")
        },
        "routing": {"defaults": {"codex": ["first", "second", "third"]}},
    }

    def write():
        path.write_text(yaml.safe_dump(document))

    write()
    return document, write


def test_ordered_pool_is_frozen_and_success_is_saved(pool_config):
    document, write = pool_config
    selected = accounts.selection("slack", "session", "codex", "C1", "U1", persist=True)
    assert selected.account.name == "first"
    assert selected.pool == ("first", "second", "third")
    accounts.remember("slack", "session", "codex", "second", selected.pool)
    document["routing"]["defaults"]["codex"] = ["third", "first"]
    write()
    resumed = accounts.selection("slack", "session", "codex", "C1", "U2")
    assert resumed.account.name == "second"
    assert resumed.pool == ("first", "second", "third")
    assert accounts.selection("slack", "new-session", "codex", "C1", "U2").pool == (
        "third",
        "first",
    )
    assert (
        accounts._saved(accounts._selection_path("slack", "session", "codex"))
        == "second"
    )


def test_fixed_account_does_not_enable_fallback(pool_config):
    document, write = pool_config
    document["routing"]["defaults"]["codex"] = "first"
    write()
    selected = accounts.selection("slack", "session", "codex", "", "", persist=True)
    assert selected.pool == ()
    assert (
        json.loads(accounts._selection_path("slack", "session", "codex").read_text())
        == "first"
    )


@pytest.mark.parametrize(
    "invalid", [[], ["first", "first"], ["first", 3], ["missing"], ["first", ""]]
)
def test_invalid_pools_fail_at_startup(pool_config, invalid):
    document, write = pool_config
    document["routing"]["defaults"]["codex"] = invalid
    write()
    with pytest.raises(accounts.AccountError):
        accounts.validate()


def test_channel_pool_overrides_user_and_default(pool_config):
    document, write = pool_config
    document["routing"]["slack"] = {
        "channels": {"C1": {"codex": ["third", "second"]}},
        "users": {"U1": {"codex": "first"}},
    }
    write()
    assert accounts.selection("slack", "session", "codex", "C1", "U1").pool == (
        "third",
        "second",
    )
    user = accounts.selection("slack", "other", "codex", "C2", "U1")
    assert user.account.name == "first"
    assert user.pool == ()


def test_account_switch_keeps_user_model_override(pool_config):
    document, write = pool_config
    document["accounts"]["first"]["model"] = "first-model"
    document["accounts"]["second"]["model"] = "second-model"
    write()
    base = agent.AgentProfile("codex", "native", "base-model", "low")
    first = accounts.apply_profile(base, accounts.named("first", "codex"))
    pinned = agent.apply_override(first, {"model": "user-model"})
    second = accounts.apply_profile(pinned, accounts.named("second", "codex"))
    assert second.model == "user-model"
    unpinned = accounts.apply_profile(first, accounts.named("third", "codex"))
    assert unpinned.model == "base-model"


@pytest.mark.parametrize(
    "record",
    [
        {"account": "first", "pool": []},
        {"account": 3, "pool": ["first"]},
        {"account": "first", "pool": "first"},
        {"account": "first", "pool": ["first", 3]},
        {"account": "first", "pool": ["first", "first"]},
        {"account": "third", "pool": ["first", "second"]},
        {"account": "first", "pool": [""]},
        {"account": "first", "pool": ["first"], "extra": True},
    ],
)
def test_invalid_saved_pool_stops_before_account_use(pool_config, record):
    path = accounts._selection_path("slack", "session", "codex")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    with pytest.raises(accounts.AccountError, match="Invalid saved"):
        accounts.saved_selection("slack", "session", "codex")


def test_remember_refuses_account_outside_pool(pool_config):
    with pytest.raises(accounts.AccountError, match="outside"):
        accounts.remember("slack", "session", "codex", "third", ("first", "second"))
