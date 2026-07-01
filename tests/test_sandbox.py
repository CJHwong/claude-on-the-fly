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
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    monkeypatch.delenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    out = sandbox.wrap(["claude", "-p"], tmp_path)
    assert out[0] == "sandbox-exec"
    assert "-f" in out and str(sandbox._JAIL_PROFILE) in out
    assert f"_BASE={sandbox._BASE_PROFILE}" in out
    assert any(a.startswith("_PROJECT_DIR=") for a in out)
    # Default loopback stays wide open (all ports) so dev servers work.
    assert "_LOOPBACK=localhost:*" in out
    # Default base does not reference _EXTRA_*, so none are passed.
    assert not any(a.startswith("_EXTRA_") for a in out)
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
    assert sandbox._DENY_MOST_PROFILE.is_file()


# --- Agnostic sandbox guidance in the system prompt ---


def test_guidance_empty_when_off(monkeypatch):
    monkeypatch.delenv("COTF_SANDBOX", raising=False)
    assert sandbox.agent_guidance(Path("/w")) == ""


def test_guidance_env_mode_only_mentions_curation(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    text = sandbox.agent_guidance(Path("/w"))
    assert "curated environment" in text
    # env mode has no file/network denials, so it must not claim any.
    assert "Operation not permitted" not in text


def test_guidance_jail_covers_all_denial_scenarios(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    monkeypatch.delenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    # recognition
    assert "Operation not permitted" in text and "Permission denied" in text
    assert "451" in text
    # every denial category has a scenario + remedy
    assert "COTF_SANDBOX_EXTRA_PATHS" in text  # file-read remedy
    assert "write profile" in text  # file-write remedy
    assert "broker route" in text  # network remedy
    assert "security find-generic-password" in text  # keychain scenario
    # allow-most default describes secret reads as the blocked set
    assert "reads of secrets are blocked" in text


def test_guidance_deny_most_lists_granted_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_FS", "deny-most")
    monkeypatch.setenv("COTF_SANDBOX_EXTRA_PATHS", "/opt/grantme")
    text = sandbox.agent_guidance(tmp_path)
    assert "read only these paths" in text
    assert str(tmp_path.resolve()) in text
    assert "/opt/grantme" in text


def test_guidance_broker_only_loopback_note(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:5/anthropic")
    assert "ONLY the local broker" in sandbox.agent_guidance(tmp_path)


def test_build_system_prompt_appends_guidance_only_when_on(monkeypatch, tmp_path):
    from claude_on_the_fly.agent import build_system_prompt

    monkeypatch.delenv("COTF_SANDBOX", raising=False)
    assert "## Sandbox" not in build_system_prompt("slack", "u", "dm", tmp_path)
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    assert "## Sandbox" in build_system_prompt("slack", "u", "dm", tmp_path)


# --- Slice 2: deny-most base selection + operator read grants ---


def test_fs_base_defaults_to_allow_most(monkeypatch):
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    assert sandbox._fs_base_profile() == sandbox._BASE_PROFILE


def test_fs_base_deny_most_swaps_profile(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_FS", "deny-most")
    assert sandbox._fs_base_profile() == sandbox._DENY_MOST_PROFILE


def test_wrap_deny_most_passes_capped_extra_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_FS", "deny-most")
    # Four grants supplied; only three fit (SBPL has no arrays).
    monkeypatch.setenv("COTF_SANDBOX_EXTRA_PATHS", "/a:/b:/c:/d")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    out = sandbox.wrap(["claude"], tmp_path)
    assert f"_BASE={sandbox._DENY_MOST_PROFILE}" in out
    extras = [a for a in out if a.startswith("_EXTRA_")]
    # Exactly three slots, always filled (unused padded with the project dir).
    assert sorted(a.split("=", 1)[0] for a in extras) == [
        "_EXTRA_1",
        "_EXTRA_2",
        "_EXTRA_3",
    ]
    assert "_EXTRA_1=/a" in out and "_EXTRA_2=/b" in out and "_EXTRA_3=/c" in out
    assert "/d" not in " ".join(out)  # the 4th grant is dropped


def test_wrap_deny_most_pads_unused_slots_with_project(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_FS", "deny-most")
    monkeypatch.delenv("COTF_SANDBOX_EXTRA_PATHS", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    out = sandbox.wrap(["claude"], tmp_path)
    project = str(tmp_path.resolve())
    # No grants => all three slots resolve to the (already-allowed) project dir.
    assert out.count(f"_EXTRA_1={project}") == 1
    assert f"_EXTRA_2={project}" in out and f"_EXTRA_3={project}" in out


# --- Slice 3: loopback narrowing ---


def test_loopback_open_by_default(monkeypatch):
    monkeypatch.delenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", raising=False)
    assert sandbox._loopback_spec() == "localhost:*"


def test_loopback_narrows_to_broker_port(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:54321/anthropic")
    assert sandbox._loopback_spec() == "localhost:54321"


def test_loopback_stays_open_when_no_broker_port(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    # Fail-safe: never lock the agent out of a broker it might need.
    assert sandbox._loopback_spec() == "localhost:*"


def test_broker_port_reads_any_base_url(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9911/openai")
    assert sandbox._broker_port() == "9911"
