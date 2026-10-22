"""Manual resume commands retain account credentials and workspace sessions."""

import json
import os
import shlex
import subprocess
from types import SimpleNamespace
from typing import cast

import pytest
import yaml

from claude_on_the_fly import accounts, agent, codex_state, settings, takeover


@pytest.fixture
def takeover_config(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    monkeypatch.setattr(settings, "operator_settings", lambda: config)
    monkeypatch.setattr(settings, "_STARTUP_VALUES", {})
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path / "daemon")
    monkeypatch.setattr(codex_state, "HOMES_DIR", tmp_path / "codex-homes")

    def configure(definitions, defaults=None, backend="claude"):
        config.write_text(
            yaml.safe_dump(
                {"accounts": definitions, "routing": {"defaults": defaults or {}}}
            )
        )
        profile = agent.AgentProfile(backend, "native", "", "")
        monkeypatch.setattr(agent, "resolve_profile", lambda: profile)

    return configure


def backend_stub(monkeypatch, result="claude --resume session"):
    observed = []

    def backend(profile=None):
        observed.append((profile, accounts.current()))
        return SimpleNamespace(takeover_command=lambda workspace, session: result)

    monkeypatch.setattr(agent, "get_backend", backend)
    return observed


def test_saved_channel_account_wins_over_changed_default(
    takeover_config, monkeypatch, tmp_path
):
    home = tmp_path / "selected account"
    takeover_config(
        {
            "channel": {"backend": "claude", "home": str(home)},
            "default": {"backend": "claude"},
        },
        {"claude": "default"},
    )
    accounts._save(accounts._selection_path("slack", "session", "claude"), "channel")
    observed = backend_stub(monkeypatch)
    result = takeover.command(tmp_path, "session", "slack")
    assert result is not None
    assert shlex.split(result)[-4:] == [
        f"CLAUDE_CONFIG_DIR={home}",
        "claude",
        "--resume",
        "session",
    ]
    assert observed[0][0].account == "channel"
    assert observed[0][1].name == "channel"
    assert accounts.current() is None


def test_default_claude_unsets_inherited_profile(
    takeover_config, monkeypatch, tmp_path
):
    takeover_config({"default": {"backend": "claude"}}, {"claude": "default"})
    backend_stub(monkeypatch)
    result = takeover.command(tmp_path, "session")
    assert result is not None
    assert shlex.split(result)[-5:] == [
        "-u",
        "CLAUDE_CONFIG_DIR",
        "claude",
        "--resume",
        "session",
    ]


def test_explicit_claude_home_survives_shell_normalization(
    takeover_config, monkeypatch, tmp_path
):
    home = tmp_path / "帳號 with spaces;$(false)'"
    takeover_config(
        {"work": {"backend": "claude", "home": str(home)}}, {"claude": "work"}
    )
    backend_stub(
        monkeypatch, "python3 -c 'import os; print(os.environ[\"CLAUDE_CONFIG_DIR\"])'"
    )
    result = takeover.command(tmp_path, "session")
    assert result is not None
    normalized = shlex.join(shlex.split(result))
    actual = subprocess.run(
        normalized, shell=True, capture_output=True, text=True, check=True
    )
    assert actual.stdout.strip() == str(home)


def test_codex_resume_uses_account_overlay(takeover_config, monkeypatch, tmp_path):
    takeover_config(
        {"code": {"backend": "codex", "home": str(tmp_path / "auth")}},
        {"codex": "code"},
        backend="codex",
    )
    observed = backend_stub(monkeypatch, "codex resume thread-id")
    result = takeover.command(tmp_path, "session", "telegram")
    assert result is not None
    with accounts.use(accounts.named("code", "codex")):
        overlay = codex_state.home_dir(tmp_path)
    assert shlex.split(result)[-4:] == [
        f"CODEX_HOME={overlay}",
        "codex",
        "resume",
        "thread-id",
    ]
    assert overlay != tmp_path / "auth"
    assert observed[0][1].name == "code"


def test_empty_configuration_preserves_original_command(
    takeover_config, monkeypatch, tmp_path
):
    takeover_config({})
    observed = backend_stub(monkeypatch, "original --resume session")
    assert takeover.command(tmp_path, "session") == "original --resume session"
    assert observed == [(None, None)]


def test_saved_legacy_selection_keeps_original_command(
    takeover_config, monkeypatch, tmp_path
):
    takeover_config({"work": {"backend": "claude"}}, {"claude": "work"})
    accounts._save(accounts._selection_path("slack", "session", "claude"), "")
    observed = backend_stub(monkeypatch)
    assert takeover.command(tmp_path, "session", "slack") == "claude --resume session"
    assert observed == [(None, None)]


def test_missing_session_has_no_command(takeover_config, monkeypatch, tmp_path):
    takeover_config({"work": {"backend": "claude"}}, {"claude": "work"})
    backend_stub(monkeypatch, None)
    assert takeover.command(tmp_path, "session") is None
    assert accounts.current() is None


def test_history_refuses_conflicting_platform_accounts(
    takeover_config, monkeypatch, tmp_path
):
    takeover_config({"first": {"backend": "claude"}, "second": {"backend": "claude"}})
    accounts._save(accounts._selection_path("slack", "session", "claude"), "first")
    accounts._save(accounts._selection_path("telegram", "session", "claude"), "second")
    observed = backend_stub(monkeypatch)
    with pytest.raises(accounts.AccountError, match="Conflicting saved accounts"):
        takeover.command(tmp_path, "session")
    assert observed == []
    assert takeover.command(tmp_path, "session", "slack") is not None


def test_matching_platform_pins_are_unambiguous(takeover_config, monkeypatch, tmp_path):
    takeover_config({"work": {"backend": "claude", "mode": "pty"}})
    for platform in ("slack", "telegram"):
        accounts._save(accounts._selection_path(platform, "session", "claude"), "work")
    observed = backend_stub(monkeypatch)
    assert takeover.command(tmp_path, "session") is not None
    assert observed[0][0].mode == "pty"
    assert observed[0][1].mode == "pty"


def test_missing_pinned_account_fails_without_fallback(
    takeover_config, monkeypatch, tmp_path
):
    takeover_config({"work": {"backend": "claude"}}, {"claude": "work"})
    accounts._save(accounts._selection_path("slack", "session", "claude"), "removed")
    observed = backend_stub(monkeypatch)
    with pytest.raises(accounts.AccountError, match="Unknown account"):
        takeover.command(tmp_path, "session", "slack")
    assert observed == []


@pytest.mark.parametrize("explicit_mode", [True, False])
def test_ollama_takeover_preserves_provider(
    takeover_config, monkeypatch, tmp_path, explicit_mode
):
    entry = {"backend": "claude"}
    if explicit_mode:
        entry.update(mode="ollama", model="qwen3:30b")
    takeover_config({"local": entry}, {"claude": "local"})
    if not explicit_mode:
        monkeypatch.setattr(
            agent,
            "resolve_profile",
            lambda: agent.AgentProfile("claude", "ollama", "qwen3:30b", ""),
        )
    observed = backend_stub(monkeypatch)
    result = takeover.command(tmp_path, "session", "slack")
    assert result is not None
    assert shlex.split(result)[-11:] == [
        "-u",
        "CLAUDE_CONFIG_DIR",
        "ollama",
        "launch",
        "claude",
        "--model",
        "qwen3:30b",
        "--yes",
        "--",
        "--resume",
        "session",
    ]
    assert observed[0][1].mode == "ollama"


@pytest.mark.parametrize("fail", [False, True])
def test_dashboard_copies_saved_account_command(
    takeover_config, monkeypatch, tmp_path, fail
):
    from claude_on_the_fly.tui.screens.dashboard import DashboardScreen

    takeover_config({"work": {"backend": "claude"}}, {"claude": "work"})
    backend_stub(monkeypatch)
    copied = []
    notices = []
    screen = SimpleNamespace(
        _selected_run=lambda: ("slack", "channel"),
        _chat_workspaces={"slack:channel": "workspace"},
        _job_sessions={"slack:channel": "session"},
        _run_workspace=lambda source, identifier: tmp_path,
        _notify=lambda *arguments: notices.append(arguments),
        app=SimpleNamespace(copy_to_clipboard=copied.append),
    )
    if fail:

        def refuse(*arguments, **keywords):
            raise accounts.AccountError("saved account unavailable")

        monkeypatch.setattr(takeover, "command", refuse)
    DashboardScreen.action_copy_takeover(cast("DashboardScreen", screen))
    if fail:
        assert copied == []
        assert "saved account unavailable" in notices[-1][0]
        return
    assert len(copied) == 1
    directory, resume = copied[0].split(" && ", 1)
    assert directory == f"cd -- {shlex.quote(str(tmp_path))}"
    assert shlex.split(resume)[-5:] == [
        "-u",
        "CLAUDE_CONFIG_DIR",
        "claude",
        "--resume",
        "session",
    ]
    assert notices[-1][1] == "information"


@pytest.mark.parametrize("fail", [False, True])
def test_history_copies_saved_account_command(
    takeover_config, monkeypatch, tmp_path, fail
):
    from claude_on_the_fly.tui.screens.history import HistoryScreen

    takeover_config({"work": {"backend": "claude"}}, {"claude": "work"})
    backend_stub(monkeypatch)
    copied = []
    notices = []
    table = SimpleNamespace(
        row_count=1,
        cursor_coordinate=SimpleNamespace(row=0),
        coordinate_to_cell_key=lambda coordinate: SimpleNamespace(
            row_key=SimpleNamespace(value="agg:slack:channel")
        ),
    )
    screen = SimpleNamespace(
        query_one=lambda *arguments: table,
        _row_watch={"agg:slack:channel": (tmp_path, "session")},
        _notify=lambda *arguments: notices.append(arguments),
        app=SimpleNamespace(copy_to_clipboard=copied.append),
    )
    if fail:

        def refuse(*arguments, **keywords):
            raise accounts.AccountError("saved account unavailable")

        monkeypatch.setattr(takeover, "command", refuse)
    HistoryScreen.action_copy_takeover(cast("HistoryScreen", screen))
    if fail:
        assert copied == []
        assert "saved account unavailable" in notices[-1][0]
        return
    assert len(copied) == 1
    directory, resume = copied[0].split(" && ", 1)
    assert directory == f"cd -- {shlex.quote(str(tmp_path))}"
    assert shlex.split(resume)[-5:] == [
        "-u",
        "CLAUDE_CONFIG_DIR",
        "claude",
        "--resume",
        "session",
    ]
    assert notices[-1][1] == "information"


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_manual_takeover_clears_inherited_credentials(
    takeover_config, monkeypatch, tmp_path, backend
):
    home = tmp_path / "selected profile"
    takeover_config(
        {"work": {"backend": backend, "home": str(home)}},
        {backend: "work"},
        backend=backend,
    )
    keys = (*accounts.AUTH_OVERRIDE_ENV, "COTF_CHATGPT_BASE_URL")
    variable = "CLAUDE_CONFIG_DIR" if backend == "claude" else "CODEX_HOME"
    script = (
        "import json, os; "
        f"print(json.dumps([os.environ.get({variable!r}), "
        f"{{key: os.environ[key] for key in {keys!r} if key in os.environ}}]))"
    )
    backend_stub(monkeypatch, shlex.join(["python3", "-c", script]))
    result = takeover.command(tmp_path, "session")
    assert result is not None
    inherited = {**os.environ, **dict.fromkeys(keys, "inherited-test-value")}
    actual = subprocess.run(
        result, shell=True, env=inherited, capture_output=True, text=True, check=True
    )
    expected = home
    if backend == "codex":
        with accounts.use(accounts.named("work", backend)):
            expected = codex_state.home_dir(tmp_path)
    assert json.loads(actual.stdout) == [str(expected), {}]
