"""Tests for the permission-approval config."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from claude_on_the_fly import permissions, settings
from claude_on_the_fly.approvals import ApprovalBroker

# --- defaults ---


def test_off_by_default():
    """Nothing about this feature may turn itself on: the whole compensating
    design (jail, egress proxy, credential broker) assumes today's behaviour is
    what an unconfigured deployment gets."""
    assert permissions.Permissions().mode == "off"
    assert not permissions.Permissions().enabled


def test_bundled_template_ships_the_section_switched_off(operator_settings):
    assert settings.bundled("permissions")
    assert not permissions.configured().enabled


def test_the_bundled_default_claude_mode_is_not_auto():
    """auto hands the decision to a model. Measured on sonnet 5 it approved sudo,
    a write into /etc, chmod -R 777, find -delete and ls ~/.ssh without asking
    once, so it must not be what an operator gets by accident."""
    assert settings.bundled("permissions").get("claude_mode") != "auto"


# --- mode ---


def test_a_bare_off_is_accepted_despite_yaml_reading_it_as_false():
    """YAML 1.1 turns an unquoted `off` into the boolean false. Refusing that
    would be pedantry about quoting in a file operators hand-edit."""
    assert permissions.parse({"mode": False}).mode == "off"


def test_on_is_refused_rather_than_guessed_at():
    """`on` is not one of the two names, and picking one for the operator is how a
    security feature ends up in a state nobody chose."""
    with pytest.raises(permissions.ConfigError, match="not a mode"):
        permissions.parse({"mode": True})


@pytest.mark.parametrize("value", ["asked", "ON", 3, None])
def test_an_unrecognised_mode_is_refused(value):
    with pytest.raises(permissions.ConfigError):
        permissions.parse({"mode": value})


def test_ask_is_case_and_whitespace_tolerant():
    assert permissions.parse({"mode": " ASK "}).mode == "ask"
    assert permissions.parse({"mode": "ask"}).enabled


# --- claude_mode ---


@pytest.mark.parametrize("mode", permissions.CLAUDE_MODES)
def test_every_advertised_claude_mode_parses(mode):
    assert permissions.parse({"claude_mode": mode}).claude_mode == mode


@pytest.mark.parametrize("mode", permissions.SILENT_CLAUDE_MODES)
def test_the_silent_modes_are_refused_and_say_why(mode):
    """Both were measured at zero prompts. Pairing either with `ask` gives a
    deployment that reports approvals as on and gates nothing, which is worse
    than refusing to start."""
    with pytest.raises(permissions.ConfigError, match="never prompts"):
        permissions.parse({"claude_mode": mode})


def test_plan_mode_is_not_accepted():
    """Its interaction with a gate has never been measured, so it is not offered."""
    with pytest.raises(permissions.ConfigError, match="not recognised"):
        permissions.parse({"claude_mode": "plan"})


def test_a_non_string_claude_mode_is_refused():
    with pytest.raises(permissions.ConfigError, match="must be one of"):
        permissions.parse({"claude_mode": 1})


# --- durations ---


def test_durations_default_when_absent():
    resolved = permissions.parse({})
    assert resolved.ttl_seconds == permissions.DEFAULT_TTL_SECONDS
    assert resolved.timeout_seconds == permissions.DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize("key", ["ttl_seconds", "timeout_seconds"])
@pytest.mark.parametrize("value", [0, -5, "soon", True])
def test_a_nonsensical_duration_is_refused(key, value):
    """True is refused explicitly: bool is an int subclass, so without the check a
    `timeout_seconds: yes` would resolve to a one-second answer window."""
    with pytest.raises(permissions.ConfigError):
        permissions.parse({key: value})


def test_durations_accept_ints_and_floats():
    resolved = permissions.parse({"ttl_seconds": 60, "timeout_seconds": 12.5})
    assert (resolved.ttl_seconds, resolved.timeout_seconds) == (60.0, 12.5)


# --- operator overrides ---


def test_the_operator_file_overrides_the_bundled_defaults(operator_settings):
    operator_settings.write_text(
        "permissions:\n  mode: ask\n  claude_mode: manual\n  timeout_seconds: 45\n"
    )
    resolved = permissions.configured()
    assert resolved.enabled
    assert resolved.claude_mode == "manual"
    assert resolved.timeout_seconds == 45.0


def test_a_broken_block_falls_back_to_off_and_says_so(operator_settings, caplog):
    """Failing closed here would gate every tool call in a deployment that never
    asked for it, so a typo has to mean "behave as before"."""
    operator_settings.write_text("permissions:\n  mode: ask\n  claude_mode: nonsense\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
        assert not permissions.configured().enabled
    assert "are OFF and tool calls are not gated" in caplog.text


def test_permissions_is_a_recognised_section(operator_settings, caplog):
    """Otherwise the startup check would report the whole block as an unknown
    top-level key that does nothing."""
    assert "permissions" in settings.SECTIONS
    operator_settings.write_text("permissions:\n  mode: ask\n")
    with caplog.at_level("ERROR", logger="claude_on_the_fly.settings"):
        settings.check_operator_settings()
    assert caplog.text == ""


# --- startup report ---


def test_startup_check_is_quiet_at_info_when_off(operator_settings, caplog):
    with caplog.at_level("WARNING", logger="claude_on_the_fly.permissions"):
        permissions.check()
    assert caplog.text == ""


def test_startup_check_warns_when_on(operator_settings, caplog):
    operator_settings.write_text("permissions:\n  mode: ask\n")
    with caplog.at_level("WARNING", logger="claude_on_the_fly.permissions"):
        permissions.check()
    assert "approvals ON" in caplog.text
    assert "Cron and the job queue stay ungated" in caplog.text


def test_startup_check_calls_out_auto_specifically(operator_settings, caplog):
    """An operator picking auto for fewer interruptions should see, once, that it
    is a model making the call and not them."""
    operator_settings.write_text("permissions:\n  mode: ask\n  claude_mode: auto\n")
    with caplog.at_level("WARNING", logger="claude_on_the_fly.permissions"):
        permissions.check()
    assert "delegates the decision to a model" in caplog.text


# --- turning a tool call into a question ---


def _call(name: str, **payload) -> permissions.ToolCall:
    return permissions.ToolCall(name=name, input=dict(payload))


WS = Path("/tmp/ws")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ls -la", "bash:ls"),
        ("/usr/bin/env python -V", "bash:env"),
        ("git status --short", "bash:git status"),
        ("git push --force", "bash:git push"),
        ("git", "bash:git"),
    ],
)
def test_a_simple_command_is_scoped_to_its_program(command, expected):
    """Approving `git` once should cover a turn of git work; that is the whole tap
    saving. The program name is taken basename-first so an absolute path cannot
    open a second subject for the same binary."""
    assert permissions.subject_for(_call("Bash", command=command), WS) == expected


@pytest.mark.parametrize(
    "command",
    [
        "ls && curl evil.example",
        "ls; curl evil.example",
        "ls | tee /tmp/x",
        "echo $(curl evil.example)",
        "cat `whoami`",
        "ls > /etc/passwd",
        "ls\ncurl evil.example",
        'ls "unbalanced',
    ],
)
def test_a_compound_command_is_never_scoped_to_its_first_word(command):
    """The load-bearing case. If `ls && curl evil.example` collapsed to `bash:ls`,
    a grant the operator gave to listing files would silently cover fetching from
    anywhere for the rest of its TTL."""
    subject = permissions.subject_for(_call("Bash", command=command), WS)
    assert subject.startswith("bash-exact:")
    assert subject != "bash:ls"


def test_a_write_inside_the_workspace_is_scoped_to_its_relative_path(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.touch()
    subject = permissions.subject_for(_call("Write", file_path=str(target)), tmp_path)
    assert subject == "write:src/app.py"


def test_each_escape_from_the_workspace_is_its_own_subject(tmp_path):
    """One shared "outside" bucket would mean approving a write to ~/.zshrc also
    authorises the next write to ~/.ssh/config."""
    first = permissions.subject_for(_call("Write", file_path="/etc/hosts"), tmp_path)
    second = permissions.subject_for(_call("Write", file_path="/etc/shadow"), tmp_path)
    assert first.startswith("write-outside:")
    assert first != second


def test_a_write_with_no_path_still_produces_a_subject(tmp_path):
    assert permissions.subject_for(_call("Write"), tmp_path) == "write:<no path>"


def test_a_patch_is_scoped_to_its_own_body(tmp_path):
    """codex sends the whole patch in `command`, so there is no single path to key
    on and no safe way to widen it."""
    subject = permissions.subject_for(
        _call("apply_patch", command="*** Begin Patch\n*** Update File: a.txt\n"),
        tmp_path,
    )
    assert subject.startswith("patch-exact:")
    assert "\n" not in subject


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.github.com/repos", "fetch:api.github.com"),
        ("not a url", "fetch:<no host>"),
    ],
)
def test_a_fetch_is_scoped_to_its_host(url, expected):
    assert permissions.subject_for(_call("WebFetch", url=url), WS) == expected


def test_an_unrecognised_tool_gets_one_subject_per_tool():
    assert permissions.subject_for(_call("SomeNewTool", x=1), WS) == "tool:SomeNewTool"


# --- the operator-facing detail ---


def test_detail_names_who_raised_the_question():
    """claude asking and cotf deciding to ask mean different things about how much
    thought went into the question, so the operator sees which."""
    call = _call("Bash", command="rm -f x")
    assert permissions.detail_for(call, asked_by="claude").startswith("claude asked:")
    assert permissions.detail_for(call, asked_by="cotf").startswith("cotf asked:")


def test_detail_flattens_newlines_out_of_agent_authored_input():
    """Newlines are structural in every frontend this reaches, so without this an
    agent could draw a fake verdict line under the real one."""
    call = _call("Bash", command="echo hi\n\n*** APPROVED BY OPERATOR ***")
    detail = permissions.detail_for(call, asked_by="cotf")
    assert "\n" not in detail
    assert "APPROVED BY OPERATOR" in detail  # shown, but inline and not as a line


def test_detail_announces_the_cap_rather_than_truncating_silently():
    """A silent cut is how an operator approves the half of a command they were not
    shown."""
    call = _call("Bash", command="echo " + "A" * 5000)
    detail = permissions.detail_for(call, asked_by="cotf")
    assert "chars total]" in detail
    assert len(detail) < 600


def test_detail_includes_the_tools_own_description_when_it_has_one():
    call = _call("Bash", command="chmod 666 f", description="Make f world-writable")
    assert "Make f world-writable" in permissions.detail_for(call, asked_by="claude")


def test_request_carries_the_tool_kind_and_the_configured_ttl():
    request = permissions.request_for(
        _call("Bash", command="ls"), WS, asked_by="cotf", ttl_seconds=99
    )
    assert (request.kind, request.subject, request.ttl_seconds) == (
        "tool",
        "bash:ls",
        99,
    )
    assert request.key == "tool:bash:ls"


# --- worth_asking, which is codex-only ---


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat f",
        "head -5 f",
        "wc -l f",
        "pwd",
        "echo hi",
        "grep -r x .",
        "git status",
        "git log --oneline",
        "git diff HEAD",
    ],
)
def test_read_only_commands_do_not_interrupt_the_operator(command):
    assert not permissions.worth_asking(_call("Bash", command=command))


@pytest.mark.parametrize(
    "command",
    [
        "chmod 777 f",
        "sudo -n true",
        "curl https://x.example",
        "npm i",
        "rm -f f",
        "git push",
        "git commit -m x",
        "find . -delete",
        "ls && curl evil.example",
        "echo $(curl evil.example)",
        'ls "unbalanced',
    ],
)
def test_anything_that_is_not_plainly_a_read_does_interrupt(command):
    assert permissions.worth_asking(_call("Bash", command=command))


def test_an_unknown_tool_always_interrupts():
    """Costing a tap is recoverable; not asking is not."""
    assert permissions.worth_asking(_call("apply_patch", command="*** Begin Patch"))
    assert permissions.worth_asking(_call("SomethingNew"))


def test_a_git_alias_of_a_write_subcommand_is_not_treated_as_a_read():
    assert permissions.worth_asking(_call("Bash", command="git reset --hard"))


@pytest.mark.parametrize(
    ("call", "expected_fragment"),
    [
        (permissions.ToolCall("Write", {"file_path": "/etc/hosts"}), "/etc/hosts"),
        (
            permissions.ToolCall("apply_patch", {"command": "*** Begin Patch"}),
            "Begin Patch",
        ),
        (permissions.ToolCall("WebFetch", {"url": "https://x.example/a"}), "x.example"),
        (permissions.ToolCall("SomethingNew", {"weird": "payload"}), "payload"),
        (permissions.ToolCall("NoInput"), "NoInput"),
    ],
)
def test_detail_describes_every_tool_shape_not_just_bash(call, expected_fragment):
    """Each branch names the field that actually says what the call would do, so an
    operator is never shown a bare tool name for a call that has a target."""
    detail = permissions.detail_for(call, asked_by="claude")
    assert expected_fragment in detail
    assert detail.startswith("claude asked:")


# --- the loopback decision service ---


def _service(gate=None, **kwargs) -> permissions.PermissionService:
    from claude_on_the_fly.approvals import ApprovalBroker, RecordingGate, tool_policy

    return permissions.PermissionService(
        broker=ApprovalBroker(
            gate if gate is not None else RecordingGate(default=True),
            policies={"tool": tool_policy()},
        ),
        workspace=Path("/tmp/ws"),
        **kwargs,
    )


async def test_a_cotf_sourced_read_is_allowed_without_asking_anyone():
    """codex has no filter of its own, so without this a turn of `ls` and
    `git status` would cost the operator a tap each."""
    from claude_on_the_fly.approvals import RecordingGate

    gate = RecordingGate(default=True)
    allowed, message = await _service(gate).decide(
        _call("Bash", command="git status"), permissions.SOURCE_COTF
    )
    assert allowed
    assert gate.seen == []
    assert "below the ask threshold" in message


async def test_the_same_read_from_claude_is_forwarded_not_filtered():
    """claude only calls out for what it would have prompted about, so filtering
    its questions again would drop questions it had already decided to ask."""
    from claude_on_the_fly.approvals import RecordingGate

    gate = RecordingGate(default=True)
    allowed, _ = await _service(gate).decide(
        _call("Bash", command="git status"), permissions.SOURCE_CLAUDE
    )
    assert allowed
    assert [request.subject for request in gate.seen] == ["bash:git status"]


async def test_a_denial_tells_the_agent_what_to_do_next():
    """Not "denied by policy": the agent cannot distinguish a refusal from a
    timeout or a rate limit, and naming one would put words in the operator's
    mouth."""
    from claude_on_the_fly.approvals import RecordingGate

    allowed, message = await _service(RecordingGate(default=False)).decide(
        _call("Bash", command="curl https://x.example"), permissions.SOURCE_COTF
    )
    assert not allowed
    assert "did not approve" in message
    assert "Do not retry" in message


async def test_the_configured_ttl_reaches_the_grant():
    from claude_on_the_fly.approvals import RecordingGate

    gate = RecordingGate(default=True)
    await _service(gate, ttl_seconds=42).decide(
        _call("Bash", command="chmod 700 f"), permissions.SOURCE_COTF
    )
    assert gate.seen[0].ttl_seconds == 42


async def test_the_service_answers_over_loopback():
    """Live: a real socket, because the shim reaches this over HTTP and a unit test
    against `decide` alone would not exercise the request parsing."""
    import aiohttp

    service = _service()
    await service.start()
    try:
        assert service.base_url.startswith("http://127.0.0.1:")
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                service.base_url + permissions.DECIDE_PATH,
                json={
                    "source": "claude",
                    "tool_name": "Bash",
                    "input": {"command": "ls"},
                    "tool_use_id": "toolu_1",
                },
            ) as response,
        ):
            assert (await response.json())["behavior"] == "allow"
    finally:
        await service.stop()


async def test_port_and_base_url_refuse_before_start():
    service = _service()
    with pytest.raises(RuntimeError, match="not started"):
        _ = service.port


@pytest.mark.parametrize(
    ("body", "expected_status"),
    [("not json", 400), ('["a list"]', 400)],
)
async def test_a_malformed_request_is_rejected_not_allowed(body, expected_status):
    import aiohttp

    service = _service()
    await service.start()
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                service.base_url + permissions.DECIDE_PATH,
                data=body,
                headers={"Content-Type": "application/json"},
            ) as response,
        ):
            assert response.status == expected_status
    finally:
        await service.stop()


async def test_a_request_with_no_tool_name_is_denied():
    """Fail closed: an unnamed call cannot be classified, so allowing it would make
    a malformed request the way around the gate."""
    import aiohttp

    service = _service()
    await service.start()
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                service.base_url + permissions.DECIDE_PATH, json={"input": {}}
            ) as response,
        ):
            assert (await response.json())["behavior"] == "deny"
    finally:
        await service.stop()


async def test_a_missing_source_is_treated_as_cotf_not_as_claude():
    """The two differ in whether cotf filters. Defaulting to claude would forward
    everything and defeat the filter; defaulting to cotf only risks a quiet allow
    for reads, which is the recoverable direction."""
    import aiohttp

    from claude_on_the_fly.approvals import RecordingGate

    gate = RecordingGate(default=True)
    service = _service(gate)
    await service.start()
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                service.base_url + permissions.DECIDE_PATH,
                json={"tool_name": "Bash", "input": {"command": "ls"}},
            ) as response,
        ):
            assert (await response.json())["behavior"] == "allow"
        assert gate.seen == []
    finally:
        await service.stop()


async def test_stop_is_safe_before_start():
    await _service().stop()


# --- wiring a backend up ---


def test_claude_argv_is_the_old_pair_when_off():
    """The off path has to be byte-identical to the behaviour before this feature,
    or enabling nothing still changes how every session is spawned."""
    assert permissions.claude_argv(permissions.Permissions()) == [
        "--permission-mode",
        "bypassPermissions",
    ]


def test_codex_argv_is_empty_when_off():
    assert permissions.codex_argv(permissions.Permissions()) == []


def test_claude_argv_sets_the_mode_and_the_prompt_tool_together():
    """Only correct as a pair: under bypassPermissions claude asks nothing and the
    prompt tool is never called, which is why claude_mode refuses that value."""
    argv = permissions.claude_argv(
        permissions.Permissions(mode="ask", claude_mode="manual")
    )
    assert argv[:2] == ["--permission-mode", "manual"]
    assert "--permission-prompt-tool" in argv
    assert permissions.PROMPT_TOOL in argv
    assert str(permissions.mcp_config_path()) in argv


def test_the_prompt_tool_name_matches_the_mcp_server_it_advertises():
    """claude validates this name at startup and refuses to run if it does not
    resolve, so a mismatch here is a daemon that cannot spawn a turn at all."""
    from claude_on_the_fly import cotf_approve

    assert (
        f"mcp__{permissions.MCP_SERVER_NAME}__{cotf_approve.TOOL_NAME}"
    ) == permissions.PROMPT_TOOL


def test_codex_argv_carries_the_hook_and_the_trust_bypass_together():
    """Without the bypass the hook is silently skipped and the command runs anyway,
    which is the exact failure this feature exists to prevent. They must never be
    separable."""
    argv = permissions.codex_argv(permissions.Permissions(mode="ask"))
    assert "--dangerously-bypass-hook-trust" in argv
    hook = argv[argv.index("-c") + 1]
    assert hook.startswith("hooks.PreToolUse=[")
    assert f"{permissions.shim_path()} hook" in hook


def test_the_codex_hook_timeout_follows_the_configured_answer_window():
    """A hook that gives up before the operator's window closes would turn a slow
    approval into a denial codex attributes to nobody."""
    argv = permissions.codex_argv(
        permissions.Permissions(mode="ask", timeout_seconds=123)
    )
    assert "timeout=123" in argv[argv.index("-c") + 1]


def test_the_shim_is_generated_executable_and_reserved(operator_settings):
    """It shares the command broker's shim directory, whose stale sweep removes
    anything it does not recognise, so the reservation is what stops it being
    deleted on the next startup."""
    import os

    from claude_on_the_fly import commands

    path = permissions.write_shim()
    assert path.is_file()
    assert os.access(path, os.X_OK)
    assert "cotf_approve" in path.read_text()
    assert path.name in commands.RESERVED_SHIM_NAMES


def test_the_mcp_config_names_the_shim_and_not_a_port(operator_settings):
    """Baking a port in would point one chat's approvals at another chat's grant
    store, since the config file is shared and the port is per session."""
    import json

    path = permissions.write_mcp_config()
    config = json.loads(path.read_text())
    server = config["mcpServers"][permissions.MCP_SERVER_NAME]
    assert server["command"] == str(permissions.shim_path())
    assert server["args"] == ["mcp"]
    assert "127.0.0.1" not in path.read_text()


# --- the ungated-turn guard ---


def test_a_turn_that_used_tools_without_asking_is_reported(caplog):
    """The substitute for a startup self-test. codex treats an untrusted or crashed
    hook as no opinion and runs the command, so the failure is silent by
    construction: the operator sees a normal turn and believes it was supervised."""
    with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
        assert permissions.warn_if_ungated(3, 0, backend="codex") is True
    assert "ran UNSUPERVISED" in caplog.text
    assert "codex" in caplog.text


@pytest.mark.parametrize(
    ("tool_calls", "requests_seen"),
    [(0, 0), (3, 1), (0, 5)],
)
def test_the_guard_stays_quiet_when_there_is_nothing_wrong(
    tool_calls, requests_seen, caplog
):
    """A turn with no tool calls asks nothing legitimately, and a turn that reached
    the gate at all proves the wiring is attached."""
    with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
        assert (
            permissions.warn_if_ungated(tool_calls, requests_seen, backend="x") is False
        )
    assert caplog.text == ""


async def test_the_service_counts_every_decision_it_is_asked_for():
    """Including the ones it answers itself: a filtered read still proves the gate
    was reached, which is the only thing the guard needs to know."""
    from claude_on_the_fly.approvals import RecordingGate

    service = _service(RecordingGate(default=True))
    assert service.requests_seen == 0
    await service.decide(_call("Bash", command="ls"), permissions.SOURCE_COTF)
    await service.decide(_call("Bash", command="curl x"), permissions.SOURCE_COTF)
    assert service.requests_seen == 2


def test_pty_gets_the_permission_mode_but_not_the_prompt_tool():
    """Interactive claude resolves --permission-prompt-tool, connects the server,
    answers tools/list, and then never calls it: it draws its own dialog. Handing it
    the flag anyway would start an MCP server nothing ever talks to. The dialog is
    relayed through the Notification hook instead."""
    argv = permissions.claude_argv(
        permissions.Permissions(mode="ask", claude_mode="manual"), pty=True
    )
    assert argv == ["--permission-mode", "manual"]
    assert "--permission-prompt-tool" not in argv
    assert "--mcp-config" not in argv


def test_native_still_gets_the_prompt_tool():
    """The two paths must not converge by accident: native has no dialog to read, so
    the prompt tool is the only channel it has."""
    argv = permissions.claude_argv(permissions.Permissions(mode="ask"), pty=False)
    assert "--permission-prompt-tool" in argv


def test_pty_with_approvals_off_is_silent(caplog):
    with caplog.at_level("WARNING", logger="claude_on_the_fly.permissions"):
        argv = permissions.claude_argv(permissions.Permissions(), pty=True)
    assert argv == ["--permission-mode", "bypassPermissions"]
    assert caplog.text == ""


# --- reading claude's own dialog (pty) ---

# Verbatim from a real claude-pty pane at 80x24. Kept exact, curly apostrophe and
# all, because the point of these is to catch the day the real thing stops matching.
REAL_BASH_DIALOG = """\
           Haiku 4.5 · Claude Team
  ▘▘ ▝▝    ~/.claude/jobs/82b9d5ff/tmp/probe_ws

❯ I am setting up a test fixture. Please make bp.txt world-writable with chmod,
  then also read AGENTS.md if it exists.

  Reading 1 file, running 1 shell command…
  ⎿  AGENTS.md

────────────────────────────────────────────────────────────────────────────────
 Bash command

   chmod a+w /Users/x/probe_ws/bp.txt
   Make bp.txt world-writable

 This command requires approval

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don’t ask again for: chmod a+w *
   3. No

 Esc to cancel · Tab to amend · ctrl+e to explain
"""

# The other observed shape: a wrapped prose explanation, no command block.
REAL_SENSITIVE_FILE_DIALOG = """\
────────────────────────────────────────────────────────────────────────────────
 Claude requested permissions to edit
 /Users/x/probe_ws/pty_probe.tmp which is a
 sensitive file.
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to probe_ws/ from this project
   3. No

 Esc to cancel · Tab to amend · ctrl+e to explain
"""


def test_the_tool_name_comes_from_the_dialog_header():
    """Only for labelling. Nothing is decided from it, which is why an unheaded
    dialog is still answerable."""
    dialog = permissions.parse_dialog(REAL_BASH_DIALOG)
    assert dialog is not None
    assert dialog.tool == "Bash"
    assert dialog.subject.startswith("pty:Bash:")


def test_the_grant_is_scoped_to_this_exact_dialog():
    """A terminal hard-wraps, so the body cannot be trusted to reproduce a path
    exactly and therefore cannot be an identity. Hashing the whole thing means a
    grant matches only an identical prompt: less reuse, no chance of over-widening."""
    one = permissions.parse_dialog(REAL_BASH_DIALOG)
    two = permissions.parse_dialog(REAL_SENSITIVE_FILE_DIALOG)
    assert one is not None and two is not None
    assert one.subject != two.subject
    # Stable across reads of the same prompt, or a grant would never be reused at all.
    assert one.subject == permissions.parse_dialog(REAL_BASH_DIALOG).subject


def test_the_yes_and_no_keys_are_read_from_the_real_dialog():
    dialog = permissions.parse_dialog(REAL_BASH_DIALOG)
    assert dialog is not None
    assert (dialog.yes_key, dialog.no_key) == ("1", "3")


def test_the_widening_option_is_never_a_candidate():
    """Answering `2. Yes, and don't ask again for: chmod a+w *` installs a standing
    wildcard rule the operator was never shown. It must not be reachable, and it must
    not be mistaken for the plain yes."""
    dialog = permissions.parse_dialog(REAL_BASH_DIALOG)
    assert dialog is not None
    assert "2" not in (dialog.yes_key, dialog.no_key)


def test_the_body_carries_why_claude_escalated():
    """The reason exists nowhere else. The transcript records what the call is; only
    the dialog says it needed approval, or that the target is a sensitive file."""
    dialog = permissions.parse_dialog(REAL_BASH_DIALOG)
    assert dialog is not None
    assert "This command requires approval" in dialog.body
    assert "chmod a+w" in dialog.body


def test_a_wrapped_explanation_is_rejoined_not_truncated():
    """A rule like "take the last line" would have reduced this to "sensitive
    file." and dropped what it was about."""
    dialog = permissions.parse_dialog(REAL_SENSITIVE_FILE_DIALOG)
    assert dialog is not None
    assert "sensitive file" in dialog.body
    assert "pty_probe.tmp" in dialog.body
    assert "\n" not in dialog.body


def test_the_option_list_position_is_not_assumed():
    """Both real dialogs put No at 3 only because both offered a widen-scope option
    in the middle. Without it, No is 2, and a hardcoded 3 would type into whatever is
    actually there."""
    two_option = (
        "────────\n Something happened\n Do you want to proceed?\n"
        " ❯ 1. Yes\n   2. No\n\n Esc to cancel\n"
    )
    dialog = permissions.parse_dialog(two_option)
    assert dialog is not None
    assert (dialog.yes_key, dialog.no_key) == ("1", "2")


@pytest.mark.parametrize(
    "pane",
    [
        "",
        "just some output, no dialog here\n",
        # The question is there but the options never rendered.
        "────────\n Do you want to proceed?\n\n Esc to cancel\n",
        # Only a widening yes, so there is no plain approve to send.
        "────────\n x\n Do you want to proceed?\n ❯ 1. Yes, and always allow\n   2. No\n",
        # No refuse option at all.
        "────────\n x\n Do you want to proceed?\n ❯ 1. Yes\n",
    ],
)
def test_an_unreadable_dialog_is_refused_rather_than_guessed_at(pane):
    """None means "do not type anything". Guessing a digit into a live session is
    how an operator's no turns into a standing allow."""
    assert permissions.parse_dialog(pane) is None


def test_the_body_is_capped_like_every_other_agent_authored_string():
    """The command and its description come from the agent, so they land in this
    text and it needs the same bound as tool input does."""
    huge = (
        "────────\n Bash command\n   echo " + "A" * 5000 + "\n"
        " needs approval\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
    )
    dialog = permissions.parse_dialog(huge)
    assert dialog is not None
    assert "chars total]" in dialog.body


# --- the pty relay ---


@pytest.fixture
def fake_tmux(monkeypatch):
    """Stand in for tmux, recording every send-keys and serving a pane."""
    sent: list[list[str]] = []
    pane = {"text": REAL_BASH_DIALOG}

    async def fake(*args: str):
        sent.append(list(args))
        if args[0] == "capture-pane":
            return 0, pane["text"]
        return 0, ""

    monkeypatch.setattr(permissions, "_tmux", fake)
    return sent, pane


def _pty_service(gate, **kwargs) -> permissions.PermissionService:
    from claude_on_the_fly.approvals import ApprovalBroker, tool_policy

    return permissions.PermissionService(
        broker=ApprovalBroker(gate, policies={"tool": tool_policy()}),
        workspace=Path("/tmp/ws"),
        tmux_session="cotf-pty-1-abcd1234",
        **kwargs,
    )


async def test_an_approved_dialog_gets_the_yes_key_and_nothing_else(fake_tmux):
    """Allow is a single keystroke. Anything more typed into a live pane is a risk
    with no upside."""
    from claude_on_the_fly.approvals import RecordingGate

    sent, _pane = fake_tmux
    service = _pty_service(RecordingGate(default=True))
    assert await service.relay_pty_dialog() is True
    keys = [args[3] for args in sent if args[0] == "send-keys"]
    assert keys == ["1"]


async def test_a_refused_dialog_also_injects_a_reason(fake_tmux):
    """The keystroke alone ends the turn without a final assistant message, so
    claude's Stop hook never fires, no envelope is written, and claude-pty waits until
    it gives up. The injected message is also the only way the reason reaches the
    model, since a keystroke carries no text."""
    from claude_on_the_fly.approvals import RecordingGate

    sent, _pane = fake_tmux
    service = _pty_service(RecordingGate(default=False))
    assert await service.relay_pty_dialog() is True
    keys = [args[3] for args in sent if args[0] == "send-keys"]
    assert keys[0] == "3"
    assert permissions.PTY_DENY_MESSAGE in keys
    assert keys[-1] == "Enter"


async def test_the_widening_option_is_never_typed(fake_tmux):
    """Option 2 is `Yes, and don't ask again for: chmod a+w *`. Sending it installs a
    standing wildcard rule the operator was never shown."""
    from claude_on_the_fly.approvals import RecordingGate

    sent, _pane = fake_tmux
    await _pty_service(RecordingGate(default=True)).relay_pty_dialog()
    assert "2" not in [args[3] for args in sent if args[0] == "send-keys"]


async def test_the_operator_sees_claudes_own_wording(fake_tmux):
    from claude_on_the_fly.approvals import RecordingGate

    gate = RecordingGate(default=True)
    await _pty_service(gate).relay_pty_dialog()
    assert "This command requires approval" in gate.seen[0].detail
    assert gate.seen[0].subject.startswith("pty:Bash:")


async def test_an_unreadable_dialog_types_nothing_at_all(fake_tmux, caplog):
    """Refusing to answer beats guessing a digit: a wrong one could approve something
    the operator never saw."""
    from claude_on_the_fly.approvals import RecordingGate

    sent, pane = fake_tmux
    pane["text"] = "no dialog here, just output"
    gate = RecordingGate(default=True)
    with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
        assert await _pty_service(gate).relay_pty_dialog() is False
    assert [args for args in sent if args[0] == "send-keys"] == []
    assert gate.seen == []
    assert "guessing a key" in caplog.text


async def test_a_service_with_no_pane_refuses_to_type(caplog):
    """A backend with no terminal cannot be answered this way, and the daemon must not
    go looking for someone else's pane."""
    from claude_on_the_fly.approvals import RecordingGate

    service = permissions.PermissionService(
        broker=ApprovalBroker(RecordingGate(default=True)),
        workspace=Path("/tmp/ws"),
    )
    with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
        assert await service.relay_pty_dialog() is False
    assert "no tmux session" in caplog.text


async def test_a_relayed_dialog_counts_toward_the_ungated_turn_guard(fake_tmux):
    """Otherwise a pty session that only ever answered dialogs would look, to the
    guard, like a session whose gate was never attached."""
    from claude_on_the_fly.approvals import RecordingGate

    service = _pty_service(RecordingGate(default=True))
    assert service.requests_seen == 0
    await service.relay_pty_dialog()
    assert service.requests_seen == 1


async def test_tmux_refusing_the_keystroke_is_reported(monkeypatch, caplog):
    from claude_on_the_fly.approvals import RecordingGate

    async def broken(*args: str):
        if args[0] == "capture-pane":
            return 0, REAL_BASH_DIALOG
        return 1, ""

    monkeypatch.setattr(permissions, "_tmux", broken)
    with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
        assert (
            await _pty_service(RecordingGate(default=True)).relay_pty_dialog() is False
        )
    assert "could not answer the dialog" in caplog.text


async def test_read_dialog_waits_for_the_prompt_to_finish_painting(monkeypatch):
    """The hook fires when claude decides to prompt, which is a moment before the
    prompt has rendered."""
    calls = {"n": 0}

    async def slow(*args: str):
        if args[0] != "capture-pane":
            return 0, ""
        calls["n"] += 1
        return 0, ("" if calls["n"] < 3 else REAL_BASH_DIALOG)

    monkeypatch.setattr(permissions, "_tmux", slow)
    monkeypatch.setattr(permissions, "_POLL_INTERVAL_SECONDS", 0.001)
    dialog = await permissions.read_dialog("pane")
    assert dialog is not None
    assert calls["n"] >= 3


async def test_read_dialog_gives_up_rather_than_polling_forever(monkeypatch):
    async def blank(*_args: str):
        return 0, "nothing"

    monkeypatch.setattr(permissions, "_tmux", blank)
    monkeypatch.setattr(permissions, "_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(permissions, "_TRANSCRIPT_WAIT_SECONDS", 0.01)
    assert await permissions.read_dialog("pane") is None


async def test_a_missing_tmux_binary_does_not_raise(monkeypatch):
    """The daemon may run somewhere tmux is not installed, and that must degrade to
    "cannot answer" rather than taking the turn down."""

    async def explode(*_a, **_k):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(permissions.asyncio, "create_subprocess_exec", explode)
    assert await permissions.capture_pane("pane") == ""


async def test_notify_returns_at_once_and_relays_in_the_background():
    """claude is blocked on its dialog, not on this request, so holding it open would
    delay the hook without changing anything."""
    import aiohttp

    from claude_on_the_fly.approvals import RecordingGate

    relayed = asyncio.Event()
    service = _pty_service(RecordingGate(default=True))

    async def fake_relay():
        relayed.set()
        return True

    service.relay_pty_dialog = fake_relay  # type: ignore[method-assign]
    await service.start()
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                service.base_url + permissions.NOTIFY_PATH, json={}
            ) as response,
        ):
            assert response.status == 202
        await asyncio.wait_for(relayed.wait(), timeout=2)
    finally:
        await service.stop()


@pytest.mark.parametrize("body", ["not json", '["a list"]'])
async def test_a_malformed_notify_is_rejected(body):
    import aiohttp

    from claude_on_the_fly.approvals import RecordingGate

    service = _pty_service(RecordingGate(default=True))
    await service.start()
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                service.base_url + permissions.NOTIFY_PATH,
                data=body,
                headers={"Content-Type": "application/json"},
            ) as response,
        ):
            assert response.status == 400
    finally:
        await service.stop()


def test_the_pane_name_is_unique_per_chat_and_session():
    """claude-pty's own default is PID-based, which the daemon cannot predict, and a
    shared name would let one chat's approval land in another chat's pane."""
    first = permissions.tmux_session_name(1, "aaaaaaaa-1111")
    assert first != permissions.tmux_session_name(2, "aaaaaaaa-1111")
    assert first != permissions.tmux_session_name(1, "bbbbbbbb-2222")
    assert first == permissions.tmux_session_name(1, "aaaaaaaa-9999")[: len(first)]


def test_pty_settings_install_only_the_permission_prompt_hook(operator_settings):
    """Supplied as an extra settings source, not written into the operator's own
    settings.json, which claude-pty already depends on for its Stop hook."""
    import json

    path = permissions.write_pty_settings()
    hooks = json.loads(path.read_text())["hooks"]["Notification"]
    assert hooks[0]["matcher"] == "permission_prompt"
    assert hooks[0]["hooks"][0]["command"].endswith(" notify")


def test_pty_argv_is_empty_when_approvals_are_off():
    assert permissions.pty_argv(permissions.Permissions()) == []


async def test_a_relay_task_is_held_until_it_finishes(monkeypatch):
    """A bare create_task can be garbage collected mid-relay, which would abandon a
    dialog with claude still parked on it."""
    from claude_on_the_fly.approvals import RecordingGate

    service = _pty_service(RecordingGate(default=True))
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_relay():
        started.set()
        await release.wait()
        return True

    service.relay_pty_dialog = slow_relay  # type: ignore[method-assign]
    await service.start()
    try:
        import aiohttp

        async with (
            aiohttp.ClientSession() as http,
            http.post(service.base_url + permissions.NOTIFY_PATH, json={}),
        ):
            pass
        await asyncio.wait_for(started.wait(), timeout=2)
        assert len(service._relays) == 1
        release.set()
        await asyncio.sleep(0.05)
        assert service._relays == set()
    finally:
        await service.stop()


async def test_tmux_reports_a_nonzero_exit_rather_than_pretending(monkeypatch):
    """The real _tmux, not the fake: a wrong session name must come back as a failure
    and not as an empty pane that looks like "no dialog"."""
    code, text = await permissions._tmux("has-session", "-t", "cotf-does-not-exist")
    assert code != 0
    assert text == ""


def test_pty_argv_points_at_the_generated_settings_file():
    argv = permissions.pty_argv(permissions.Permissions(mode="ask"))
    assert argv == ["--settings", str(permissions.pty_settings_path())]


# --- forcing the one backend approvals can use ---


def test_pty_env_forces_the_tmux_backend():
    """claude-pty picks tmux only when CLAUDE_PTY_NO_TMUX is not "1". An operator with
    that exported for their own use would otherwise lose approvals on every pty turn,
    and the symptom is a turn that stalls to its timeout rather than an error."""
    assert permissions.pty_env(permissions.Permissions(mode="ask")) == {
        "CLAUDE_PTY_NO_TMUX": "0"
    }


def test_pty_env_is_empty_when_approvals_are_off():
    """The script backend is perfectly fine when nothing is being gated, so this must
    not change how an ungated deployment runs."""
    assert permissions.pty_env(permissions.Permissions()) == {}


async def test_a_missing_pane_is_reported_as_the_script_fallback(monkeypatch, caplog):
    """The two causes need different fixes, so the log has to say which one happened:
    no session at all means claude-pty never used tmux."""
    from claude_on_the_fly.approvals import RecordingGate

    async def no_session(*args: str):
        if args[0] == "has-session":
            return 1, ""
        return 0, "no dialog on this pane"

    monkeypatch.setattr(permissions, "_tmux", no_session)
    monkeypatch.setattr(permissions, "_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(permissions, "_TRANSCRIPT_WAIT_SECONDS", 0.01)
    with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
        assert (
            await _pty_service(RecordingGate(default=True)).relay_pty_dialog() is False
        )
    assert "fell back to its script backend" in caplog.text
    assert "CLAUDE_PTY_NO_TMUX" in caplog.text


async def test_a_live_pane_with_no_dialog_is_reported_differently(monkeypatch, caplog):
    from claude_on_the_fly.approvals import RecordingGate

    async def alive_but_blank(*args: str):
        if args[0] == "has-session":
            return 0, ""
        return 0, "just ordinary output"

    monkeypatch.setattr(permissions, "_tmux", alive_but_blank)
    monkeypatch.setattr(permissions, "_POLL_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(permissions, "_TRANSCRIPT_WAIT_SECONDS", 0.01)
    with caplog.at_level("ERROR", logger="claude_on_the_fly.permissions"):
        assert (
            await _pty_service(RecordingGate(default=True)).relay_pty_dialog() is False
        )
    assert "could not read the permission dialog" in caplog.text
    assert "script backend" not in caplog.text
