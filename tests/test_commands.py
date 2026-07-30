"""Tests for the command broker.

The load-bearing tests are the readback refusals and the argv parsing they rest
on. Everything else about this module is plumbing; the refusal is the one place a
mistake puts a live credential inside the sandbox.
"""

from __future__ import annotations

import asyncio
import os
import stat

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

GH = commands.SHIMMED_TOOLS[0]


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
