"""sandbox: env curation and seatbelt wrapping, gated by COTF_SANDBOX."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from claude_on_the_fly import agent, egress, sandbox


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


def test_session_overrides_reach_the_agent_with_the_sandbox_off(monkeypatch):
    """Sandboxing and approvals are independent settings, so `off` must not eat the
    session env. It used to: agent_env() returned None before the overrides were
    read, which dropped the approval service's own endpoint and left claude-pty
    naming its tmux session after its pid -- a turn stuck at a dialog whose pane
    the daemon could not find. The values are ephemeral-port loopback URLs, so
    there is no static default that could stand in for them.
    """
    monkeypatch.delenv("COTF_SANDBOX", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-curated-in-this-mode")
    token = sandbox.session_env(
        {
            "COTF_APPROVE_URL": "http://127.0.0.1:56188/decide",
            "COTF_APPROVE_NOTIFY_URL": "http://127.0.0.1:56188/notify",
            "CLAUDE_PTY_TMUX_SESSION": "cotf-pty-42-abcd1234",
            "CLAUDE_PTY_NO_TMUX": "0",
        }
    )
    try:
        env = sandbox.agent_env()
    finally:
        sandbox.reset_session_env(token)

    assert env is not None
    assert env["COTF_APPROVE_URL"] == "http://127.0.0.1:56188/decide"
    assert env["COTF_APPROVE_NOTIFY_URL"] == "http://127.0.0.1:56188/notify"
    assert env["CLAUDE_PTY_TMUX_SESSION"] == "cotf-pty-42-abcd1234"
    assert env["CLAUDE_PTY_NO_TMUX"] == "0"
    # Off means off: this mode withholds nothing, so the daemon env is inherited
    # whole rather than rebuilt from the passthrough allowlist.
    assert env["ANTHROPIC_API_KEY"] == "sk-not-curated-in-this-mode"


def test_a_session_override_does_not_outlive_its_token(monkeypatch):
    """The ContextVar is per turn. If a reset left the value behind, the next
    session would inherit the previous one's approval endpoint."""
    monkeypatch.delenv("COTF_SANDBOX", raising=False)
    token = sandbox.session_env({"COTF_APPROVE_URL": "http://127.0.0.1:1/decide"})
    sandbox.reset_session_env(token)
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
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
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


def test_wrap_jail_refuses_without_sandbox_exec(monkeypatch):
    """A configured jail that cannot apply must fail, not quietly run unjailed.

    This used to hand back the bare argv, on the reading that a missing
    sandbox-exec meant "not macOS". With a Linux jail that reading is gone, and
    what is left is a turn running with no sandbox because of a warning nobody
    reads until afterwards.
    """
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(sandbox.SandboxBoundaryError, match="sandbox-exec"):
        sandbox.wrap(["claude", "-p"], Path("/w"))


def test_wrap_jail_refuses_on_linux_without_bwrap(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(sandbox.SandboxBoundaryError, match="bubblewrap"):
        sandbox.wrap(["codex", "exec"], Path("/w"))


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
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    monkeypatch.delenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    # recognition
    assert "Operation not permitted" in text and "Permission denied" in text
    assert "451" in text
    # every denial category has a scenario + remedy
    assert (
        "sandbox.extra_paths" in text
    )  # file-read remedy, named as the operator sets it
    assert "write profile" in text  # file-write remedy
    # Network remedy is now an approval, not a config change: the agent is told
    # the request pauses for the operator and that a 403 means they declined.
    assert "operator is asked" in text
    assert "Do not retry in a loop" in text
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
    # macOS wording. Linux never uses it -- the namespace reaches the brokered
    # services and external hosts through the proxy, so "external hosts are
    # blocked" would be wrong there. Its counterpart is
    # test_guidance_network_line_is_accurate_on_linux.
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
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
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
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
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    out = sandbox.wrap(["claude"], tmp_path)
    project = str(tmp_path.resolve())
    # No grants => all three slots resolve to the (already-allowed) project dir.
    assert out.count(f"_EXTRA_1={project}") == 1
    assert f"_EXTRA_2={project}" in out and f"_EXTRA_3={project}" in out


# --- Slice 3: loopback narrowing ---


def _clear_loopback_env(monkeypatch):
    for var in (
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
        "HTTPS_PROXY",
        "COTF_CMD_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)


def test_loopback_open_by_default(monkeypatch):
    monkeypatch.delenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", raising=False)
    assert sandbox._loopback_specs() == ("localhost:*",) * sandbox._LOOPBACK_SLOTS


def test_loopback_narrows_to_broker_port(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    _clear_loopback_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:54321/anthropic")
    # Only the broker port is known, so spare slots repeat it rather than
    # widening back to every loopback port.
    assert sandbox._loopback_specs() == ("localhost:54321",) * sandbox._LOOPBACK_SLOTS


def test_loopback_narrows_to_every_known_service(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    _clear_loopback_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:54321/anthropic")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:54322")
    monkeypatch.setenv("COTF_CMD_ENDPOINT", "http://127.0.0.1:54323")
    monkeypatch.setenv("COTF_APPROVE_URL", "http://127.0.0.1:54324/decide")
    assert sandbox._loopback_specs() == (
        "localhost:54321",
        "localhost:54322",
        "localhost:54323",
        "localhost:54324",
    )


def test_loopback_reads_the_per_session_env_not_just_os_environ(monkeypatch):
    """Regression: per-session egress proxies publish HTTPS_PROXY into the
    ContextVar, not os.environ. Reading os.environ narrowed the jail to the
    broker port alone and locked the agent out of the proxy it was handed."""
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    _clear_loopback_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:5001/anthropic")
    token = sandbox.session_env(
        {
            "HTTPS_PROXY": "http://127.0.0.1:6002",
            "COTF_CMD_ENDPOINT": "http://127.0.0.1:7003",
        }
    )
    try:
        # Three services and four slots, so the spare repeats the first
        # port -- a duplicate allow, which is harmless.
        assert sandbox._loopback_specs() == (
            "localhost:5001",
            "localhost:6002",
            "localhost:7003",
            "localhost:5001",
        )
    finally:
        sandbox.reset_session_env(token)


def test_loopback_narrows_to_egress_port_alone(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    _clear_loopback_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:54322")
    # A deployment with no credentialed provider still gets a narrowed jail.
    assert sandbox._loopback_specs() == ("localhost:54322",) * sandbox._LOOPBACK_SLOTS


def test_loopback_warns_rather_than_silently_dropping_a_service(monkeypatch, caplog):
    """Guard for a future fifth loopback service. Unreachable through env today
    (the broker serves every route on one port, so the four sources fill the four
    slots exactly), so drive _loopback_ports directly."""
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    monkeypatch.setattr(sandbox, "_loopback_ports", lambda: ["1", "2", "3", "4", "5"])
    with caplog.at_level("WARNING"):
        specs = sandbox._loopback_specs()
    # Five services, four slots: the drop must be loud, never silent.
    assert specs == (
        "localhost:1",
        "localhost:2",
        "localhost:3",
        "localhost:4",
    )
    assert "unreachable" in caplog.text


def test_loopback_ports_collapses_duplicate_ports(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    _clear_loopback_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9000/anthropic")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9000")
    assert sandbox._loopback_ports() == ["9000"]


def test_loopback_stays_open_when_no_service_is_known(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    _clear_loopback_env(monkeypatch)
    # Fail-safe: never lock the agent out of a broker it might need.
    assert sandbox._loopback_specs() == ("localhost:*",) * sandbox._LOOPBACK_SLOTS


def test_wrap_jail_passes_three_loopback_slots(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    out = sandbox.wrap(["claude"], tmp_path)
    for param in ("_LOOPBACK=", "_LOOPBACK_ALT=", "_LOOPBACK_ALT2="):
        assert any(a.startswith(param) for a in out), param


def test_loopback_ports_reads_any_base_url(monkeypatch):
    _clear_loopback_env(monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9911/openai")
    assert sandbox._loopback_ports() == ["9911"]


def test_session_env_layers_over_the_allowlist(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-be-dropped")
    token = sandbox.session_env({"HTTPS_PROXY": "http://127.0.0.1:5555"})
    try:
        env = sandbox.agent_env() or {}
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:5555"
        # Layering must not reopen the allowlist.
        assert "AWS_SECRET_ACCESS_KEY" not in env
    finally:
        sandbox.reset_session_env(token)


def test_session_env_is_scoped_to_its_token(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    token = sandbox.session_env({"HTTPS_PROXY": "http://127.0.0.1:6666"})
    sandbox.reset_session_env(token)
    # After reset the override is gone, so one turn's proxy cannot bleed into
    # the next turn's spawn.
    assert "HTTPS_PROXY" not in (sandbox.agent_env() or {})


async def test_session_env_does_not_leak_across_concurrent_tasks(monkeypatch):
    """asyncio copies context per task, which is what makes a ContextVar safe
    here: two chats running at once must not see each other's proxy."""
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    seen: dict[str, str | None] = {}

    async def turn(name: str, port: int) -> None:
        token = sandbox.session_env({"HTTPS_PROXY": f"http://127.0.0.1:{port}"})
        try:
            await asyncio.sleep(0)
            seen[name] = (sandbox.agent_env() or {}).get("HTTPS_PROXY")
        finally:
            sandbox.reset_session_env(token)

    await asyncio.gather(turn("a", 1111), turn("b", 2222))
    assert seen == {
        "a": "http://127.0.0.1:1111",
        "b": "http://127.0.0.1:2222",
    }


def test_env_editor_restarts_frontends_when_sandbox_vars_change():
    """Editing these in the TUI must prompt a restart, or the new policy
    silently does not take effect: orchestrator.run reads them at startup."""
    from claude_on_the_fly.checks import SANDBOX_ENV_VARS
    from claude_on_the_fly.tui.env_editor import EnvDiff, affected_daemons

    for var in SANDBOX_ENV_VARS:
        affected = affected_daemons(EnvDiff(changed={var: ("off", "jail")}))
        assert affected == {"telegram", "slack"}, f"{var} restarts {affected}"


def test_guidance_teaches_the_403_shape_the_agent_will_actually_see(
    monkeypatch, tmp_path
):
    """No client surfaces a CONNECT response body, so the reason phrase is the only
    channel. Pointing the agent at the status line is what makes it readable at
    all; without this it sees a bare proxy error and has nothing to report."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    assert "403 Forbidden by egress policy:" in text
    # The exact prefix the proxy emits, so the two cannot drift apart.
    assert egress._NEVER_ASK.hint.startswith("Forbidden by egress policy:")
    assert egress._NEVER_ASK.hint in text


def test_guidance_warns_keychain_denial_is_not_an_eperm(monkeypatch, tmp_path):
    """Verified against a live run: a denied keychain read reports "item could
    not be found", not EPERM, so an agent taught EPERM-means-policy would read
    it as "the credential does not exist" and hunt for it elsewhere."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    assert "could not be found" in text
    assert "does not exist" in text


def test_guidance_teaches_the_linux_signatures_not_the_macos_ones(
    monkeypatch, tmp_path
):
    """The errno lesson inverts across platforms, so shipping one text would be
    actively wrong on the other.

    Measured against a live bubblewrap jail: a denied read reports "No such file
    or directory" because the path is never mounted, and a denied write reports
    "Read-only file system". An agent taught the seatbelt story would read the
    first as "this file does not exist on the machine" and go looking elsewhere,
    which is the exact failure the macOS keychain bullet exists to prevent.
    """
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    assert "No such file or directory" in text
    assert "Read-only file system" in text
    assert "do not go looking for it elsewhere" in text
    # The seatbelt-only wording must not leak onto Linux.
    assert "Operation not permitted" not in text
    assert "security find-generic-password" not in text
    # D-Bus is the Linux keychain path, and it is gone with /run.
    assert "D-Bus" in text


def test_guidance_separates_read_scope_from_write_scope(monkeypatch, tmp_path):
    """Regression from a real codex transcript: the agent refused to read
    ~/.gitconfig, which the profile permits, because "Read and write the
    workspace" led the allowed list and it took that as the boundary. Four turns,
    zero tool calls, including one refusal of a permitted operation."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    assert "Reading:" in text and "Writing:" in text
    # The conflating phrasing must not come back.
    assert "Read and write the workspace" not in text
    assert "different scopes" in text
    # And it is told to try rather than pre-decline, or denials stay invisible
    # and permitted operations get refused.
    assert "rather than declining in advance" in text


def test_guidance_write_scope_names_the_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    assert str(tmp_path.resolve()) in text


# --- brokered CLIs, and the remedy the agent relays for one that is not ---


def test_guidance_names_the_brokered_commands(monkeypatch, tmp_path):
    """An agent that knows which CLIs are brokered can tell a policy boundary from
    a broken tool. Without the list it has to guess from the failure."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    monkeypatch.setattr(
        "claude_on_the_fly.commands.shimmed_names", lambda: ["acli", "gh"]
    )
    text = sandbox.agent_guidance(tmp_path)
    assert "work normally: acli, gh" in text


def test_guidance_says_so_when_nothing_is_brokered(monkeypatch, tmp_path):
    """A deployment with neither gh nor acli installed must not be told an empty
    list of tools "work normally"."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    monkeypatch.setattr("claude_on_the_fly.commands.shimmed_names", lambda: [])
    text = sandbox.agent_guidance(tmp_path)
    assert "No credentialed CLI is brokered" in text
    assert "work normally:" not in text


def test_guidance_remedy_for_an_unbrokered_cli_is_the_settings_file(
    monkeypatch, tmp_path
):
    """It used to point at COTF_SANDBOX_EXTRA_PATHS, which is the fix commands.py
    documents as broken: granting the credential path either leaves the tool broken
    or hands its token to the session. The remedy is the commands section."""
    from claude_on_the_fly import settings

    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    assert "adds the tool to the `commands:` section" in text
    assert str(settings.operator_settings()) in text
    assert "the remedy is NOT a read grant" in text


def test_guidance_forbids_routing_around_an_unbrokered_cli(monkeypatch, tmp_path):
    """The observed failure this whole subsystem exists for: denied gh, the agent
    reached the same private repos through the model provider's own GitHub
    integration instead of reporting the block."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    assert "do not reach the same service by another route" in text
    assert "provider-side integration" in text


# --- diagnostic logging ---


def test_jail_spawn_is_logged(monkeypatch, tmp_path, caplog):
    """The one positive record that the jail was applied. Without it, a run with
    COTF_SANDBOX unset looks identical to a jailed one: both are free of denials,
    and no denials also reads as success."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    # Stubbed rather than skipped off macOS: the line under test is the log record,
    # not sandbox-exec's presence, so there is no reason for this one to be
    # platform-gated.
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    # The record comes from sandbox_macos now: the seatbelt argv builder moved
    # there, and the log line moved with the code that emits it.
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox_macos"):
        sandbox.wrap(["/bin/echo", "hi"], tmp_path)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "sandbox: jailed echo" in logged
    assert "fs=fs-allow-reads.sb" in logged
    assert str(tmp_path.resolve()) in logged


def test_env_only_mode_logs_no_jail_line(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"):
        sandbox.wrap(["/bin/echo", "hi"], tmp_path)
    assert "jailed" not in "\n".join(r.getMessage() for r in caplog.records)


def test_curated_env_logs_names_never_values(monkeypatch, caplog):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-appear")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    with caplog.at_level("DEBUG", logger="claude_on_the_fly.sandbox"):
        env = sandbox.agent_env() or {}
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "env curated" in logged
    assert "'LANG'" in logged
    # The record of "the secret did not reach the agent" must not itself leak it.
    assert "sk-ant-must-not-appear" not in logged
    assert "ANTHROPIC_API_KEY" not in env


async def test_verify_denials_is_inert_when_not_jailed(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    assert await sandbox.verify_denials() == {}


async def test_verify_denials_reports_each_probe(
    monkeypatch, tmp_path, caplog, original_home
):
    """Real sandbox-exec run against the real home.

    conftest redirects HOME to a tmpdir, which would make every probe path absent
    and the assertion below vacuous — it would pass because nothing is there, not
    because the profile denies anything. So this one test reaches for the real
    home on purpose.
    """
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(original_home))
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"):
        results = await sandbox.verify_denials(tmp_path)
    assert results, "expected at least one probe"
    assert sandbox.READABLE not in results.values(), f"leaked: {results}"
    # At least one real deny must have been exercised, or the run proved nothing.
    assert sandbox.DENIED in results.values(), f"nothing actually denied: {results}"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "confirmed denied" in logged


async def test_absent_path_is_not_counted_as_denied(monkeypatch, tmp_path, caplog):
    """An absent credential store is not evidence the boundary works.

    conftest's tmpdir HOME gives exactly that situation, so this asserts the
    honest outcome rather than a false pass.
    """
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"):
        results = await sandbox.verify_denials(tmp_path)
    assert set(results.values()) == {sandbox.ABSENT}, results
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "deny untested" in logged
    assert f"0/{len(sandbox._DENY_PROBES)} probed" in logged


@pytest.fixture
def probe_paths_exist():
    """Put every credential path the deny probes look for on disk.

    The probes settle absent-versus-denied from *outside* the jail now, because
    bubblewrap hides a path by not mounting it and the resulting "No such file or
    directory" is indistinguishable from the file never having been there. A spec
    that is not on disk is therefore answered ABSENT without spawning anything,
    so a test asserting on probe behaviour has to create the files first.
    """
    created = []
    for spec in sandbox._deny_probe_specs():
        path = Path(os.path.expanduser(spec))
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("probe fixture\n")
        created.append(path)
    yield
    for path in created:
        path.unlink(missing_ok=True)


async def test_broken_profile_is_not_reported_as_absent(
    monkeypatch, tmp_path, caplog, probe_paths_exist
):
    """A profile that will not parse is a hard failure, not a missing file.

    The first version of verify_denials reported it as six "absent" paths, which
    reads as benign. Found by deliberately breaking the profile, not by review.
    """
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    broken = tmp_path / "broken.sb"
    broken.write_text("(version 1)\n(this-is-not-a-real-operation\n")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_JAIL_PROFILE", broken)
    with (
        caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"),
        pytest.raises(sandbox.SandboxBoundaryError, match="refusing to start"),
    ):
        await sandbox.verify_denials(tmp_path)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "the profile is broken" in logged
    assert "did not load" in logged
    # Must not claim anything about the boundary.
    assert "confirmed denied" not in logged
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_home_param_is_realpathed(monkeypatch, tmp_path):
    """Seatbelt matches resolved paths, so an unresolved _HOME matches nothing.

    On a host whose home is behind a symlink the base profile's credential denies
    would all no-op while the profile still loaded and the log still said
    "jailed". _TMPDIR and _PROJECT_DIR were already realpath'd; _HOME was not.
    """
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home)
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    monkeypatch.setenv("HOME", str(linked_home))
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    argv = sandbox.wrap(["/bin/echo", "hi"], tmp_path)
    home_param = next(arg for arg in argv if arg.startswith("_HOME="))
    assert home_param == f"_HOME={real_home}"
    assert str(linked_home) not in home_param


def test_data_dir_param_is_realpathed(monkeypatch, tmp_path):
    """The profile's data-dir rules are parameterized on _DATA_DIR, so a
    redirected DATA_DIR (COTF_DATA_DIR) reached through a symlink gets the same
    realpath treatment as _HOME. An unresolved param would silently match
    nothing: the memory grants and the .env/logs denies scoped to it would
    no-op while the profile still loaded and the log still said "jailed"."""
    real = tmp_path / "real-data"
    real.mkdir()
    linked = tmp_path / "linked-data"
    linked.symlink_to(real)
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", linked)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    argv = sandbox.wrap(["/bin/echo", "hi"], tmp_path)
    data_param = next(arg for arg in argv if arg.startswith("_DATA_DIR="))
    assert data_param == f"_DATA_DIR={real}"
    assert str(linked) not in data_param


async def test_credential_denies_fire_under_a_symlinked_home(monkeypatch, tmp_path):
    """End-to-end version of the above: a real sandbox-exec run proves the deny
    matches when home is reached through a symlink."""
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    real_home = Path(os.path.realpath(tmp_path)) / "home"
    (real_home / ".aws").mkdir(parents=True)
    (real_home / ".aws" / "credentials").write_text("CANARY\n")
    linked = Path(os.path.realpath(tmp_path)) / "home-link"
    linked.symlink_to(real_home)
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(linked))
    argv = sandbox.wrap(["/bin/cat", str(linked / ".aws" / "credentials")], tmp_path)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env={"HOME": str(linked), "PATH": "/usr/bin:/bin"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert b"CANARY" not in out, "credential deny did not fire through the symlink"
    assert b"not permitted" in err.lower()


async def test_daemon_env_file_is_denied_to_the_agent(monkeypatch, tmp_path):
    """Env curation strips SLACK_TOKEN / TELEGRAM_BOT_TOKEN from the agent's
    environment. Leaving the file that holds them readable defeated that, and it
    was readable until a probe checked.

    With the Slack user token a hijacked agent could post as the operator, which
    means answering its own approval prompts: the gate re-checks the sender, and
    the sender would have been legitimate.
    """
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    home = Path(os.path.realpath(tmp_path)) / "home"
    data = home / ".claude-on-the-fly"
    (data / "logs").mkdir(parents=True)
    (data / "memory").mkdir()
    (data / "shims").mkdir()
    (data / ".env").write_text("SLACK_TOKEN=xoxb-CANARY\nTELEGRAM_BOT_TOKEN=CANARY\n")
    (data / "logs" / "slack.log").write_text("CANARY-CONVERSATION\n")
    (data / "memory" / "note.md").write_text("ordinary agent memory\n")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(home))

    async def read(path: Path) -> tuple[int, bytes]:
        argv = sandbox.wrap(["/bin/cat", str(path)], tmp_path)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await proc.communicate()
        return proc.returncode or 0, out

    rc, out = await read(data / ".env")
    assert b"CANARY" not in out, "the daemon's own tokens are readable"
    assert rc != 0

    rc, out = await read(data / "logs" / "slack.log")
    assert b"CANARY-CONVERSATION" not in out, "prior conversations are readable"

    # Memory must stay reachable: the agent is meant to read it.
    rc, out = await read(data / "memory" / "note.md")
    assert rc == 0 and b"ordinary agent memory" in out


class TestInertWhenOff:
    """ "zero change for anyone who hasn't opted in" is a stated design goal in this
    module's docstring, so it gets a test rather than a promise."""

    @pytest.fixture(autouse=True)
    def _sandbox_off(self, monkeypatch):
        for var in (
            "COTF_SANDBOX",
            "COTF_SANDBOX_FS",
            "COTF_SANDBOX_EXTRA_PATHS",
            "COTF_SANDBOX_BROKER_ONLY_LOOPBACK",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_env_is_inherited_unchanged(self):
        # None is what create_subprocess_exec treats as "inherit os.environ".
        assert sandbox.agent_env() is None

    def test_argv_is_not_wrapped(self, tmp_path):
        argv = ["claude", "-p", "hello"]
        assert (
            sandbox.wrap(argv, tmp_path) is argv or sandbox.wrap(argv, tmp_path) == argv
        )

    async def test_no_deny_probes_run(self, tmp_path):
        assert await sandbox.verify_denials(tmp_path) == {}

    def test_nothing_is_logged(self, tmp_path, caplog):
        """No spawn record, no probe lines, no curation line."""
        with caplog.at_level("DEBUG", logger="claude_on_the_fly.sandbox"):
            sandbox.agent_env()
            sandbox.wrap(["claude"], tmp_path)
        assert caplog.records == []

    def test_guidance_is_empty_so_the_prompt_is_untouched(self, tmp_path):
        assert sandbox.agent_guidance(tmp_path) == ""

    def test_profiles_are_not_read(self, tmp_path, monkeypatch):
        """A missing or broken profile must not matter when the sandbox is off."""
        monkeypatch.setattr(sandbox, "_JAIL_PROFILE", tmp_path / "does-not-exist.sb")
        monkeypatch.setattr(sandbox, "_BASE_PROFILE", tmp_path / "also-missing.sb")
        argv = ["claude", "-p", "hello"]
        assert sandbox.wrap(argv, tmp_path) == argv


# --- probe outcomes that a real machine will not produce on demand ---


async def test_a_probe_that_cannot_be_spawned_says_nothing_either_way(
    monkeypatch, tmp_path, caplog, probe_paths_exist
):
    """Not an outcome: a probe that never ran is evidence about the probe, not
    about the boundary, so it must not land in the results dict at all."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")

    async def cannot_spawn(*_args, **_kwargs):
        raise OSError("too many open files")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cannot_spawn)
    with caplog.at_level("WARNING", logger="claude_on_the_fly.sandbox"):
        assert await sandbox.verify_denials(tmp_path) == {}
    assert "failed to run" in "\n".join(r.getMessage() for r in caplog.records)


async def test_a_probe_that_hangs_is_abandoned_not_awaited_forever(
    monkeypatch, tmp_path, caplog, probe_paths_exist
):
    """These run on the daemon's startup path, before it serves anything."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")

    class HangingProbe:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(3600)
            return b"", b""

    async def spawn_hanging(*_args, **_kwargs):
        return HangingProbe()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_hanging)
    monkeypatch.setattr(asyncio, "wait_for", _immediate_timeout)
    with caplog.at_level("WARNING", logger="claude_on_the_fly.sandbox"):
        assert await sandbox.verify_denials(tmp_path) == {}
    assert "failed to run" in "\n".join(r.getMessage() for r in caplog.records)


async def _immediate_timeout(awaitable, timeout=None):
    """Stand-in for asyncio.wait_for that always expires."""
    task = asyncio.ensure_future(awaitable)
    task.cancel()
    raise TimeoutError


async def test_a_readable_credential_path_is_an_error_not_a_pass(
    monkeypatch, tmp_path, caplog, probe_paths_exist
):
    """The one outcome that means the boundary is not in force. It cannot be
    produced on a correctly configured machine, so it is faked here rather than
    left untested: a silent READABLE is the whole failure this function exists to
    catch."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")

    class ReadableProbe:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def spawn_readable(*_args, **_kwargs):
        return ReadableProbe()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_readable)
    with (
        caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"),
        pytest.raises(sandbox.SandboxBoundaryError, match="refusing to start"),
    ):
        await sandbox.verify_denials(tmp_path)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "PROBE FAIL" in logged
    assert "credential path(s) READABLE inside the jail" in logged
    # A leak must never be reported alongside a reassuring count.
    assert "confirmed denied" not in logged


async def test_probes_run_concurrently_not_one_after_another(
    monkeypatch, tmp_path, probe_paths_exist
):
    """Six independent subprocesses, each with its own 15s ceiling, on the
    daemon's startup path. In sequence the worst case was a minute and a half of a
    daemon that had not begun serving."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    live = 0
    peak = 0

    class SlowProbe:
        returncode = 1

        async def communicate(self):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1
            return b"", b"operation not permitted"

    async def spawn_slow(*_args, **_kwargs):
        return SlowProbe()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_slow)
    results = await sandbox.verify_denials(tmp_path)
    assert peak == len(sandbox._DENY_PROBES), f"peak concurrency was {peak}"
    assert set(results.values()) == {sandbox.DENIED}


# --- shim PATH routing ---


def test_an_unreadable_shim_dir_leaves_path_alone(monkeypatch, tmp_path):
    """Prepending a directory that cannot be listed would put an unusable entry
    first on the agent's PATH."""
    monkeypatch.setenv("COTF_SANDBOX", "env")
    # Has to exist, or the is_dir() check short-circuits before any listing.
    sandbox.shim_dir().mkdir(parents=True, exist_ok=True)

    def cannot_list(_self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", cannot_list)
    env = sandbox._with_shims_on_path({"PATH": "/usr/bin"})
    assert env["PATH"] == "/usr/bin"


# --- the agent's own memory has to survive the jail ---


def _run_jailed(argv: list[str], workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        sandbox.wrap(argv, workspace),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("fs_base", ["", "deny-most"])
def test_memory_is_writable_under_the_jail(monkeypatch, tmp_path, fs_base):
    """system_prompt.md points the agent at {memory_root}/users/<sender>/*.md and
    tells it to keep them current. That path is outside the workspace, so without
    an explicit grant every memory write failed with "Operation not permitted" and
    the feature was silently off under `jail` — with nothing in the log, because
    macOS cannot report a seatbelt denial."""
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_FS", fs_base)
    memory = agent.MEMORY_DIR / "users" / "someone"
    memory.mkdir(parents=True, exist_ok=True)
    target = memory / "profile.md"
    done = _run_jailed(
        ["/bin/sh", "-c", f"echo remembered > {target} && cat {target}"], tmp_path
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "remembered"


@pytest.mark.parametrize("profile", [sandbox._BASE_PROFILE, sandbox._DENY_MOST_PROFILE])
def test_memory_grant_is_scoped_to_memory_not_the_whole_data_dir(profile):
    """The grant names memory/ rather than the data dir it sits in, because the
    siblings are .env (SLACK_TOKEN and TELEGRAM_BOT_TOKEN, with which the agent
    could answer its own approval prompts) and logs/ (every prior conversation).

    Structural rather than a live probe: the tmpdir HOME the suite runs under sits
    inside _TMPDIR, which the profile grants wholesale, so a live read there would
    pass for the wrong reason. The live counterpart is below.
    """
    text = profile.read_text()
    grants = re.findall(
        r'\(allow file-(?:read|write)\*.*?\(param "_DATA_DIR"\) "([^"]*)"', text
    )
    assert grants, "expected at least one data-dir grant"
    for suffix in grants:
        assert suffix.startswith("/memory") or suffix.startswith("/shims"), (
            f"data-dir grant {suffix!r} is wider than memory/ and shims/"
        )


def test_the_daemons_own_env_file_is_unreadable_in_the_jail(
    monkeypatch, tmp_path, original_home
):
    """The live counterpart, against the real home so the deny is the reason the
    read fails rather than the file being absent. Reads only; never creates or
    modifies anything under the real home."""
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    secrets = original_home / ".claude-on-the-fly" / ".env"
    if not secrets.is_file():
        pytest.skip("no real .env on this machine to probe")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(original_home))
    done = _run_jailed(["/bin/cat", str(secrets)], tmp_path)
    assert done.returncode != 0, ".env was readable inside the jail"
    assert "not permitted" in done.stderr.lower(), done.stderr


# Measured: what a real `codex exec` turn wrote under ~/.codex. A grant missing from
# here is a codex that cannot run; a grant here that codex does not need is attack
# surface, so the list is the measurement and not a guess.
_CODEX_MUST_WRITE = (
    "/.codex/sessions",
    "/.codex/tmp",
    "/.codex/cache",
    "/.codex/log",
    "/.codex/shell_snapshots",
    "/.codex/plugins/cache",
    "/.codex/models_cache.json",
    "/.codex/auth.json",
)

# Files that decide what codex executes, or what it is told to do. None was written by
# a real turn.
_CODEX_MUST_NOT_WRITE = (
    "hooks.json",
    "config.toml",
    "rules/x.rules",
    "AGENTS.md",
    "history.jsonl",
    "plugins/manifest.toml",
    "a-config-codex-has-not-invented-yet.toml",
)


@pytest.mark.parametrize("profile", [sandbox._BASE_PROFILE, sandbox._DENY_MOST_PROFILE])
def test_the_cotf_env_deny_covers_any_depth(profile):
    """The tokens must not be readable from a copy one directory down.

    `~/.claude-on-the-fly` is deliberately readable -- the agent's workspace and memory
    live under it -- so the tokens are covered by a regex rather than a subpath deny.
    The regex used to be anchored at the directory root, which left
    `pre-migration-backup-*/.env` and a syncer's `sub/.env` readable while the file an
    operator actually thinks about was protected. Asserted on the profile text because
    the suite's HOME is a tmpdir the profile grants wholesale, so a live read there
    would succeed for the wrong reason.
    """
    text = profile.read_text()
    if profile == sandbox._BASE_PROFILE:
        # The default location's own deny lives only in allow-reads; deny-most
        # covers the default location through its blanket _HOME opacity.
        default_rule = next(
            line
            for line in text.splitlines()
            if "deny file-read*" in line
            and "claude-on-the-fly" in line
            and ".env" in line
        )
        assert "(.*/)?" in default_rule, default_rule
    # A redirected data dir (COTF_DATA_DIR) is covered by the same-shaped rule
    # scoped to _DATA_DIR in both profiles, so a second daemon's .env is denied
    # wherever the dir sits -- under _HOME, where deny-most is opaque anyway,
    # or outside it, where only this deny reaches.
    data_rule = next(
        line
        for line in text.splitlines()
        if "deny file-read*" in line and "_DATA_DIR" in line and ".env" in line
    )
    assert "(.*/)?" in data_rule, data_rule


def test_the_cotf_env_is_a_verified_denial():
    """A regex deny is the kind that can be narrowed by accident and stay quiet about
    it, and macOS cannot report a seatbelt denial. Probing it per run is the substitute
    for trusting it."""
    assert "~/.claude-on-the-fly/.env" in sandbox._DENY_PROBES


def test_deny_probe_specs_dedupe_the_default_location(monkeypatch):
    """The default location is probed once: the daemon's own .env is the same
    file, and a second probe adds a startup subprocess for nothing."""
    monkeypatch.setattr(
        "claude_on_the_fly.agent.DATA_DIR", Path.home() / ".claude-on-the-fly"
    )
    assert sandbox._deny_probe_specs() == sandbox._DENY_PROBES


def test_deny_probe_specs_append_a_redirected_env(monkeypatch, tmp_path):
    """A redirected data dir probes its own .env in addition to the default
    location's, which then belongs to another daemon on the same machine."""
    redirected = tmp_path / "other-data"
    monkeypatch.setattr("claude_on_the_fly.agent.DATA_DIR", redirected)
    specs = sandbox._deny_probe_specs()
    assert specs == (*sandbox._DENY_PROBES, str(redirected / ".env"))


@pytest.mark.parametrize("profile", [sandbox._BASE_PROFILE, sandbox._DENY_MOST_PROFILE])
def test_the_codex_directory_is_deny_default_not_a_denylist(profile):
    """~/.codex must stay partly writable or codex will not start, but it also holds
    everything deciding what codex executes and what it is told.

    An earlier revision granted the whole directory and denied three files. That list
    was already incomplete: plugins/, history.jsonl and AGENTS.md stayed writable, and
    AGENTS.md is standing instructions codex reads on every later run, so an injected
    agent could leave itself orders that outlive the session. Enumerating the
    dangerous files loses that race every time codex adds one, which is why this is a
    deny with an explicit re-grant instead.

    Structural because the suite's HOME is a tmpdir inside _TMPDIR, which the profile
    grants wholesale, so a live write there would succeed for the wrong reason. The
    live counterpart is below.
    """
    text = profile.read_text()
    assert (
        '(deny file-write* (subpath (string-append (param "_HOME") "/.codex")))' in text
    ), "the ~/.codex write policy is no longer deny-default"
    granted = set(
        re.findall(
            r"\(allow file-write\*\s*\n?\s*\((?:subpath|literal) "
            r'\(string-append \(param "_HOME"\) "([^"]+)"',
            text,
        )
    )
    for path in _CODEX_MUST_WRITE:
        assert path in granted, f"{path} is not granted; codex cannot run without it"
    # The re-grants must not reach back up to the whole directory.
    assert "/.codex" not in granted, "the blanket ~/.codex grant is back"


@pytest.mark.parametrize("target", _CODEX_MUST_NOT_WRITE)
def test_codex_execution_control_paths_are_unwritable_in_the_jail(
    monkeypatch, tmp_path, original_home, target
):
    """Live counterpart, against the real home so the deny is why the write fails
    rather than the path being absent.

    This has to run against the real home: a tmpdir HOME sits under _TMPDIR or
    /private/var/folders, both granted wholesale, so the grant would win and the probe
    would pass for the wrong reason.

    Appends zero bytes so an existing file cannot be altered even if a deny had stopped
    working, and for the paths that do not exist the only possible damage is a stray
    empty file that codex's own parsers would reject rather than act on.
    """
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    if not (original_home / ".codex").is_dir():
        pytest.skip("no real ~/.codex on this machine to probe")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(original_home))
    path = original_home / ".codex" / target
    existed = path.exists()

    done = _run_jailed(["/bin/sh", "-c", f"printf '' >> {path}"], tmp_path)

    assert done.returncode != 0, f"{target} was writable inside the jail"
    assert "not permitted" in done.stderr.lower(), done.stderr
    assert path.exists() == existed, f"the probe changed {target} on disk"


def test_codex_can_still_write_what_a_real_turn_needs(
    monkeypatch, tmp_path, original_home
):
    """The other half: deny-default is only correct if the re-grants are complete.
    Every path here was observed being written by a real `codex exec` turn, so a deny
    creeping over one of them is a codex that cannot run."""
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    sessions = original_home / ".codex" / "sessions"
    if not sessions.is_dir():
        pytest.skip("no real ~/.codex/sessions on this machine to probe")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(original_home))
    probe = sessions / ".cotf-suite-probe"
    try:
        done = _run_jailed(["/bin/sh", "-c", f"printf '' > {probe}"], tmp_path)
        assert done.returncode == 0, f"sessions/ is not writable: {done.stderr}"
        assert probe.exists()
    finally:
        if probe.exists() and probe.stat().st_size == 0:
            probe.unlink()


def test_deny_most_guidance_lists_every_path_the_profile_grants(monkeypatch):
    """The guidance also tells the agent not to narrow its reads, because
    "refusing a read you are actually permitted to make costs the user real work".
    A path missing from this list is one it will decline to try, so the list and
    the profile have to move together."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_FS", "deny-most")
    guidance = sandbox.agent_guidance(Path("/tmp/workspace"))

    profile = sandbox._DENY_MOST_PROFILE.read_text()
    granted = set(re.findall(r'\(allow file-read\*.*?_HOME"\)\s+"(/[^"]+)"', profile))
    assert granted, "expected home-relative read grants in the profile"
    for suffix in granted:
        assert suffix in guidance, (
            f"{suffix} is granted by fs-deny-most.sb but missing from the guidance"
        )


def test_guidance_names_memory_as_writable(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    guidance = sandbox.agent_guidance(Path("/tmp/workspace"))
    assert str(agent.MEMORY_DIR) in guidance
    assert "Writing:" in guidance


def test_an_unrecognised_sandbox_mode_says_so_instead_of_silently_disabling(
    monkeypatch, caplog
):
    """It still resolves to off, because refusing to start would turn a typo into an
    outage. But a misspelled `jial` used to read as "no sandbox at all" with nothing
    anywhere to say so, which is the most expensive way to be wrong about this."""
    monkeypatch.setenv("COTF_SANDBOX", "jial")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.sandbox"):
        assert sandbox.mode() == "off"
    assert "not one of" in caplog.text
    assert "NO sandbox" in caplog.text


def test_an_unset_sandbox_mode_is_silent(monkeypatch, caplog):
    """Choosing off deliberately must not produce an error every startup."""
    monkeypatch.delenv("COTF_SANDBOX", raising=False)
    with caplog.at_level("ERROR", logger="claude_on_the_fly.sandbox"):
        assert sandbox.mode() == "off"
    assert caplog.text == ""


# --- Linux jail: grants, wrapping, relay, preflight ---


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def test_extra_paths_are_uncapped_on_linux(monkeypatch):
    """The cap is a seatbelt artifact -- SBPL has no arrays -- so carrying it onto
    a mount namespace would be inventing a limit to look consistent."""
    monkeypatch.setenv("COTF_SANDBOX_EXTRA_PATHS", "/a:/b:/c:/d:/e")
    assert len(sandbox._extra_read_paths(cap=None)) == 5
    assert len(sandbox._extra_read_paths()) == sandbox._MAX_EXTRA_PATHS


def test_linux_grants_hide_the_data_dir_and_keep_memory(tmp_path):
    grants = sandbox._linux_grants(tmp_path / "ws")
    home = Path(os.path.realpath(Path.home()))
    assert home in grants["opaque"]
    resolved = [Path(os.path.realpath(p)) for p in grants["opaque"]]
    assert Path(os.path.realpath(agent.DATA_DIR)) in resolved
    # memory/ is re-granted deeper than the opaque data dir, so the sibling .env
    # and logs/ stay hidden while the agent's memory stays writable.
    assert Path(os.path.realpath(agent.MEMORY_DIR)) in grants["read_write"]


def test_linux_grants_protect_what_codex_executes_or_is_told(tmp_path):
    grants = sandbox._linux_grants(tmp_path / "ws")
    codex = Path(os.path.realpath(Path.home())) / ".codex"
    assert codex in grants["read_write"], "writable so codex can create its own state"
    for name in (
        "config.toml",
        "hooks.json",
        "AGENTS.md",
        "rules",
        "plugins",
        "agents",
    ):
        assert codex / name in grants["write_denied"]


def test_linux_wrap_grants_the_backend_binarys_directory(linux, tmp_path, monkeypatch):
    """$HOME is an opaque tmpfs, so an npm-global or ~/bin backend vanishes and the
    spawn dies with "No such file or directory: codex" before any policy runs."""
    fake = tmp_path / "opt" / "bin" / "codex"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        shutil, "which", lambda name: str(fake) if name == "codex" else "/usr/bin/bwrap"
    )
    out = sandbox.wrap(["codex", "exec"], tmp_path / "ws")
    assert str(fake.parent) in out
    assert out[0] == "bwrap"
    assert out[-2:] == ["codex", "exec"]


def test_linux_wrap_materialises_project_write_denies(linux, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sandbox.wrap(["codex"], workspace)
    assert (workspace / ".mcp.json").read_text() == "{}\n"
    assert (workspace / ".vscode").is_dir()


async def test_session_relay_is_inert_off_linux(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    relay = await sandbox.open_session_relay(
        {"HTTPS_PROXY": "http://127.0.0.1:1/"}, "chat"
    )
    await relay.close()  # always safe, whatever the platform


async def test_session_relay_warns_when_nothing_is_brokered(linux, caplog):
    """A jail the agent cannot reach any host service from is working, not broken
    -- but it is never what a deployment wants, so it must not pass silently."""
    with caplog.at_level("WARNING", logger="claude_on_the_fly.sandbox"):
        relay = await sandbox.open_session_relay({}, "chat")
    await relay.close()
    assert "no brokered loopback port" in "\n".join(
        r.getMessage() for r in caplog.records
    )


async def test_session_relay_bridges_the_ports_from_the_overrides(linux, monkeypatch):
    """Ports come from the overrides about to be published, not os.environ: a
    per-session egress proxy lives only in the ContextVar."""
    # sun_path caps out near 104 bytes and the fake home used by these tests is
    # already most of that, so point the relay at a short directory.
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        monkeypatch.setattr(agent, "DATA_DIR", Path(short))
        relay = await sandbox.open_session_relay(
            {"HTTPS_PROXY": "http://127.0.0.1:19099/"}, "chat"
        )
        try:
            assert sandbox._SESSION_SOCKETS.get() == {
                19099: relay._relay.sockets[19099]
            }
        finally:
            await relay.close()
        assert sandbox._SESSION_SOCKETS.get() is None


async def test_preflight_is_a_noop_when_not_jailed(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX", "env")
    await sandbox.preflight()


async def test_preflight_refuses_when_the_jail_cannot_run(linux, monkeypatch):
    async def broken(argv, workspace, timeout=20):
        return 1, "bwrap: No permissions to creating new namespace"

    monkeypatch.setattr(sandbox, "_run_jailed", broken)
    with pytest.raises(sandbox.SandboxBoundaryError, match="trivial command"):
        await sandbox.preflight()


async def test_preflight_refuses_when_egress_is_open(linux, monkeypatch):
    """The load-bearing claim of the design is that the egress proxy cannot be
    bypassed. Until this existed, nothing checked it on either platform."""

    async def leaky(argv, workspace, timeout=20):
        return (0, "cotf") if "echo" in argv[0] else (0, "REACHED")

    monkeypatch.setattr(sandbox, "_run_jailed", leaky)
    with pytest.raises(sandbox.SandboxBoundaryError, match="reached the internet"):
        await sandbox.preflight()


async def test_preflight_refuses_an_inconclusive_egress_probe(linux, monkeypatch):
    async def mute(argv, workspace, timeout=20):
        return (0, "cotf") if "echo" in argv[0] else (1, "ModuleNotFoundError")

    monkeypatch.setattr(sandbox, "_run_jailed", mute)
    with pytest.raises(sandbox.SandboxBoundaryError, match="inconclusive"):
        await sandbox.preflight()


async def test_preflight_passes_when_the_jail_holds(linux, monkeypatch, caplog):
    async def healthy(argv, workspace, timeout=20):
        return (
            (0, "cotf") if "echo" in argv[0] else (1, "BLOCKED:Network is unreachable")
        )

    monkeypatch.setattr(sandbox, "_run_jailed", healthy)
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"):
        await sandbox.preflight()
    assert "preflight ok" in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("boom", [OSError("no exec"), TimeoutError()])
async def test_preflight_reports_a_probe_that_could_not_run(linux, monkeypatch, boom):
    async def raiser(argv, workspace, timeout=20):
        raise boom

    monkeypatch.setattr(sandbox, "_run_jailed", raiser)
    with pytest.raises(sandbox.SandboxBoundaryError, match="could not run"):
        await sandbox.preflight()


async def test_egress_preflight_failure_is_distinguished(linux, monkeypatch):
    async def half(argv, workspace, timeout=20):
        if "echo" in argv[0]:
            return 0, "cotf"
        raise TimeoutError()

    monkeypatch.setattr(sandbox, "_run_jailed", half)
    with pytest.raises(
        sandbox.SandboxBoundaryError, match="egress preflight could not run"
    ):
        await sandbox.preflight()


async def test_an_absent_path_is_absent_without_spawning_anything(
    monkeypatch, tmp_path
):
    """bubblewrap hides a path by not mounting it, so a denied read reports "No
    such file or directory" -- identical to a missing file. Settling that outside
    the jail is what keeps DENIED and ABSENT distinguishable."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")

    async def must_not_spawn(*_a, **_k):
        raise AssertionError("probed a path that is not on this machine")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", must_not_spawn)
    assert await sandbox._probe_deny(str(tmp_path / "nope"), tmp_path) == sandbox.ABSENT


def test_probe_workspace_is_scratch_not_the_daemons_cwd():
    """The Linux jail materialises .mcp.json and .vscode/ in whatever workspace it
    is handed, so probing in cwd would leave those in somebody's checkout."""
    assert sandbox._probe_workspace().is_dir()
    assert agent.DATA_DIR in sandbox._probe_workspace().parents


async def test_preflight_names_the_userns_remedy_when_that_is_the_cause(
    linux, monkeypatch
):
    """The failure most operators on a current Ubuntu will hit, and the one whose
    message points somewhere else: bubblewrap creates the namespace, is refused
    netlink inside it, and reports RTM_NEWADDR. Nothing in that names the sysctl."""

    async def apparmor_blocked(argv, workspace, timeout=20):
        return 1, "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"

    monkeypatch.setattr(sandbox, "_run_jailed", apparmor_blocked)
    with pytest.raises(
        sandbox.SandboxBoundaryError, match="apparmor_restrict_unprivileged_userns"
    ):
        await sandbox.preflight()


async def test_preflight_does_not_guess_a_remedy_for_other_failures(linux, monkeypatch):
    async def other(argv, workspace, timeout=20):
        return 1, "bwrap: execvp /bin/echo: No such file or directory"

    monkeypatch.setattr(sandbox, "_run_jailed", other)
    with pytest.raises(sandbox.SandboxBoundaryError) as caught:
        await sandbox.preflight()
    assert "apparmor" not in str(caught.value)


def test_guidance_read_scope_ignores_sandbox_fs_on_linux(monkeypatch, tmp_path):
    """`sandbox.fs` cannot take effect on Linux, so reading it produced a prompt
    that contradicted itself: the agent was told it could read most of the
    filesystem while $HOME was an opaque tmpfs, and separately told not to read
    "No such file" as proof a path is absent."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    text = sandbox.agent_guidance(tmp_path)
    assert "You can read only these paths" in text
    assert "read most of the filesystem" not in text
    # Derived from the real grants, not a restated list that can drift.
    assert str(tmp_path.resolve()) in text


def test_guidance_network_line_is_accurate_on_linux(monkeypatch, tmp_path):
    """The macOS broker-only wording claims external hosts are blocked. On Linux
    they are reachable through the proxy, and telling the agent otherwise would
    make it decline work it can do."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    text = sandbox.agent_guidance(tmp_path)
    assert "no other port on the host is reachable" in text
    assert "external hosts are blocked" not in text


def test_inert_linux_settings_are_announced(monkeypatch, caplog):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_FS", "deny-most")
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"):
        sandbox._log_inert_settings()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "sandbox.fs has no effect on Linux" in logged
    assert "sandbox.broker_only_loopback has no effect on Linux" in logged


def test_nothing_is_announced_as_inert_on_macos(monkeypatch, caplog):
    monkeypatch.setenv("COTF_SANDBOX_FS", "deny-most")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"):
        sandbox._log_inert_settings()
    assert "no effect" not in "\n".join(r.getMessage() for r in caplog.records)
