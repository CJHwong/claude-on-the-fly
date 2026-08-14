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

from claude_on_the_fly import agent, egress, sandbox, sandbox_macos


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
    # Scoped to the _EXTRA_ slots rather than the whole argv. Matching "/d" across
    # every parameter made this fail about one run in sixteen once the session
    # params arrived: _CODEX_HOME ends in a sha256 of the workspace, so a hash
    # beginning with "d" contains "/d" and has nothing to do with the 4th grant.
    assert "/d" not in " ".join(extras)  # the 4th grant is dropped


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
    # "/.codex/sessions" used to be here. Rollouts now go to the workspace's own
    # CODEX_HOME, so the shared tree is denied both ways on purpose: it held every
    # other thread's verbatim turns, and leaving it writable while denying the read
    # would mean codex could write a rollout it cannot then resume from.
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
    creeping over one of them is a codex that cannot run.

    The rollout probe moved to the workspace's own CODEX_HOME, which is where codex
    writes them now. Measured against codex-cli 0.147.0: a real `codex exec` turn
    with CODEX_HOME redirected wrote its rollout under that directory, so the
    redirect is the mechanism and not an assumption.
    """
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    from claude_on_the_fly import codex_state

    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(original_home))
    sessions = codex_state.ensure_home(tmp_path) / "sessions"
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


def test_linux_grants_name_which_write_denies_are_directories(tmp_path):
    """`.vscode` is a directory and `.bashrc` is a file, and neither has a suffix
    to say so. Every declared directory has to be in the deny list too, or the
    stand-in for an absent one is created as the wrong kind."""
    grants = sandbox._linux_grants(tmp_path / "ws")
    assert grants["write_denied_dirs"]
    assert set(grants["write_denied_dirs"]) <= set(grants["write_denied"])
    project = Path(os.path.realpath(tmp_path / "ws"))
    assert project / ".vscode" in grants["write_denied_dirs"]
    assert project / ".bashrc" not in grants["write_denied_dirs"]


def test_linux_grants_skip_the_git_denies_in_a_linked_worktree(tmp_path):
    """In a worktree or submodule `.git` is a file naming a gitdir elsewhere, so
    there is no `<project>/.git/hooks` to mount over -- and materialising one
    raises NotADirectoryError, which would take the whole turn with it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".git").write_text("gitdir: /elsewhere/.git/worktrees/ws\n")
    grants = sandbox._linux_grants(workspace)
    project = Path(os.path.realpath(workspace))
    assert project / ".git/hooks" not in grants["write_denied"]
    assert project / ".git/config" not in grants["write_denied"]
    assert project / ".git/hooks" not in grants["write_denied_dirs"]
    # The rest of the contract is untouched.
    assert project / ".mcp.json" in grants["write_denied"]


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
        if "echo" in argv[0]:
            return 0, "cotf"
        # The session probe asserts the file actually appeared rather than trusting
        # the exit code, so a stub has to do the write a real jail would have done.
        if "printf ok >" in argv[-1]:
            target = Path(argv[-1].rsplit("printf ok > ", 1)[1].strip())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("ok")
            return 0, ""
        return 1, "BLOCKED:Network is unreachable"

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


def test_runtime_read_paths_cover_the_binary_and_its_interpreter(monkeypatch, tmp_path):
    """The jail has to read the thing it is jailing. Both the agent binary and the
    interpreter routinely live under $HOME, which every least-privilege profile
    makes opaque, so omitting them does not tighten the jail -- it stops the
    backend starting. Measured before this existed: a backend under ~/.local/bin
    exited 126 and sandbox-exec refused the venv interpreter with rc 71, which
    made the startup egress probe block the daemon outright."""
    fake = tmp_path / "opt" / "bin" / "codex"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        shutil, "which", lambda name: str(fake) if name == "codex" else None
    )
    paths = sandbox._runtime_read_paths(["codex", "exec"])
    assert fake.parent in paths
    assert Path(sandbox.__file__).parent in paths
    assert len(paths) == len({str(p) for p in paths}), "duplicates waste fixed slots"


def test_runtime_read_paths_tolerate_an_unresolvable_binary(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert sandbox._runtime_read_paths([]) == sandbox._runtime_read_paths(["nope"])


def test_macos_deny_most_passes_the_runtime_slots(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_FS", "deny-most")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    out = sandbox.wrap(["claude", "-p"], tmp_path)
    slots = [a for a in out if a.startswith("_RUNTIME_")]
    assert len(slots) == sandbox_macos._RUNTIME_SLOTS, "every slot must be filled"


def test_allow_reads_does_not_pass_runtime_slots(monkeypatch, tmp_path):
    """fs-allow-reads.sb does not reference them; it allows reads globally."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(sandbox, "_platform", lambda: "darwin")
    assert not [
        a for a in sandbox.wrap(["claude"], tmp_path) if a.startswith("_RUNTIME_")
    ]


def test_jailing_without_a_relay_is_said_out_loud(monkeypatch, tmp_path, caplog):
    """The jobs daemon spawns without opening a relay, because it runs as its own
    process and builds no broker. The namespace then reaches nothing on the host.
    macOS is in the same position for the same reason, so this is not a Linux
    regression -- but there it merely fails, where here it would look like a hang."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    with caplog.at_level("WARNING", logger="claude_on_the_fly.sandbox"):
        sandbox.wrap(["codex", "exec"], tmp_path)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "no brokered loopback port" in logged
    assert "jobs daemon" in logged


# --- one thread's transcripts must not be another thread's to read ---


def _session_dirs(home: Path, workspace: Path) -> tuple[Path, Path]:
    """(this workspace's session dir, a second workspace's) under `home`."""
    from claude_on_the_fly import transcript

    other = workspace.parent / f"{workspace.name}-second-thread"
    other.mkdir(parents=True, exist_ok=True)
    return transcript.claude_session_dir(workspace), transcript.claude_session_dir(
        other
    )


@pytest.mark.parametrize("fs_base", ["", "deny-most"])
def test_the_running_threads_claude_session_dir_is_writable_under_the_jail(
    monkeypatch, tmp_path, original_home, fs_base
):
    """The claude CLI is the jailed process, and it writes its own session JSONL to
    `<config dir>/projects/<workspace hash>/`. The blanket deny on ~/.claude covered
    that path, so under `jail` the session never persisted and resume could not work.
    Turns still completed, which is why it stayed hidden.

    Against the real home on purpose: the suite's tmpdir HOME sits inside _TMPDIR,
    which both profiles grant wholesale, so a write there would pass for the wrong
    reason. Creates only its own session directory and removes it again.
    """
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_FS", fs_base)
    monkeypatch.setenv("HOME", str(original_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workspace = tmp_path / "thread-one"
    workspace.mkdir()
    own, _ = _session_dirs(original_home, workspace)
    if not own.parent.is_dir():
        pytest.skip("no real claude projects dir on this machine to probe")
    try:
        # `wrap` creates the chain on the host before the spawn, so the directory is
        # there by the time the CLI runs. It has to be: a recursive mkdir from inside
        # walks up into the opaque $HOME and fails at the home directory itself.
        _run_jailed(["/bin/true"], workspace)
        assert own.is_dir(), "wrap did not create this thread's session directory"
        target = own / "session.jsonl"
        wrote = _run_jailed(
            ["/bin/sh", "-c", f"echo '{{}}' > {target} && cat {target}"], workspace
        )
        assert wrote.returncode == 0, wrote.stderr
        assert wrote.stdout.strip() == "{}"
    finally:
        shutil.rmtree(own, ignore_errors=True)


@pytest.mark.parametrize("fs_base", ["", "deny-most"])
def test_another_threads_claude_session_file_is_unreadable_under_the_jail(
    monkeypatch, tmp_path, original_home, fs_base
):
    """Every workspace is one chat thread, so the session store held every other
    thread's verbatim turns and both profiles let a jailed turn read all of them.
    Worse in kind than the credentials each profile denies one by one: those are
    tokens, these are the message bodies themselves, other senders' included.

    Against the real home for the same reason as the test above, and this direction
    needs it more: a tmpdir home would make the read succeed via the _TMPDIR grant
    and the assertion would be measuring nothing.
    """
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("COTF_SANDBOX_FS", fs_base)
    monkeypatch.setenv("HOME", str(original_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workspace = tmp_path / "thread-one"
    workspace.mkdir()
    own, other = _session_dirs(original_home, workspace)
    if not own.parent.is_dir():
        pytest.skip("no real claude projects dir on this machine to probe")
    other.mkdir(parents=True, exist_ok=True)
    victim = other / "transcript.jsonl"
    victim.write_text("{}\n")
    try:
        done = _run_jailed(["/bin/cat", str(victim)], workspace)
        assert done.returncode != 0, "another thread's transcript was readable"
        assert "not permitted" in done.stderr.lower(), done.stderr
        # The store must not even be enumerable: the directory names are the
        # workspace paths, which leak who the daemon is talking to.
        listed = _run_jailed(["/bin/ls", str(own.parent)], workspace)
        assert listed.returncode != 0, "the session store was listable"
    finally:
        shutil.rmtree(other, ignore_errors=True)


@pytest.mark.parametrize("profile", [sandbox._BASE_PROFILE, sandbox._DENY_MOST_PROFILE])
def test_the_claude_session_grant_names_one_thread_not_the_whole_store(profile):
    """Structural counterpart to the two live probes.

    The grant has to be the parameterised per-run path. A rule written against
    `_HOME/.claude/projects` would look equivalent and would not be: CLAUDE_CONFIG_DIR
    can move the store outside $HOME, where the deny would match nothing while the
    profile still loaded and the log still said "jailed".
    """
    text = profile.read_text()
    assert '(deny file-read* (subpath (param "_CLAUDE_PROJECTS")))' in text
    assert '(allow file-read* (subpath (param "_CLAUDE_PROJECT")))' in text
    assert '(allow file-write* (subpath (param "_CLAUDE_PROJECT")))' in text
    # Nothing may re-grant the store itself, in either direction.
    assert '(subpath (string-append (param "_HOME") "/.claude/projects"))' not in text


def test_both_profiles_receive_the_session_grant_params(tmp_path):
    """Unlike _EXTRA_*, these are referenced by both bases, so both must be passed.
    A profile referencing an unpassed -D is refused by sandbox-exec outright, which
    would take the daemon down rather than silently drop the grant."""
    for base in (sandbox._BASE_PROFILE, sandbox._DENY_MOST_PROFILE):
        argv = sandbox_macos.jail_argv(
            ["/bin/echo"],
            home=tmp_path,
            data_dir=tmp_path,
            project=tmp_path,
            tmpdir=tmp_path,
            claude_config=tmp_path / "config",
            claude_projects=tmp_path / "projects",
            claude_project=tmp_path / "projects" / "thread",
            codex_sessions=tmp_path / "codex" / "sessions",
            codex_home=tmp_path / "codex-homes" / "thread",
            base=base,
            loopback=("localhost:*",) * 4,
            extra_paths=[],
        )
        assert f"_CLAUDE_PROJECTS={tmp_path / 'projects'}" in argv
        assert f"_CLAUDE_PROJECT={tmp_path / 'projects' / 'thread'}" in argv


def test_the_session_grant_follows_a_redirected_config_dir(monkeypatch, tmp_path):
    """CLAUDE_CONFIG_DIR moves the store, so the grant must move with it. Derived
    from transcript, which owns the hash scheme the CLI computes, so the path the
    jail grants and the path the daemon later reads cannot drift apart."""
    redirected = tmp_path / "elsewhere"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(redirected))
    workspace = tmp_path / "workspaces" / "slack" / "thread"
    workspace.mkdir(parents=True)
    _, projects, thread = sandbox._claude_session_paths(workspace)
    assert projects == Path(os.path.realpath(redirected / "projects"))
    assert thread.parent == projects
    assert thread.name == str(workspace.resolve()).replace("/", "-").replace(
        ".", "-"
    ).replace("_", "-")


def test_the_spawned_agent_is_told_which_config_dir_the_daemon_resolved(
    monkeypatch, tmp_path
):
    """CLAUDE_CONFIG_DIR is not a passthrough key, so a deployment setting it in
    DATA_DIR/.env had the daemon resolving one store and the spawned CLI defaulting
    to ~/.claude. The jail grant is derived from the daemon's answer, so a child
    using a different one would write where nothing is granted."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    env = sandbox.agent_env()
    assert env is not None
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "elsewhere")


def test_linux_grants_hide_other_threads_sessions_and_keep_this_ones(tmp_path):
    """The bubblewrap mirror of the seatbelt pair. projects/ is opaque rather than
    read-only, because the read-only ~/.claude mount would otherwise expose every
    other thread's turns. The running thread's directory is granted back deeper, the
    same depth-ordering trade the plugins/cache entry makes."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _, projects, thread = sandbox._claude_session_paths(workspace)
    # Masked only when present, so make it present rather than depending on
    # whatever another test in this session happened to create.
    projects.mkdir(parents=True, exist_ok=True)
    grants = sandbox._linux_grants(workspace)
    assert projects in grants["opaque"]
    assert thread in grants["read_write"]
    # The claude config stays readable (settings, plugins, the agent's own
    # credential), so the narrowing must come from the opaque child, not from
    # dropping the parent mount.
    assert Path(os.path.realpath(Path.home())) / ".claude" in grants["read_only"]


# --- codex rollouts are per thread too ---


@pytest.mark.parametrize("profile", [sandbox._BASE_PROFILE, sandbox._DENY_MOST_PROFILE])
def test_the_codex_rollout_deny_comes_after_the_codex_read_grant(profile):
    """SBPL is last-match-wins, so ordering is the policy here and not a style
    question. Measured with the pair placed before the `~/.codex` read allow: the
    allow won, and a jailed turn read 91855 bytes of another thread's rollout while
    the profile still loaded and the startup log still said "jailed".
    """
    lines = profile.read_text().splitlines()

    def index_of(needle: str) -> int:
        for number, line in enumerate(lines):
            if needle in line and not line.strip().startswith(";;"):
                return number
        return -1

    deny = index_of('(deny file-read* (subpath (param "_CODEX_SESSIONS")))')
    grant = index_of('(allow file-read* (subpath (param "_CODEX_HOME")))')
    assert deny >= 0 and grant >= 0, "the codex session rules are missing"
    assert grant > deny, "the per-thread grant must win over the tree deny"
    shared = index_of(
        '(allow file-read* (subpath (string-append (param "_HOME") "/.codex")))'
    )
    if shared >= 0:
        assert deny > shared, "the deny is overridden by the wider ~/.codex allow"


@pytest.mark.parametrize("profile", [sandbox._BASE_PROFILE, sandbox._DENY_MOST_PROFILE])
def test_the_shared_codex_rollout_tree_is_no_longer_writable(profile):
    """Rollouts go to the per-thread home now. Denying the shared tree both ways is
    deliberate: if the redirect ever failed to reach the child, codex would write
    here and then be refused the read back, and a resume that silently forgets the
    conversation is the failure this change exists to remove."""
    text = profile.read_text()
    assert '"/.codex/sessions"' not in text, "the shared rollout tree is writable again"
    assert '(allow file-write* (subpath (param "_CODEX_HOME")))' in text


def test_each_workspace_gets_its_own_codex_home(tmp_path):
    """The isolation boundary. codex names a rollout at startup from the date and
    thread id, so there is nothing per-workspace to grant inside one shared tree."""
    from claude_on_the_fly import codex_state

    first, second = tmp_path / "thread-one", tmp_path / "thread-two"
    first.mkdir()
    second.mkdir()
    assert codex_state.home_dir(first) != codex_state.home_dir(second)
    # Stable across calls, or a resume would look in a directory nothing wrote to.
    assert codex_state.home_dir(first) == codex_state.home_dir(first)


def test_the_codex_home_links_the_operators_config_and_credential(tmp_path):
    """codex must still read the operator's configuration and its own credential.
    Links rather than copies, so an operator's edit applies on the next turn, and a
    write through the link resolves onto a path the profile already governs."""
    from claude_on_the_fly import codex_state

    shared = tmp_path / "shared-codex"
    (shared / "prompts").mkdir(parents=True)
    (shared / "config.toml").write_text("model = 'x'\n")
    (shared / "auth.json").write_text("{}\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = codex_state.ensure_home(workspace, shared=shared)
    assert (home / "config.toml").resolve() == (shared / "config.toml").resolve()
    assert (home / "auth.json").resolve() == (shared / "auth.json").resolve()
    assert (home / "prompts").resolve() == (shared / "prompts").resolve()
    # Created here rather than left to codex: a recursive mkdir under an opaque
    # $HOME walks up and tries to create an ancestor it cannot stat.
    assert (home / "sessions").is_dir()
    # An entry the operator does not have must not become a dangling link.
    assert not (home / "hooks.json").exists()


def test_ensure_home_is_idempotent_and_relinks_a_moved_target(tmp_path):
    """It runs before every spawn, so a second call must not fail, and an operator
    who repoints a shared entry gets the new target without a restart."""
    from claude_on_the_fly import codex_state

    shared = tmp_path / "shared-codex"
    shared.mkdir()
    (shared / "config.toml").write_text("a\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = codex_state.ensure_home(workspace, shared=shared)
    again = codex_state.ensure_home(workspace, shared=shared)
    assert home == again
    moved = tmp_path / "other-codex"
    moved.mkdir()
    (moved / "config.toml").write_text("b\n")
    relinked = codex_state.ensure_home(workspace, shared=moved)
    assert (relinked / "config.toml").readlink() == moved / "config.toml"


def test_removing_a_workspace_takes_its_codex_home_without_following_links(tmp_path):
    """The home holds that thread's rollouts and its name encodes a path that will
    never exist again, so nothing else could reclaim it. The links are unlinked
    first, or the tree walk would delete the operator's shared config."""
    from claude_on_the_fly import codex_state

    shared = tmp_path / "shared-codex"
    shared.mkdir()
    (shared / "config.toml").write_text("keep me\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = codex_state.ensure_home(workspace, shared=shared)
    (home / "sessions" / "rollout-x.jsonl").write_text("{}\n")
    codex_state.remove_workspace(workspace)
    assert not home.exists()
    assert (shared / "config.toml").read_text() == "keep me\n"


def test_linux_grants_hide_other_threads_rollouts_and_keep_this_ones(tmp_path):
    """The bubblewrap mirror. The shared tree is masked with an empty tmpfs; the
    thread's own home is a read-write mount, and it has to exist because a mount
    source that is absent takes the whole turn with it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    codex_sessions, codex_home = sandbox._codex_session_paths(workspace)
    # The shared tree is masked only when it is there, because bwrap cannot create
    # a mount point under a read-only root. Make it exist, as a host that has run
    # codex would have.
    codex_sessions.mkdir(parents=True, exist_ok=True)
    grants = sandbox._linux_grants(workspace)
    assert codex_sessions in grants["opaque"]
    assert codex_home in grants["read_write"]


def test_an_absent_session_store_is_not_named_as_a_mount(tmp_path, monkeypatch):
    """bwrap creates its mount points inside a `--ro-bind / /` root, so naming a
    path that does not exist on the host fails the whole spawn with "Can't mkdir
    parents ... Read-only file system" rather than being skipped.

    Caught by CI on a fresh runner, not by a local probe: a developer machine has
    run claude and codex already, so every one of these paths happens to exist and
    the bug is invisible there. A tree that is absent holds no other thread's
    transcripts either, so masking it buys nothing.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "never-made"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _, claude_projects, _ = sandbox._claude_session_paths(workspace)
    codex_sessions, _ = sandbox._codex_session_paths(workspace)
    assert not claude_projects.exists() and not codex_sessions.exists()
    opaque = sandbox._linux_grants(workspace)["opaque"]
    assert claude_projects not in opaque
    assert codex_sessions not in opaque
    # And present ones are masked, or the whole boundary would be conditional.
    claude_projects.mkdir(parents=True)
    codex_sessions.mkdir(parents=True)
    opaque = sandbox._linux_grants(workspace)["opaque"]
    assert claude_projects in opaque and codex_sessions in opaque


def test_the_linux_wrap_creates_the_session_mounts_it_binds(tmp_path, monkeypatch):
    """The read-write half of the same constraint. These two cannot be skipped when
    absent the way an opaque tree can: the turn needs them writable, so they are
    created on the host first, exactly as the write-deny targets are."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    directories, files = sandbox._session_mount_sources(workspace)
    assert not any(path.exists() for path in (*directories, *files))
    for source in directories:
        source.mkdir(parents=True, exist_ok=True)
    for source in files:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.touch(exist_ok=True)
    # Every source is now bindable, and the opaque projects/ parent exists as a
    # side effect of creating the claude child.
    assert all(path.is_dir() for path in directories)
    # Files stay files. Creating a directory called policy-limits.json would leave
    # the CLI unable to write its own state, which is why the two lists are split.
    assert all(path.is_file() for path in files)
    assert sandbox._claude_session_paths(workspace)[1].is_dir()


# --- what a claude turn is allowed to write under its config dir ---

# Instruction-bearing entries: read on later invocations, so a write outlives the
# session. None may appear in the measured runtime-write list, and each is probed
# live below.
_CLAUDE_MUST_NOT_WRITE = (
    "settings.json",
    "settings.local.json",
    "CLAUDE.md",
    "hooks.json",
)

# The same class, but directories. Probed by creating a child rather than appending
# to a file: the nested parents do not exist on a real host, so an append reports
# ENOENT and cannot tell "denied" from "absent" -- the ambiguity `_probe_deny`
# exists to settle. mkdir reports EPERM either way.
_CLAUDE_MUST_NOT_CREATE_IN = (
    "commands",
    "skills",
    "agents",
    "plugins",
)


@pytest.mark.parametrize("profile", [sandbox._BASE_PROFILE, sandbox._DENY_MOST_PROFILE])
def test_the_claude_runtime_writes_are_granted_against_the_config_param(profile):
    """Every measured entry must be granted, or the agent silently loses that
    capability under the jail. Written against _CLAUDE_CONFIG rather than
    _HOME/.claude, because CLAUDE_CONFIG_DIR can move the tree outside $HOME where
    a _HOME-derived rule matches nothing while the profile still loads."""
    text = profile.read_text()
    for name in sandbox._CLAUDE_RUNTIME_WRITE_DIRS:
        rule = f'(allow file-write* (subpath (string-append (param "_CLAUDE_CONFIG") "/{name}")))'
        assert rule in text, f"{name} is not granted; the agent loses it under jail"
    for name in sandbox._CLAUDE_RUNTIME_WRITE_FILES:
        rule = f'(allow file-write* (literal (string-append (param "_CLAUDE_CONFIG") "/{name}")))'
        assert rule in text, f"{name} is not granted"
    assert '(deny file-write* (subpath (param "_CLAUDE_CONFIG")))' in text


def test_the_runtime_write_list_holds_nothing_instruction_bearing():
    """The line that makes the grants safe. Anything here is read on a later
    invocation, so a turn that could write it would leave itself standing orders,
    which is the whole reason the config directory is deny-default."""
    granted = set(sandbox._CLAUDE_RUNTIME_WRITES)
    for name in _CLAUDE_MUST_NOT_WRITE:
        top = name.split("/")[0]
        assert name not in granted and top not in granted, f"{name} became writable"
    # projects/ is conversation-bearing and is granted per thread, never wholesale.
    assert "projects" not in granted


@pytest.mark.parametrize("target", _CLAUDE_MUST_NOT_WRITE)
def test_claude_instruction_paths_stay_unwritable_in_the_jail(
    monkeypatch, tmp_path, original_home, target
):
    """Live counterpart, against the real home so the deny is why the write fails
    rather than the path being absent. Never creates anything under the real home:
    a successful write is the failure this asserts against."""
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(original_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    path = original_home / ".claude" / target
    done = _run_jailed(["/bin/sh", "-c", f"printf x >> {path}"], tmp_path)
    assert done.returncode != 0, f"{target} was writable inside the jail"
    assert "not permitted" in done.stderr.lower(), done.stderr


@pytest.mark.parametrize("target", _CLAUDE_MUST_NOT_CREATE_IN)
def test_claude_instruction_dirs_stay_unwritable_in_the_jail(
    monkeypatch, tmp_path, original_home, target
):
    """A turn must not be able to drop a new command, skill, agent or plugin
    manifest, each of which the next invocation would load."""
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(original_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # Aim at a path whose parent exists, or the error is ENOENT and says nothing
    # about the policy: create the directory itself when it is absent, a child
    # inside it when it is already there.
    entry = original_home / ".claude" / target
    probe = entry / "cotf-suite-probe" if entry.is_dir() else entry
    done = _run_jailed(["/bin/mkdir", str(probe)], tmp_path)
    assert done.returncode != 0, f"{target} accepted a new entry inside the jail"
    assert "not permitted" in done.stderr.lower(), done.stderr
    assert not probe.exists()


def test_the_prompt_history_of_other_threads_is_unreadable_in_the_jail(
    monkeypatch, tmp_path, original_home
):
    """history.jsonl is every prompt typed in every project on the host, so it
    crosses threads the same way projects/ does. Read-only probe against the real
    file, so the deny is the reason it fails."""
    if not shutil.which("sandbox-exec"):
        pytest.skip("macOS only")
    history = original_home / ".claude" / "history.jsonl"
    if not history.is_file():
        pytest.skip("no real history.jsonl on this machine to probe")
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("HOME", str(original_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    done = _run_jailed(["/bin/cat", str(history)], tmp_path)
    assert done.returncode != 0, "cross-project prompt history was readable"
    assert "not permitted" in done.stderr.lower(), done.stderr


def test_linux_grants_mirror_the_claude_runtime_writes_and_mask_history(tmp_path):
    """The bubblewrap half. ~/.claude is a read-only mount here, so each measured
    entry needs its own deeper read-write mount, and hiding one file inside it needs
    a mount over that file rather than a rule."""
    monkeypatch_free_config = tmp_path / "config"
    (monkeypatch_free_config / "shell-snapshots").mkdir(parents=True)
    history = monkeypatch_free_config / "history.jsonl"
    history.write_text("{}\n")
    os.environ["CLAUDE_CONFIG_DIR"] = str(monkeypatch_free_config)
    try:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        grants = sandbox._linux_grants(workspace)
        for name in sandbox._CLAUDE_RUNTIME_WRITES:
            assert monkeypatch_free_config / name in grants["read_write"], name
        assert history in grants["masked"]
    finally:
        del os.environ["CLAUDE_CONFIG_DIR"]


async def test_preflight_refuses_when_the_agent_cannot_persist_its_session(
    linux, monkeypatch
):
    """The failure this probe exists for. It is the only positive check in
    preflight, because a profile that denies the CLI its own session file breaks
    nothing a negative check can see: every turn still completes, and the loss only
    surfaces later as a resume with no memory of the conversation. That shipped
    once, and the other two probes were green throughout."""

    async def jail_denies_the_session_write(argv, workspace, timeout=20):
        if "echo" in argv[0]:
            return 0, "cotf"
        if "printf ok >" in argv[-1]:
            return 1, "Operation not permitted"
        return 1, "BLOCKED:Network is unreachable"

    monkeypatch.setattr(sandbox, "_run_jailed", jail_denies_the_session_write)
    with pytest.raises(sandbox.SandboxBoundaryError, match="silently lose"):
        await sandbox.preflight()


async def test_preflight_does_not_trust_the_exit_code_alone(linux, monkeypatch):
    """A zero exit with no file is the shape a mount that silently went missing
    has: the shell succeeds against a tmpfs the jail then discards. So the probe
    checks the host side, which is where the CLI's real session file has to land."""

    async def lies_about_success(argv, workspace, timeout=20):
        if "echo" in argv[0]:
            return 0, "cotf"
        if "printf ok >" in argv[-1]:
            return 0, ""
        return 1, "BLOCKED:Network is unreachable"

    monkeypatch.setattr(sandbox, "_run_jailed", lies_about_success)
    with pytest.raises(sandbox.SandboxBoundaryError, match="cannot write"):
        await sandbox.preflight()


def test_the_shared_codex_rollout_tree_is_always_masked_on_linux(tmp_path, monkeypatch):
    """The gap this closes: Linux binds ~/.codex read-write, so an unmasked shared
    tree could be created by the jailed turn itself and written into -- working but
    unisolated, where macOS refuses. Masking needs the path to exist, so it is
    created rather than skipped."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "never-run-codex"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    codex_sessions, _ = sandbox._codex_session_paths(workspace)
    assert not codex_sessions.exists()
    directories, _ = sandbox._session_mount_sources(workspace)
    assert codex_sessions in directories, "the shared tree is not created, so unmasked"
    for source in directories:
        source.mkdir(parents=True, exist_ok=True)
    assert codex_sessions in sandbox._linux_grants(workspace)["opaque"]


def test_the_mount_sources_exist_before_the_grants_are_computed(tmp_path, monkeypatch):
    """Ordering, and it is the policy rather than a detail.

    `_linux_grants` decides whether to mask a session store by whether it exists.
    Creating the sources after that call left the shared codex tree unmasked and
    then created it, and because Linux binds ~/.codex read-write a jailed turn could
    write a rollout straight into it. Measured on a fresh host: rc 0, the file
    landed, and every test in this file still passed.
    """
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "never-run-codex"))
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/bwrap")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    codex_sessions, _ = sandbox._codex_session_paths(workspace)
    assert not codex_sessions.exists(), "precondition: the shared tree is absent"
    captured: dict = {}

    def capture(argv, **kwargs):
        captured.update(kwargs)
        return ["bwrap", *argv]

    monkeypatch.setattr(sandbox.sandbox_linux, "jail_argv", capture)
    monkeypatch.setattr(
        sandbox.sandbox_linux, "prepare_placeholders", lambda _root: object()
    )
    monkeypatch.setattr(
        sandbox.sandbox_linux, "ensure_write_deny_targets", lambda *a, **k: None
    )
    sandbox.wrap(["/bin/echo"], workspace)
    assert codex_sessions in captured["opaque"], (
        "the shared rollout tree was not masked, so a jailed turn could write it"
    )


def test_a_mount_source_that_cannot_be_created_is_reported_not_swallowed(
    tmp_path, monkeypatch, caplog
):
    """A source that cannot be created is either a mount the turn then lacks or a
    mask that then does not apply, and the second is a boundary going missing. It
    must not raise either: the spawn still has to happen, and bwrap reports the
    real consequence better than a guess here would."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def refuse(self, *args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", refuse)
    with caplog.at_level("WARNING", logger="claude_on_the_fly.sandbox"):
        sandbox._ensure_session_mount_sources(workspace)
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "could not create mount source" in messages


def test_a_mount_source_file_that_cannot_be_created_is_reported(
    tmp_path, monkeypatch, caplog
):
    """Same for the file half of the list, which is created with touch."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def refuse(self, *args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "touch", refuse)
    with caplog.at_level("WARNING", logger="claude_on_the_fly.sandbox"):
        sandbox._ensure_session_mount_sources(workspace)
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "could not create mount source" in messages
    assert "policy-limits.json" in messages


async def test_the_session_preflight_reports_a_probe_that_could_not_run(
    linux, monkeypatch
):
    """A probe that never ran is not evidence of anything, so it must not read as a
    pass. Distinguished from the session write being denied, which is a different
    and much more specific failure."""

    async def healthy_until_the_session_probe(argv, workspace, timeout=20):
        if "echo" in argv[0]:
            return 0, "cotf"
        if "printf ok >" in argv[-1]:
            raise OSError("no exec")
        return 1, "BLOCKED:Network is unreachable"

    monkeypatch.setattr(sandbox, "_run_jailed", healthy_until_the_session_probe)
    with pytest.raises(sandbox.SandboxBoundaryError, match="session preflight could"):
        await sandbox.preflight()


def test_the_runtime_grants_cover_a_launcher_symlinked_elsewhere(tmp_path, monkeypatch):
    """`claude` installs as a symlink in ~/.local/bin pointing into
    ~/.local/share/claude/versions/<v>. Granting only the resolved parent left
    execvp unable to read the symlink it has to resolve first, and the jail failed
    with rc 71 "execvp() of 'claude' failed: No such file or directory" -- which
    reads like a missing binary rather than a denial, and is why the deny-most
    profile could not start the claude backend at all.

    The npm layout this was first measured against keeps both in one directory, so
    a single grant covered them by accident.
    """
    launcher_dir = tmp_path / "bin"
    target_dir = tmp_path / "share" / "versions"
    launcher_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    real = target_dir / "2.1.232"
    real.write_text("#!/bin/sh\n")
    launcher = launcher_dir / "agent-cli"
    launcher.symlink_to(real)
    monkeypatch.setattr(shutil, "which", lambda _name: str(launcher))
    granted = sandbox._runtime_read_paths(["agent-cli"])
    assert launcher_dir in granted, "the launcher's own directory is not granted"
    assert target_dir in granted, "the resolved binary's directory is not granted"


def test_the_runtime_slot_count_covers_every_path_the_wrapper_supplies(tmp_path):
    """A path past the slot cap is truncated, silently, and the backend then fails
    to exec. The cap and the list have to move together."""
    granted = sandbox._runtime_read_paths(["python3"])
    assert len(granted) <= sandbox_macos._RUNTIME_SLOTS, (
        f"{len(granted)} runtime paths but only {sandbox_macos._RUNTIME_SLOTS} slots; "
        "the extras are dropped and the backend cannot start"
    )
