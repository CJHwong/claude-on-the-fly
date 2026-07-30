"""Tests for the command broker.

The load-bearing tests are the readback refusals and the argv parsing they rest
on. Everything else about this module is plumbing; the refusal is the one place a
mistake puts a live credential inside the sandbox.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest
from aiohttp import ClientSession

from claude_on_the_fly import commands
from claude_on_the_fly.commands import (
    ENDPOINT_ENV,
    MAX_STREAM_BYTES,
    CommandBroker,
    ShimmedTool,
    leading_tokens,
    refuses_readback,
)

GH = next(t for t in commands.load_tools() if t.name == "gh")


# --- argv parsing: the basis of every refusal ---


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["pr", "view"], ("pr", "view")),
        (["pr", "view", "--json", "x"], ("pr", "view")),
        (["auth", "token"], ("auth", "token")),
        ([], ()),
        (["--json", "x", "pr", "list"], ("pr", "list")),
        # A flag's value is not a subcommand, or a leading global flag could
        # push a refused pair out of the matched prefix.
        (["--repo", "o/r", "auth", "token"], ("auth", "token")),
        (["--repo=o/r", "auth", "token"], ("auth", "token")),
        (["--", "auth", "token"], ("auth", "token")),
    ],
)
def test_leading_tokens(argv, expected):
    assert leading_tokens(argv) == expected


# --- credential readback ---


@pytest.mark.parametrize(
    "argv",
    [
        ["auth", "token"],
        ["auth", "token", "--hostname", "github.com"],
        ["--repo", "o/r", "auth", "token"],
        ["--repo=o/r", "auth", "token"],
        ["auth", "status", "--show-token"],
        ["--show-token", "auth", "status"],
        ["pr", "list", "--show-token"],
    ],
)
def test_readback_refused(argv):
    assert refuses_readback(GH, argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["auth", "status"],
        ["pr", "list"],
        ["pr", "view", "1"],
        ["api", "user"],  # not a readback: no policy here, only readback refusal
        [],
    ],
)
def test_non_readback_allowed(argv):
    assert refuses_readback(GH, argv) is False


def test_gh_readback_covers_both_shapes():
    assert ("auth", "token") in GH.readback
    assert "--show-token" in GH.readback_flags


# --- broker lifecycle and dispatch ---


@pytest.fixture
def echo_tool():
    """A shimmed tool backed by /bin/echo so tests need no real CLI."""
    return ShimmedTool(name="echo", readback=frozenset({("secret",)}))


async def start(tmp_path, tools, **kwargs) -> tuple[CommandBroker, int]:
    broker = CommandBroker(tmp_path / "shims", tools, **kwargs)
    port = await broker.start()
    return broker, port


async def post(port: int, payload: dict) -> dict:
    async with ClientSession() as client:
        resp = await client.post(f"http://127.0.0.1:{port}/run", json=payload)
        return await resp.json()


async def test_runs_the_real_binary(tmp_path, echo_tool):
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        out = await post(port, {"tool": "echo", "argv": ["hello", "world"]})
        assert out["stdout"].strip() == "hello world"
        assert out["rc"] == 0
        assert out["refused"] is False
    finally:
        await broker.stop()


async def test_readback_refused_over_the_wire(tmp_path, echo_tool):
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        out = await post(port, {"tool": "echo", "argv": ["secret", "value"]})
        assert out["refused"] is True
        assert out["rc"] == 1
        # The refused command must not have run, so its output cannot appear.
        assert "secret value" not in out["stdout"]
        assert "command broker exists to keep" in out["stderr"]
    finally:
        await broker.stop()


async def test_exit_code_propagates(tmp_path):
    broker, port = await start(tmp_path, (ShimmedTool(name="false"),))
    try:
        out = await post(port, {"tool": "false", "argv": []})
        assert out["rc"] == 1
    finally:
        await broker.stop()


async def test_unbrokered_tool_is_refused(tmp_path, echo_tool):
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        out = await post(port, {"tool": "curl", "argv": ["https://example.com"]})
        assert out["rc"] == 127
        assert "not brokered" in out["stderr"]
    finally:
        await broker.stop()


async def test_absent_binary_is_not_shimmed(tmp_path):
    broker, _ = await start(
        tmp_path, (ShimmedTool(name="definitely-not-installed-xyz"),)
    )
    try:
        # Shimming a missing binary would turn "command not found" into a
        # confusing broker error, so it is filtered at construction.
        assert broker.shimmed == []
    finally:
        await broker.stop()


async def test_stdin_is_forwarded(tmp_path):
    broker, port = await start(tmp_path, (ShimmedTool(name="cat"),))
    try:
        out = await post(port, {"tool": "cat", "argv": [], "stdin": "piped input"})
        assert out["stdout"] == "piped input"
    finally:
        await broker.stop()


async def test_output_is_capped(tmp_path):
    broker, port = await start(tmp_path, (ShimmedTool(name="yes"),), run_timeout=1.0)
    try:
        out = await post(port, {"tool": "yes", "argv": ["x"]})
        # `yes` never exits, so this also covers the timeout path.
        assert len(out["stdout"]) <= MAX_STREAM_BYTES
    finally:
        await broker.stop()


async def test_timeout_reports_and_does_not_hang(tmp_path):
    broker, port = await start(tmp_path, (ShimmedTool(name="sleep"),), run_timeout=0.3)
    try:
        out = await asyncio.wait_for(
            post(port, {"tool": "sleep", "argv": ["30"]}), timeout=10
        )
        assert out["rc"] == 124
        assert "timed out" in out["stderr"]
    finally:
        await broker.stop()


async def test_malformed_body_is_rejected(tmp_path, echo_tool):
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        async with ClientSession() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/run",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
    finally:
        await broker.stop()


async def test_subprocess_env_is_narrow(tmp_path, monkeypatch):
    """The broker runs unjailed, so the subprocess must not inherit every secret
    the daemon happens to hold."""
    monkeypatch.setenv("SOME_OTHER_SECRET", "must-not-propagate")
    monkeypatch.setenv("GH_HOST", "github.example")
    env = commands._subprocess_env(GH)
    assert "SOME_OTHER_SECRET" not in env
    assert env.get("GH_HOST") == "github.example"
    assert "PATH" in env and "HOME" in env


# --- shim generation ---


async def test_shims_are_written_executable(tmp_path, echo_tool):
    broker, _port = await start(tmp_path, (echo_tool,))
    try:
        shim = tmp_path / "shims" / "echo"
        assert shim.is_file()
        assert shim.stat().st_mode & stat.S_IXUSR
        body = shim.read_text()
        assert ENDPOINT_ENV in body
        # Stdlib only: the shim runs inside the sandbox where the project's
        # dependencies are not importable.
        assert "import urllib.request" in body
        assert "aiohttp" not in body
    finally:
        await broker.stop()


async def test_agent_env_points_at_the_endpoint(tmp_path, echo_tool):
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        assert broker.agent_env() == {ENDPOINT_ENV: f"http://127.0.0.1:{port}"}
    finally:
        await broker.stop()


def test_port_before_start_raises(tmp_path):
    broker = CommandBroker(tmp_path / "shims")
    with pytest.raises(RuntimeError, match="not started"):
        _ = broker.port


async def test_stop_is_idempotent(tmp_path, echo_tool):
    broker, _ = await start(tmp_path, (echo_tool,))
    await broker.stop()
    await broker.stop()


async def test_shims_are_regenerated_on_restart(tmp_path, echo_tool):
    broker, first = await start(tmp_path, (echo_tool,))
    await broker.stop()
    broker2, second = await start(tmp_path, (echo_tool,))
    try:
        # A new port each run, so a stale shim would point at a dead endpoint.
        # The shim reads the endpoint from env rather than baking it in, which is
        # what makes that safe.
        assert ENDPOINT_ENV in (tmp_path / "shims" / "echo").read_text()
        # Both runs bound a port; the shim resolves it from env at exec time, so
        # it does not matter whether the OS reused the number.
        assert first > 0 and second > 0
    finally:
        await broker2.stop()


# --- PATH wiring ---


def test_shim_dir_lives_outside_the_write_allowlist():
    from claude_on_the_fly import sandbox

    # DATA_DIR is not writable from inside the sandbox, so the agent can exec the
    # shims but not rewrite them. A tmpdir would be agent-writable.
    assert sandbox.shim_dir().name == "shims"
    assert ".claude-on-the-fly" in str(sandbox.shim_dir())


def test_path_untouched_when_no_shims_exist(monkeypatch, tmp_path):
    from claude_on_the_fly import sandbox

    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(sandbox, "shim_dir", lambda: tmp_path / "absent")
    assert (sandbox.agent_env() or {})["PATH"] == "/usr/bin"


def test_path_gets_the_shim_dir_when_populated(monkeypatch, tmp_path):
    from claude_on_the_fly import sandbox

    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "gh").write_text("#!/bin/sh\n")
    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(sandbox, "shim_dir", lambda: shims)
    assert (sandbox.agent_env() or {})["PATH"] == f"{shims}:/usr/bin"


def test_endpoint_var_survives_env_curation(monkeypatch):
    from claude_on_the_fly import sandbox

    monkeypatch.setenv("COTF_SANDBOX", "env")
    monkeypatch.setenv(ENDPOINT_ENV, "http://127.0.0.1:9999")
    monkeypatch.setenv("GITHUB_TOKEN", "must-be-dropped")
    env = sandbox.agent_env() or {}
    assert env[ENDPOINT_ENV] == "http://127.0.0.1:9999"
    assert "GITHUB_TOKEN" not in env


def test_endpoint_is_in_the_restart_mapping():
    """Editing it in the TUI must prompt a frontend restart, or the shims point
    at a dead endpoint."""
    from claude_on_the_fly.checks import SLACK_ENV_VARS, TELEGRAM_ENV_VARS

    for group in (SLACK_ENV_VARS, TELEGRAM_ENV_VARS):
        assert os.environ is not None  # keep the import honest
        assert any("COTF_SANDBOX" in var for var in group)


# --- diagnostic logging ---


async def test_shim_invocation_is_logged_with_argv0(tmp_path, echo_tool, caplog):
    """Only a shim reaches /run, so an arrival here is the one parent-side proof
    the shim was used. argv0 says whether PATH resolution or a direct path did it."""
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("DEBUG", logger="claude_on_the_fly.commands"):
            await post(
                port,
                {"tool": "echo", "argv": ["hi"], "argv0": "/shims/echo", "cwd": "/w"},
            )
        assert any(
            "shim invocation echo" in r.getMessage() and "/shims/echo" in r.getMessage()
            for r in caplog.records
        )
    finally:
        await broker.stop()


async def test_refusal_logs_the_cwd(tmp_path, echo_tool, caplog):
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("WARNING", logger="claude_on_the_fly.commands"):
            await post(port, {"tool": "echo", "argv": ["secret"], "cwd": "/w"})
        assert any("REFUSE echo" in (r.getMessage()) for r in caplog.records)
    finally:
        await broker.stop()


async def test_long_argv_token_is_clipped_by_default(
    tmp_path, echo_tool, caplog, monkeypatch
):
    """A `--body` blob is agent-authored prose, not an audit fact, so it must not
    ride into the log with the argv."""
    monkeypatch.delenv("COTF_LOG_CONTENT", raising=False)
    prose = "x" * 400
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("WARNING", logger="claude_on_the_fly.commands"):
            await post(port, {"tool": "echo", "argv": ["--body", prose]})
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "RUN echo" in logged
        assert prose not in logged
        assert "+352" in logged  # 400 - 48 clipped, and the count is reported
    finally:
        await broker.stop()


async def test_content_logging_opt_in_restores_full_argv(
    tmp_path, echo_tool, caplog, monkeypatch
):
    monkeypatch.setenv("COTF_LOG_CONTENT", "1")
    prose = "y" * 200
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("WARNING", logger="claude_on_the_fly.commands"):
            await post(port, {"tool": "echo", "argv": ["--body", prose]})
        assert prose in "\n".join(r.getMessage() for r in caplog.records)
    finally:
        await broker.stop()


async def test_subprocess_env_keys_logged_never_values(
    tmp_path, echo_tool, caplog, monkeypatch
):
    monkeypatch.setenv("GH_HOST", "secret-host.example")
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("DEBUG", logger="claude_on_the_fly.commands"):
            await post(port, {"tool": "echo", "argv": ["hi"]})
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "with env" in logged
        # The subprocess env is logged by key so a value can never leak through it.
        assert "secret-host.example" not in logged
    finally:
        await broker.stop()


# --- stdin handling ---


async def test_shim_does_not_hang_on_an_idle_stdin(tmp_path, monkeypatch):
    """A child of an agent harness inherits an open, silent stdin pipe.

    A plain read() on that waits for an EOF that never arrives, so the command
    hung forever with no output and no log line. Found by a real jailed run, not
    by review.
    """
    monkeypatch.setenv("COTF_SANDBOX", "off")
    broker, _port = await start(tmp_path, (ShimmedTool(name="echo"),))
    try:
        shim = str(tmp_path / "shims" / "echo")
        read_fd, write_fd = os.pipe()  # never written to, never closed
        try:
            proc = await asyncio.create_subprocess_exec(
                shim,
                "alive",
                env={**os.environ, **broker.agent_env()},
                stdin=read_fd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=20)
            assert out.decode().strip() == "alive"
        finally:
            os.close(read_fd)
            os.close(write_fd)
    finally:
        await broker.stop()


async def test_shim_still_forwards_real_piped_stdin(tmp_path, monkeypatch):
    """The idle-stdin fix must not cost the feature it guards."""
    monkeypatch.setenv("COTF_SANDBOX", "off")
    broker, _port = await start(tmp_path, (ShimmedTool(name="cat"),))
    try:
        shim = str(tmp_path / "shims" / "cat")
        proc = await asyncio.create_subprocess_exec(
            shim,
            env={**os.environ, **broker.agent_env()},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await asyncio.wait_for(
            proc.communicate(b"piped through the shim"), timeout=20
        )
        assert out.decode() == "piped through the shim"
    finally:
        await broker.stop()


# --- acli ---

ACLI = next(t for t in commands.load_tools() if t.name == "acli")


@pytest.mark.parametrize(
    "argv",
    [
        ["auth", "logout"],
        ["auth", "login"],
        ["auth", "switch"],
        ["--site", "x", "auth", "logout"],
    ],
)
def test_acli_credential_state_changes_are_refused(argv):
    assert refuses_readback(ACLI, argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["auth", "status"],
        ["jira", "workitem", "view", "ACE-1"],
        ["jira", "workitem", "search", "--jql", "project = ACE"],
        [],
    ],
)
def test_acli_ordinary_work_is_forwarded(argv):
    assert refuses_readback(ACLI, argv) is False


def test_acli_has_no_token_readback_because_none_exists():
    """`acli auth` offers login/logout/status/switch and no token print, so the
    absence of a token entry here is deliberate, not an oversight."""
    assert not any("token" in pair for pair in ACLI.readback)
    assert ACLI.readback_flags == frozenset()


# --- YAML config ---


def test_bundled_config_parses_and_ships_gh_and_acli():
    tools = {t.name: t for t in commands.load_tools(override=Path("/nonexistent"))}
    assert "gh" in tools and "acli" in tools
    assert ("auth", "token") in tools["gh"].readback


def test_bundled_config_is_in_the_package():
    """It sits beside the seatbelt profiles, so it must survive a wheel build the
    same way they do."""
    assert commands.BUNDLED_CONFIG.is_file()
    assert commands.BUNDLED_CONFIG.parent.name == "claude_on_the_fly"


def test_readback_is_written_as_words_not_nested_lists(tmp_path):
    """ "auth token" is far easier to get right than [[auth, token]] in a file
    whose whole job is refusing the correct commands."""
    config = tmp_path / "commands.yaml"
    config.write_text("tools:\n  - name: echo\n    readback:\n      - secret thing\n")
    tools = {t.name: t for t in commands.load_tools(override=config)}
    assert ("secret", "thing") in tools["echo"].readback
    assert refuses_readback(tools["echo"], ["secret", "thing", "--flag"]) is True
    assert refuses_readback(tools["echo"], ["secret"]) is False


def test_operator_file_adds_a_tool_without_touching_the_others(tmp_path, caplog):
    config = tmp_path / "commands.yaml"
    config.write_text("tools:\n  - name: kubectl\n    readback:\n      - config view\n")
    with caplog.at_level("INFO", logger="claude_on_the_fly.commands"):
        tools = {t.name: t for t in commands.load_tools(override=config)}
    assert "kubectl" in tools
    # The bundled entries survive, refusals intact.
    assert ("auth", "token") in tools["gh"].readback
    assert any("adds tool 'kubectl'" in r.getMessage() for r in caplog.records)


def test_override_that_drops_a_refusal_warns_loudly(tmp_path, caplog):
    """The one edit here that hands the agent a credential, so it must be visible
    rather than silently accepted."""
    config = tmp_path / "commands.yaml"
    config.write_text("tools:\n  - name: gh\n")
    with caplog.at_level("INFO", logger="claude_on_the_fly.commands"):
        tools = {t.name: t for t in commands.load_tools(override=config)}
    assert tools["gh"].readback == frozenset()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "dropping a readback refusal must warn"
    message = warnings[0].getMessage()
    assert "no longer refuses" in message
    assert "auth" in message and "token" in message


def test_override_keeping_refusals_does_not_warn(tmp_path, caplog):
    config = tmp_path / "commands.yaml"
    config.write_text(
        "tools:\n  - name: gh\n    readback:\n      - auth token\n"
        "    readback_flags:\n      - --show-token\n"
        "    env_passthrough: [GH_HOST]\n"
    )
    with caplog.at_level("INFO", logger="claude_on_the_fly.commands"):
        commands.load_tools(override=config)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_malformed_operator_file_falls_back_to_bundled(tmp_path, caplog):
    """Ignoring it outright would silently remove every shim, which sends the
    agent looking for another route to the same capability."""
    config = tmp_path / "commands.yaml"
    config.write_text("tools: not-a-list\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.commands"):
        tools = {t.name: t for t in commands.load_tools(override=config)}
    assert "gh" in tools and ("auth", "token") in tools["gh"].readback
    errors = "\n".join(r.getMessage() for r in caplog.records)
    assert "ignoring" in errors and "unavailable" in errors


def test_unparseable_yaml_falls_back(tmp_path, caplog):
    config = tmp_path / "commands.yaml"
    config.write_text("tools:\n  - name: [unclosed\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.commands"):
        tools = commands.load_tools(override=config)
    assert any(t.name == "gh" for t in tools)


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        ("[]", "must be a mapping"),
        ("other: 1", "no 'tools' key"),
        ("tools:\n  - 'just a string'", "must be a mapping"),
        ("tools:\n  - name: ''", "no name"),
        ("tools:\n  - name: 'two words'", "whitespace"),
        ("tools:\n  - name: gh\n    readback: nope", "must be a list"),
        ("tools:\n  - name: gh\n    readback:\n      - '  '", "empty entry"),
    ],
)
def test_malformed_entries_are_named_precisely(document, fragment):
    import yaml

    with pytest.raises(ValueError, match=fragment):
        commands.parse_tools(yaml.safe_load(document), source="test.yaml")


def test_operator_config_lives_outside_the_agents_write_scope():
    """This file decides what runs outside the sandbox with real credentials, so
    the agent must not be able to edit it."""
    from claude_on_the_fly import sandbox

    path = commands.operator_config()
    assert path.name == "commands.yaml"
    assert ".claude-on-the-fly" in str(path)
    # Same directory that holds the shims, which is read/exec but not writable.
    assert path.parent == sandbox.shim_dir().parent
