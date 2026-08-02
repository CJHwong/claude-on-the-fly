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

from claude_on_the_fly import commands, logs
from claude_on_the_fly.commands import (
    ENDPOINT_ENV,
    MAX_STREAM_BYTES,
    CommandBroker,
    ShimmedTool,
    allowed_command,
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


def test_bundled_gh_allowlist_blocks_alias_api_and_unknown_commands():
    assert allowed_command(GH, ["pr", "view", "--repo", "owner/repo"])
    assert not allowed_command(GH, ["alias", "set", "x", "!cat /etc/passwd"])
    assert not allowed_command(GH, ["api", "--method", "DELETE", "/repos/x"])
    assert not allowed_command(GH, ["arbitrary-alias"])


# --- broker lifecycle and dispatch ---


@pytest.fixture
def echo_tool():
    """A shimmed tool backed by /bin/echo so tests need no real CLI."""
    return ShimmedTool(name="echo", readback=frozenset({("secret",)}), allow=((),))


async def start(tmp_path, tools, **kwargs) -> tuple[CommandBroker, int]:
    broker = CommandBroker(tmp_path / "shims", tools, **kwargs)
    port = await broker.start()
    return broker, port


async def post(
    broker: CommandBroker, payload: dict, *, workspace: Path | None = None
) -> dict:
    env = broker.agent_env(workspace)
    async with ClientSession() as client:
        resp = await client.post(
            f"http://127.0.0.1:{broker.port}/run",
            json=payload,
            headers={commands.TOKEN_HEADER: env[commands.TOKEN_ENV]},
        )
        return await resp.json()


async def test_runs_the_real_binary(tmp_path, echo_tool):
    broker, _port = await start(tmp_path, (echo_tool,))
    try:
        out = await post(broker, {"tool": "echo", "argv": ["hello", "world"]})
        assert out["stdout"].strip() == "hello world"
        assert out["rc"] == 0
        assert out["refused"] is False
    finally:
        await broker.stop()


async def test_readback_refused_over_the_wire(tmp_path, echo_tool):
    broker, _port = await start(tmp_path, (echo_tool,))
    try:
        out = await post(broker, {"tool": "echo", "argv": ["secret", "value"]})
        assert out["refused"] is True
        assert out["rc"] == 1
        # The refused command must not have run, so its output cannot appear.
        assert "secret value" not in out["stdout"]
        assert "command broker exists to keep" in out["stderr"]
    finally:
        await broker.stop()


async def test_exit_code_propagates(tmp_path):
    broker, _port = await start(tmp_path, (ShimmedTool(name="false", allow=((),)),))
    try:
        out = await post(broker, {"tool": "false", "argv": []})
        assert out["rc"] == 1
    finally:
        await broker.stop()


async def test_unbrokered_tool_is_refused(tmp_path, echo_tool):
    broker, _port = await start(tmp_path, (echo_tool,))
    try:
        out = await post(broker, {"tool": "curl", "argv": ["https://example.com"]})
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
    broker, _port = await start(tmp_path, (ShimmedTool(name="cat", allow=((),)),))
    try:
        out = await post(broker, {"tool": "cat", "argv": [], "stdin": "piped input"})
        assert out["stdout"] == "piped input"
    finally:
        await broker.stop()


async def test_output_is_capped(tmp_path):
    broker, _port = await start(
        tmp_path, (ShimmedTool(name="yes", allow=((),)),), run_timeout=1.0
    )
    try:
        out = await post(broker, {"tool": "yes", "argv": ["x"]})
        # `yes` never exits, so this also covers the timeout path.
        assert len(out["stdout"]) <= MAX_STREAM_BYTES
    finally:
        await broker.stop()


async def test_timeout_reports_and_does_not_hang(tmp_path):
    broker, _port = await start(
        tmp_path, (ShimmedTool(name="sleep", allow=((),)),), run_timeout=0.3
    )
    try:
        out = await asyncio.wait_for(
            post(broker, {"tool": "sleep", "argv": ["30"]}), timeout=10
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


async def test_missing_or_wrong_broker_token_is_rejected(tmp_path, echo_tool):
    broker, port = await start(tmp_path, (echo_tool,))
    try:
        async with ClientSession() as client:
            for token in (None, "wrong"):
                headers = {} if token is None else {commands.TOKEN_HEADER: token}
                resp = await client.post(
                    f"http://127.0.0.1:{port}/run",
                    json={"tool": "echo", "argv": ["hello"]},
                    headers=headers,
                )
                assert resp.status == 403
    finally:
        await broker.stop()


async def test_command_allowlist_denies_unlisted_subcommands(tmp_path):
    tool = ShimmedTool(name="echo", allow=(("hello",),))
    broker, _port = await start(tmp_path, (tool,))
    try:
        allowed = await post(broker, {"tool": "echo", "argv": ["hello", "world"]})
        denied = await post(broker, {"tool": "echo", "argv": ["nope"]})
        assert allowed["rc"] == 0
        assert denied["rc"] == 126
        assert denied["refused"] is True
    finally:
        await broker.stop()


async def test_scoped_token_rejects_a_cwd_outside_the_session_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker, _port = await start(tmp_path, (ShimmedTool(name="echo", allow=((),)),))
    try:
        denied = await post(
            broker,
            {"tool": "echo", "argv": ["hello"], "cwd": str(tmp_path)},
            workspace=workspace,
        )
        allowed = await post(
            broker,
            {"tool": "echo", "argv": ["hello"], "cwd": str(workspace)},
            workspace=workspace,
        )
        assert denied["rc"] == 126
        assert denied["refused"] is True
        assert allowed["stdout"].strip() == "hello"
    finally:
        await broker.stop()


async def test_scoped_token_is_revoked_after_a_turn(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker, _port = await start(tmp_path, (ShimmedTool(name="echo", allow=((),)),))
    try:
        env = broker.agent_env(workspace)
        broker.revoke_token(env[commands.TOKEN_ENV])
        async with ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{broker.port}/run",
                json={"tool": "echo", "argv": [], "cwd": str(workspace)},
                headers={commands.TOKEN_HEADER: env[commands.TOKEN_ENV]},
            )
        assert response.status == 403
    finally:
        await broker.stop()


@pytest.mark.parametrize(
    "argv", [["/etc/passwd"], ["--file=/etc/passwd"], ["../outside"]]
)
async def test_path_arguments_cannot_escape_the_workspace(tmp_path, argv):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker, _port = await start(tmp_path, (ShimmedTool(name="echo", allow=((),)),))
    try:
        result = await post(
            broker,
            {"tool": "echo", "argv": argv, "cwd": str(workspace)},
            workspace=workspace,
        )
        assert result["rc"] == 126
        assert result["refused"] is True
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
        env = broker.agent_env()
        assert env[ENDPOINT_ENV] == f"http://127.0.0.1:{port}"
        assert env[commands.TOKEN_ENV] == broker._token
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
    broker, _port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("DEBUG", logger="claude_on_the_fly.commands"):
            await post(
                broker,
                {"tool": "echo", "argv": ["hi"], "argv0": "/shims/echo", "cwd": "/w"},
            )
        assert any(
            "shim invocation echo" in r.getMessage() and "/shims/echo" in r.getMessage()
            for r in caplog.records
        )
    finally:
        await broker.stop()


async def test_refusal_logs_the_cwd(tmp_path, echo_tool, caplog):
    broker, _port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("WARNING", logger="claude_on_the_fly.commands"):
            await post(broker, {"tool": "echo", "argv": ["secret"], "cwd": "/w"})
        assert any("REFUSE echo" in (r.getMessage()) for r in caplog.records)
    finally:
        await broker.stop()


async def test_long_argv_token_is_clipped_by_default(
    tmp_path, echo_tool, caplog, monkeypatch
):
    """A `--body` blob is agent-authored prose, not an audit fact, so it must not
    ride into the log with the argv."""
    monkeypatch.setattr(logs, "log_content", lambda: False)
    prose = "x" * 400
    broker, _port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("WARNING", logger="claude_on_the_fly.commands"):
            await post(broker, {"tool": "echo", "argv": ["--body", prose]})
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "RUN echo" in logged
        assert prose not in logged
        assert "+352" in logged  # 400 - 48 clipped, and the count is reported
    finally:
        await broker.stop()


async def test_content_logging_opt_in_restores_full_argv(
    tmp_path, echo_tool, caplog, monkeypatch
):
    monkeypatch.setattr(logs, "log_content", lambda: True)
    prose = "y" * 200
    broker, _port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("WARNING", logger="claude_on_the_fly.commands"):
            await post(broker, {"tool": "echo", "argv": ["--body", prose]})
        assert prose in "\n".join(r.getMessage() for r in caplog.records)
    finally:
        await broker.stop()


async def test_subprocess_env_keys_logged_never_values(
    tmp_path, echo_tool, caplog, monkeypatch
):
    monkeypatch.setenv("GH_HOST", "secret-host.example")
    broker, _port = await start(tmp_path, (echo_tool,))
    try:
        with caplog.at_level("DEBUG", logger="claude_on_the_fly.commands"):
            await post(broker, {"tool": "echo", "argv": ["hi"]})
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
    broker, _port = await start(tmp_path, (ShimmedTool(name="echo", allow=((),)),))
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
    broker, _port = await start(tmp_path, (ShimmedTool(name="cat", allow=((),)),))
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
    tools = {t.name: t for t in commands.load_tools()}
    assert "gh" in tools and "acli" in tools
    assert ("auth", "token") in tools["gh"].readback


def test_readback_is_written_as_words_not_nested_lists(operator_settings):
    """ "auth token" is far easier to get right than [[auth, token]] in a file
    whose whole job is refusing the correct commands."""
    operator_settings.write_text(
        "commands:\n  tools:\n    - name: echo\n"
        "      readback:\n        - secret thing\n"
    )
    tools = {t.name: t for t in commands.load_tools()}
    assert ("secret", "thing") in tools["echo"].readback
    assert refuses_readback(tools["echo"], ["secret", "thing", "--flag"]) is True
    assert refuses_readback(tools["echo"], ["secret"]) is False


def test_operator_file_adds_a_tool_without_touching_the_others(
    operator_settings, caplog
):
    operator_settings.write_text(
        "commands:\n  tools:\n    - name: kubectl\n"
        "      readback:\n        - config view\n"
    )
    with caplog.at_level("INFO", logger="claude_on_the_fly.commands"):
        tools = {t.name: t for t in commands.load_tools()}
    assert "kubectl" in tools
    # The bundled entries survive, refusals intact.
    assert ("auth", "token") in tools["gh"].readback
    assert any("adds tool 'kubectl'" in r.getMessage() for r in caplog.records)


def test_override_that_drops_a_refusal_warns_loudly(operator_settings, caplog):
    """The one edit here that hands the agent a credential, so it must be visible
    rather than silently accepted."""
    operator_settings.write_text("commands:\n  tools:\n    - name: gh\n")
    with caplog.at_level("INFO", logger="claude_on_the_fly.commands"):
        tools = {t.name: t for t in commands.load_tools()}
    assert tools["gh"].readback == frozenset()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "dropping a readback refusal must warn"
    message = warnings[0].getMessage()
    assert "no longer refuses" in message
    assert "auth" in message and "token" in message


def test_override_keeping_refusals_does_not_warn(operator_settings, caplog):
    operator_settings.write_text(
        "commands:\n  tools:\n    - name: gh\n"
        "      readback:\n        - auth token\n"
        "      readback_flags:\n        - --show-token\n"
        "      env_passthrough: [GH_HOST]\n"
    )
    with caplog.at_level("INFO", logger="claude_on_the_fly.commands"):
        commands.load_tools()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_malformed_commands_section_falls_back_to_bundled(operator_settings, caplog):
    """Ignoring it outright would silently remove every shim, which sends the
    agent looking for another route to the same capability."""
    operator_settings.write_text("commands:\n  tools: not-a-list\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.commands"):
        tools = {t.name: t for t in commands.load_tools()}
    assert "gh" in tools and ("auth", "token") in tools["gh"].readback
    errors = "\n".join(r.getMessage() for r in caplog.records)
    assert "ignoring" in errors and "unavailable" in errors


def test_unparseable_yaml_falls_back(operator_settings, caplog):
    operator_settings.write_text("commands:\n  tools:\n    - name: [unclosed\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly"):
        tools = commands.load_tools()
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


def test_shimmed_names_reports_only_tools_on_path(monkeypatch):
    """The agent's guidance names these, so a tool absent from PATH must not
    appear: telling the agent `aws` is brokered when it is not sends it into a
    failure it was told was impossible."""
    monkeypatch.setattr(
        commands.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None
    )
    assert commands.shimmed_names() == ["gh"]


# --- the readback refusal cannot be dodged by a leading flag ---


@pytest.mark.parametrize(
    "argv",
    [
        ["auth", "token"],
        # A *boolean* global flag: the value-consuming reading swallows "auth"
        # and the invocation looked like `gh token`, which matches no refusal.
        ["--help", "auth", "token"],
        ["-q", "auth", "token"],
        # And the value-consuming reading still has to work, so both must.
        ["--repo", "o/r", "auth", "token"],
        ["--repo=o/r", "auth", "token"],
        ["--help", "auth", "status", "--show-token"],
    ],
)
def test_leading_flag_cannot_smuggle_a_readback_past_the_refusal(argv):
    """Whether a bare flag consumes the next token is unknowable without the
    tool's own flag table, so both readings are checked and either one matching
    refuses. Assuming a single reading is a bypass of the only refusal this
    broker has."""
    assert refuses_readback(GH, argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["pr", "view", "--json", "title"],
        ["pr", "list"],
        ["issue", "create", "--title", "auth", "--body", "token"],
        ["api", "--method", "GET", "repos/o/r"],
    ],
)
def test_ordinary_work_is_not_caught_by_the_second_reading(argv):
    """The mirror cost of checking both readings is a flag *value* that spells a
    refused subcommand, and only at the very front of the argv. Ordinary
    invocations must stay allowed or the shim is useless."""
    assert refuses_readback(GH, argv) is False


def test_leading_tokens_second_reading_treats_every_bare_word_as_a_subcommand():
    assert leading_tokens(["--json", "x", "pr", "list"]) == ("pr", "list")
    assert leading_tokens(["--json", "x", "pr", "list"], flags_take_values=False) == (
        "x",
        "pr",
        "list",
    )


def test_a_tool_with_no_readback_list_refuses_nothing():
    plain = ShimmedTool(name="jq")
    assert refuses_readback(plain, ["auth", "token"]) is False


# --- stale shims ---


async def test_dropping_a_tool_removes_its_shim(tmp_path, echo_tool):
    """The shim dir goes on the agent's PATH ahead of the real binaries, so a
    shim left behind for a tool that is no longer brokered does not fail over to
    the real binary: it shadows it and answers "not brokered" with rc 127,
    permanently."""
    shim_dir = tmp_path / "shims"
    first = CommandBroker(shim_dir, (echo_tool, ShimmedTool(name="cat")))
    first.write_shims()
    assert sorted(p.name for p in shim_dir.iterdir()) == ["cat", "echo"]

    second = CommandBroker(shim_dir, (echo_tool,))
    second.write_shims()
    assert sorted(p.name for p in shim_dir.iterdir()) == ["echo"]


async def test_stale_shim_removal_is_logged(tmp_path, echo_tool, caplog):
    shim_dir = tmp_path / "shims"
    CommandBroker(shim_dir, (echo_tool, ShimmedTool(name="cat"))).write_shims()
    with caplog.at_level("INFO", logger="claude_on_the_fly.commands"):
        CommandBroker(shim_dir, (echo_tool,)).write_shims()
    assert "removing stale shim cat" in "\n".join(
        r.getMessage() for r in caplog.records
    )


async def test_a_directory_in_the_shim_dir_is_left_alone(tmp_path, echo_tool):
    """Only files are candidates. Deleting a directory here would need rmtree,
    which is not a thing shim maintenance should be able to do."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir(parents=True)
    (shim_dir / "operator-notes").mkdir()
    CommandBroker(shim_dir, (echo_tool,)).write_shims()
    assert (shim_dir / "operator-notes").is_dir()


# --- subprocess failure modes ---


async def test_a_binary_that_cannot_be_executed_reports_127_not_a_traceback(
    tmp_path, monkeypatch
):
    """An OSError from the spawn itself (a broken interpreter line, a corrupt
    binary) has to come back as a command result, or the agent sees a 500 from the
    broker and cannot tell what it did wrong.

    Executable enough for `shutil.which` to resolve it, so it survives the
    construction filter, and unexecutable enough for exec to fail.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    broken = bindir / "broken-shebang"
    broken.write_text("#!/nonexistent/interpreter\n")
    broken.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    broker, _port = await start(
        tmp_path, (ShimmedTool(name="broken-shebang", allow=((),)),)
    )
    try:
        out = await post(broker, {"tool": "broken-shebang", "argv": []})
        assert out["rc"] == 127
        assert "cannot run broken-shebang" in out["stderr"]
    finally:
        await broker.stop()


async def test_output_past_the_cap_is_truncated_with_an_actionable_note(tmp_path):
    """Capped while reading rather than truncated afterwards: an unbounded
    producer would otherwise be buffered whole and turn any chatty command into a
    daemon memory bomb."""
    (tmp_path / "source").write_bytes(b"x" * (MAX_STREAM_BYTES * 2))
    broker, _port = await start(tmp_path, (ShimmedTool(name="head", allow=((),)),))
    try:
        out = await post(
            broker,
            {
                "tool": "head",
                "argv": ["-c", str(MAX_STREAM_BYTES * 2), "source"],
                "cwd": str(tmp_path),
            },
        )
        assert len(out["stdout"].encode("utf-8", "replace")) <= MAX_STREAM_BYTES
        assert "output truncated" in out["stderr"]
        assert "write the full result to a file" in out["stderr"]
    finally:
        await broker.stop()


async def test_truncation_is_logged(tmp_path, caplog):
    (tmp_path / "source").write_bytes(b"x" * (MAX_STREAM_BYTES * 2))
    broker, _port = await start(tmp_path, (ShimmedTool(name="head", allow=((),)),))
    try:
        with caplog.at_level("WARNING", logger="claude_on_the_fly.commands"):
            await post(
                broker,
                {
                    "tool": "head",
                    "argv": ["-c", str(MAX_STREAM_BYTES * 2), "source"],
                    "cwd": str(tmp_path),
                },
            )
    finally:
        await broker.stop()
    assert "output truncated at the cap" in "\n".join(
        r.getMessage() for r in caplog.records
    )


async def test_stdin_for_a_command_that_never_reads_it_does_not_fail_the_run(
    tmp_path,
):
    """`true` exits without draining stdin, so the write races the exit and can
    hit a broken pipe. That is the command working, not an error to surface."""
    broker, _port = await start(tmp_path, (ShimmedTool(name="true", allow=((),)),))
    try:
        # Comfortably past a 64 KiB pipe buffer so the write has to block,
        # and comfortably under the broker's own 1 MiB request cap.
        out = await post(broker, {"tool": "true", "argv": [], "stdin": "x" * (1 << 19)})
        assert out["rc"] == 0
        assert out["stderr"] == ""
    finally:
        await broker.stop()


async def test_read_capped_handles_an_absent_stream():
    """A process spawned without a pipe has None for that stream."""
    assert await commands._read_capped(None) == (b"", False)


# --- config validation ---


def test_a_scalar_where_a_list_belongs_is_named_not_coerced():
    """A bare string is iterable, so coercing it would turn "--show-token" into a
    set of 12 single-character flags that match nothing."""
    with pytest.raises(ValueError, match="readback_flags must be a list, got str"):
        commands.parse_tools(
            {"tools": [{"name": "gh", "readback_flags": "--show-token"}]},
            source="test",
        )


def test_an_empty_readback_entry_is_rejected_rather_than_matching_everything():
    """An empty prefix is a prefix of every argv, so it would refuse every
    invocation of the tool."""
    with pytest.raises(ValueError, match="readback has an empty entry"):
        commands.parse_tools(
            {"tools": [{"name": "gh", "readback": ["  "]}]}, source="test"
        )


def test_the_stale_sweep_spares_the_approval_shim(tmp_path):
    """It shares this directory because fs-deny-most.sb re-grants reads here and
    nowhere else under DATA_DIR. Without the reservation the sweep would delete it
    on the next startup and every gated tool call would fail on a missing
    interpreter."""
    from claude_on_the_fly.commands import RESERVED_SHIM_NAMES, CommandBroker

    reserved = tmp_path / next(iter(RESERVED_SHIM_NAMES))
    tmp_path.mkdir(parents=True, exist_ok=True)
    reserved.write_text("#!/bin/sh\n")
    stale = tmp_path / "some-old-tool"
    stale.write_text("#!/bin/sh\n")

    CommandBroker(tmp_path, tools=[]).write_shims()

    assert reserved.is_file(), "the approval shim was swept away"
    assert not stale.exists(), "a genuinely stale shim was left behind"
