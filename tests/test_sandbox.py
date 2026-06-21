"""sandbox: env curation and seatbelt wrapping, gated by COTF_SANDBOX."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from claude_on_the_fly import sandbox


def test_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("COTF_SANDBOX", raising=False)
    assert sandbox.mode() == "off"
    assert sandbox.enabled() is False


def test_unknown_mode_is_off(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "banana")
    assert sandbox.mode() == "off"


@pytest.mark.parametrize("value", ["env", "jail"])
def test_enabled_modes(monkeypatch, value):
    monkeypatch.setenv("COTF_SANDBOX", value)
    assert sandbox.mode() == value
    assert sandbox.enabled() is True


def test_agent_env_none_when_off(monkeypatch):
    monkeypatch.delenv("COTF_SANDBOX", raising=False)
    # None => create_subprocess_exec inherits the parent env (current behavior).
    assert sandbox.agent_env() is None


def test_agent_env_drops_secrets_keeps_essentials(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-leak")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-leak")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/anthropic")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HOME", "/Users/x")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = sandbox.agent_env()
    assert env is not None
    # Secrets dropped.
    for leaked in (
        "ANTHROPIC_API_KEY",
        "SLACK_APP_TOKEN",
        "JIRA_API_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    ):
        assert leaked not in env, f"{leaked} must not reach the agent"
    # Essentials + broker routing kept.
    assert env["HOME"] == "/Users/x"
    assert env["PATH"] == "/usr/bin"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9/anthropic"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9"


def test_wrap_unchanged_when_not_jail(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    argv = ["claude", "-p", "--permission-mode", "bypassPermissions"]
    assert sandbox.wrap(argv, Path("/w")) == argv


def test_wrap_jail_builds_sandbox_exec(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    out = sandbox.wrap(["claude", "-p"], tmp_path)
    assert out[0] == "sandbox-exec"
    assert "-f" in out and str(sandbox._JAIL_PROFILE) in out
    assert f"_BASE={sandbox._BASE_PROFILE}" in out
    assert any(a.startswith("_PROJECT_DIR=") for a in out)
    # Original command preserved at the tail.
    assert out[-2:] == ["claude", "-p"]


def test_wrap_jail_degrades_without_sandbox_exec(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    argv = ["claude", "-p"]
    # No sandbox-exec (non-macOS) degrades to bare argv; env is still curated.
    assert sandbox.wrap(argv, Path("/w")) == argv


def test_vendored_profiles_present():
    assert sandbox._JAIL_PROFILE.is_file()
    assert sandbox._BASE_PROFILE.is_file()
