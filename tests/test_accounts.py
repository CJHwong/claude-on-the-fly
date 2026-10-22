"""Account routing keeps credentials separate from conversation history."""

import pytest
import yaml

from claude_on_the_fly import accounts, agent, settings


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(settings, "operator_settings", lambda: path)
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path / "state-root")

    def write(document):
        path.write_text(yaml.safe_dump(document))

    return write


def definitions():
    return {
        "work": {"backend": "claude"},
        "personal": {"backend": "claude", "home": "~/profiles/personal"},
        "code": {"backend": "codex", "home": "~/profiles/code"},
    }


def test_absent_configuration_preserves_defaults(configuration):
    assert accounts.all_accounts() == {}
    assert accounts.select("slack", "session", "claude", "C1", "U1") is None
    assert accounts.current() is None


def test_routing_priority_and_durable_pin(configuration):
    document = {
        "accounts": definitions(),
        "routing": {
            "defaults": {"claude": "work"},
            "slack": {
                "channels": {"C1": {"claude": "personal"}},
                "users": {"U1": {"claude": "work"}},
            },
        },
    }
    configuration(document)
    selected = accounts.select("slack", "session", "claude", "C1", "U1", persist=True)
    assert selected.name == "personal"
    document["routing"]["slack"]["channels"]["C1"]["claude"] = "work"
    configuration(document)
    assert accounts.select("slack", "session", "claude", "C1", "U2").name == "personal"
    assert accounts.select("slack", "new-session", "claude", "C1", "U2").name == "work"
    assert accounts.select("slack", "user-session", "claude", "C2", "U1").name == "work"
    assert (
        accounts.select("telegram", "default-session", "claude", "42", "42").name
        == "work"
    )


def test_backend_specific_routing(configuration):
    configuration(
        {"accounts": definitions(), "routing": {"defaults": {"codex": "code"}}}
    )
    assert accounts.select("slack", "session", "codex", "C1", "U1").name == "code"
    assert accounts.select("slack", "session", "claude", "C1", "U1") is None


def test_context_and_profile(configuration):
    configuration(
        {
            "accounts": {
                "local": {"backend": "claude", "mode": "ollama", "model": "local-model"}
            }
        }
    )
    selected = accounts.all_accounts()["local"]
    profile = accounts.apply_profile(
        agent.AgentProfile("claude", "native", "", ""), selected
    )
    assert (profile.account, profile.mode, profile.model) == (
        "local",
        "ollama",
        "local-model",
    )
    with accounts.use(selected):
        assert accounts.current() == selected
    assert accounts.current() is None
    assert accounts.apply_profile(profile, None).account == ""


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {"backend": "unknown"},
        {"backend": "claude", "home": 3},
        {"backend": "claude", "mode": "bad"},
        {"backend": "claude", "model": 5},
        {"backend": "claude", "mode": "ollama"},
        {"backend": "claude", "typo": True},
        {"backend": "claude", "home": "relative/path"},
    ],
)
def test_invalid_account_fails(configuration, entry):
    configuration({"accounts": {"broken": entry}})
    with pytest.raises(accounts.AccountError):
        accounts.all_accounts()


def test_wrong_backend_does_not_fall_back(configuration):
    configuration(
        {"accounts": definitions(), "routing": {"defaults": {"claude": "code"}}}
    )
    with pytest.raises(accounts.AccountError, match="backend"):
        accounts.select("slack", "session", "claude", "", "")


def test_removed_pinned_account_fails(configuration):
    configuration(
        {"accounts": definitions(), "routing": {"defaults": {"claude": "work"}}}
    )
    accounts.select("slack", "session", "claude", "", "", persist=True)
    configuration({"accounts": {}})
    with pytest.raises(accounts.AccountError, match="work"):
        accounts.select("slack", "session", "claude", "", "")
