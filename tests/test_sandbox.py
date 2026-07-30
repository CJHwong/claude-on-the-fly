"""sandbox: env curation and seatbelt wrapping, gated by COTF_SANDBOX."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_on_the_fly import agent, sandbox


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
    assert sandbox._loopback_specs() == ("localhost:*",) * 3


def test_loopback_narrows_to_broker_port(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    _clear_loopback_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:54321/anthropic")
    # Only the broker port is known, so spare slots repeat it rather than
    # widening back to every loopback port.
    assert sandbox._loopback_specs() == ("localhost:54321",) * 3


def test_loopback_narrows_to_all_three_services(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    _clear_loopback_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:54321/anthropic")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:54322")
    monkeypatch.setenv("COTF_CMD_ENDPOINT", "http://127.0.0.1:54323")
    assert sandbox._loopback_specs() == (
        "localhost:54321",
        "localhost:54322",
        "localhost:54323",
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
        assert sandbox._loopback_specs() == (
            "localhost:5001",
            "localhost:6002",
            "localhost:7003",
        )
    finally:
        sandbox.reset_session_env(token)


def test_loopback_narrows_to_egress_port_alone(monkeypatch):
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    _clear_loopback_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:54322")
    # A deployment with no credentialed provider still gets a narrowed jail.
    assert sandbox._loopback_specs() == ("localhost:54322",) * 3


def test_loopback_warns_rather_than_silently_dropping_a_service(monkeypatch, caplog):
    """Guard for a future fourth loopback service. Unreachable through env today
    (the broker serves every route on one port, so the three sources fill the
    three slots exactly), so drive _loopback_ports directly."""
    monkeypatch.setenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", "1")
    monkeypatch.setattr(sandbox, "_loopback_ports", lambda: ["1", "2", "3", "4"])
    with caplog.at_level("WARNING"):
        specs = sandbox._loopback_specs()
    # Four services, three slots: the drop must be loud, never silent.
    assert specs == ("localhost:1", "localhost:2", "localhost:3")
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
    assert sandbox._loopback_specs() == ("localhost:*",) * 3


def test_wrap_jail_passes_three_loopback_slots(monkeypatch, tmp_path):
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_BROKER_ONLY_LOOPBACK", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    out = sandbox.wrap(["claude"], tmp_path)
    for param in ("_LOOPBACK=", "_LOOPBACK_ALT=", "_LOOPBACK_ALT2="):
        assert any(a.startswith(param) for a in out), param


def test_preapproved_hosts_parses_and_normalizes(monkeypatch):
    monkeypatch.setenv("COTF_EGRESS_ALLOW", "GitHub.com, pypi.org ,,")
    assert sandbox.preapproved_hosts() == frozenset({"github.com", "pypi.org"})


def test_preapproved_hosts_empty_by_default(monkeypatch):
    monkeypatch.delenv("COTF_EGRESS_ALLOW", raising=False)
    assert sandbox.preapproved_hosts() == frozenset()


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


def test_guidance_warns_keychain_denial_is_not_an_eperm(monkeypatch, tmp_path):
    """Verified against a live run: a denied keychain read reports "item could
    not be found", not EPERM, so an agent taught EPERM-means-policy would read
    it as "the credential does not exist" and hunt for it elsewhere."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.delenv("COTF_SANDBOX_FS", raising=False)
    text = sandbox.agent_guidance(tmp_path)
    assert "could not be found" in text
    assert "does not exist" in text


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
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"):
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
    assert "0/6 probed" in logged


async def test_broken_profile_is_not_reported_as_absent(monkeypatch, tmp_path, caplog):
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
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"):
        results = await sandbox.verify_denials(tmp_path)
    assert set(results.values()) == {sandbox.BROKEN}, results
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
    monkeypatch.setenv("HOME", str(linked_home))
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    argv = sandbox.wrap(["/bin/echo", "hi"], tmp_path)
    home_param = next(arg for arg in argv if arg.startswith("_HOME="))
    assert home_param == f"_HOME={real_home}"
    assert str(linked_home) not in home_param


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
            "COTF_EGRESS_ALLOW",
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

    def test_no_preapproved_hosts(self):
        assert sandbox.preapproved_hosts() == frozenset()

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
    monkeypatch, tmp_path, caplog
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
    monkeypatch, tmp_path, caplog
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
    monkeypatch, tmp_path, caplog
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
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox"):
        results = await sandbox.verify_denials(tmp_path)
    assert set(results.values()) == {sandbox.READABLE}, results
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "PROBE FAIL" in logged
    assert "credential path(s) READABLE inside the jail" in logged
    # A leak must never be reported alongside a reassuring count.
    assert "confirmed denied" not in logged


async def test_probes_run_concurrently_not_one_after_another(monkeypatch, tmp_path):
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
        r'\(allow file-(?:read|write)\*.*?\.claude-on-the-fly([^"]*)"', text
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
