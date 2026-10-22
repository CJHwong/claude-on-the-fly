"""Account exhaustion requires a terminal provider failure."""

import json

import pytest

from claude_on_the_fly import quota


@pytest.mark.parametrize(
    "failure",
    [
        {"code": "insufficient_quota"},
        {"error": {"type": "credit_balance_too_low"}},
        {"codex_error_info": "UsageLimitExceeded"},
        "You've hit your usage limit. Please try again tomorrow.",
        "You've hit your limit. Resets at 5pm.",
        "Your credit balance is too low to access the Anthropic API.",
    ],
)
def test_explicit_provider_exhaustion(failure):
    assert quota.provider_exhausted(failure)


@pytest.mark.parametrize(
    "failure",
    [
        "429 Too Many Requests",
        "rate limit exceeded",
        "usage limit",
        "The selected model is at capacity",
        "401 Unauthorized",
        "403 Forbidden",
        "The document says: You've hit your usage limit.",
        {"type": "rate_limit_error", "message": "Try again shortly"},
        {"code": {"unknown": "shape"}},
        None,
    ],
)
def test_other_failures_do_not_change_accounts(failure):
    assert not quota.provider_exhausted(failure)


def test_successful_claude_text_is_not_a_provider_error():
    assert quota.claude_error({"result": "You've hit your limit."}, "", 0, {}) is None
    assert quota.claude_error({"result": "You've hit your limit."}, "", 1, {}) is None


def test_terminal_claude_error_includes_started_tools():
    failure = quota.claude_error(
        {"is_error": True, "errors": ["You've hit your limit."]}, "", 0, {"Bash": 1}
    )
    assert isinstance(failure, quota.AccountExhaustedError)
    assert failure.tools_started
    assert not failure.resume_ready


def test_claude_stderr_requires_failed_exit():
    assert quota.claude_error({}, "You've hit your limit.", 0, {}) is None
    assert quota.claude_error({}, "You've hit your limit.", 1, {}) is not None


@pytest.mark.parametrize("complete", [False, True])
def test_claude_resume_needs_every_durable_tool_result(tmp_path, complete):
    path = tmp_path / "session.jsonl"
    prior = json.dumps({"message": {"content": []}}) + "\n"
    records = [{"message": {"content": [{"type": "tool_use", "id": "call-1"}]}}]
    if complete:
        records.append(
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "call-1"}]}}
        )
    path.write_text(prior + "".join(json.dumps(record) + "\n" for record in records))
    assert quota.claude_resume_ready(path, len(prior)) is complete


def test_missing_or_broken_claude_transcript_fails_closed(tmp_path):
    path = tmp_path / "missing"
    assert not quota.claude_resume_ready(path, 0)
    path.write_text("not json\n")
    assert not quota.claude_resume_ready(path, 0)


async def test_claude_backend_marks_durable_progress_for_resume(tmp_path, monkeypatch):
    from claude_on_the_fly.backends import claude

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude.transcript, "claude_projects_dir", lambda: projects)
    path = projects / claude._workspace_to_claude_hash(workspace) / "test-session.jsonl"
    path.parent.mkdir(parents=True)
    failure = quota.AccountExhaustedError("claude", tools_started=True)

    async def exhausted(*args, **kwargs):
        path.write_text(
            "\n".join(
                json.dumps(record)
                for record in [
                    {"message": {"content": [{"type": "tool_use", "id": "done"}]}},
                    {
                        "message": {
                            "content": [{"type": "tool_result", "tool_use_id": "done"}]
                        }
                    },
                ]
            )
            + "\n"
        )
        raise failure

    monkeypatch.setattr(claude.agent, "_exec", exhausted)
    with pytest.raises(quota.AccountExhaustedError) as raised:
        await claude.ClaudeBackend().run(workspace, "test-session", "hello", "telegram")
    assert raised.value.thread_id == "test-session"
    assert raised.value.resume_ready


@pytest.mark.parametrize(
    "failure",
    [
        '{"error":{"code":"insufficient_quota"}}',
        '[{"code":"usage_limit_reached"}]',
    ],
)
def test_encoded_provider_error_retains_explicit_code(failure):
    assert quota.provider_exhausted(failure)


def test_malformed_encoded_provider_error_does_not_switch():
    assert not quota.provider_exhausted('{"code":"insufficient_quota"')


@pytest.mark.parametrize(
    "record",
    [
        None,
        1,
        "text",
        [],
        {"message": 1},
        {"message": []},
        {"message": {"content": 42}},
        {"message": {"content": ["not a part"]}},
        {"message": {"content": [{"type": {}}]}},
        {"message": {"content": [{"type": []}]}},
        {"message": {"content": [{"type": "tool_use", "id": []}]}},
        {"message": {"content": [{"type": "tool_use", "id": 1}]}},
        {"message": {"content": [{"type": "tool_result", "tool_use_id": {}}]}},
        {"message": {"content": [{"type": "tool_use"}]}},
    ],
)
def test_malformed_record_fails_closed_even_with_other_completed_tools(
    tmp_path, record
):
    path = tmp_path / "session.jsonl"
    records = [
        {"message": {"content": [{"type": "tool_use", "id": "done"}]}},
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "done"}]}},
        record,
    ]
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
    assert not quota.claude_resume_ready(path, 0)


def test_transcript_metadata_and_text_parts_are_valid(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {"type": "system", "subtype": "init"},
                {"type": "user", "message": {"content": "Write the marker"}},
                {"message": {"content": [{"type": "text", "text": "working"}]}},
                {"message": {"content": [{"type": "tool_use", "id": "done"}]}},
                {
                    "message": {
                        "content": [{"type": "tool_result", "tool_use_id": "done"}]
                    }
                },
            ]
        )
        + "\n"
    )
    assert quota.claude_resume_ready(path, 0)


@pytest.mark.parametrize(
    "authentication",
    [
        {"status": 401},
        {"status_code": 403},
        {"type": "authentication_error"},
        {"code": "permission_error"},
        {"code": "invalid_api_key"},
    ],
)
def test_authentication_rejection_wins_over_quota_message(authentication):
    assert not quota.provider_exhausted(
        {**authentication, "message": "You've hit your limit."}
    )


@pytest.mark.parametrize(
    "previous_safe,current_tools,current_safe,expected",
    [
        (True, False, False, True),
        (True, True, True, True),
        (True, True, False, False),
        (False, False, False, False),
    ],
)
async def test_codex_nudge_failure_keeps_previous_tool_progress(
    tmp_path, monkeypatch, previous_safe, current_tools, current_safe, expected
):
    from unittest.mock import AsyncMock

    from claude_on_the_fly.backends import codex

    failure = quota.AccountExhaustedError(
        "codex",
        tools_started=current_tools,
        resume_ready=current_safe,
        thread_id="nudge-thread",
    )
    execute = AsyncMock(
        side_effect=[
            {
                "thread_id": "nudge-thread",
                "body": "",
                "tool_counts": {"exec": 1},
                "tools_started": True,
                "resume_ready": previous_safe,
            },
            failure,
        ]
    )
    monkeypatch.setattr(codex, "_run_codex_exec", execute)
    with pytest.raises(quota.AccountExhaustedError) as raised:
        await codex.CodexBackend().run(tmp_path, "nudge-session", "hello", "telegram")
    assert raised.value.tools_started
    assert raised.value.resume_ready is expected


@pytest.mark.parametrize("complete", [False, True])
async def test_claude_nudge_failure_keeps_previous_tool_progress(
    tmp_path, monkeypatch, complete
):
    from claude_on_the_fly.backends import claude

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude.transcript, "claude_projects_dir", lambda: projects)
    path = (
        projects / claude._workspace_to_claude_hash(workspace) / "nudge-session.jsonl"
    )
    path.parent.mkdir(parents=True)
    invocation = 0

    async def execute(*args, **kwargs):
        nonlocal invocation
        invocation += 1
        if invocation == 2:
            raise quota.AccountExhaustedError("claude")
        records = [{"message": {"content": [{"type": "tool_use", "id": "command"}]}}]
        if complete:
            records.append(
                {
                    "message": {
                        "content": [{"type": "tool_result", "tool_use_id": "command"}]
                    }
                }
            )
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return {"result": "", "tool_counts": {"Bash": 1}}

    monkeypatch.setattr(claude.agent, "_exec", execute)
    with pytest.raises(quota.AccountExhaustedError) as raised:
        await claude.ClaudeBackend().run(
            workspace, "nudge-session", "hello", "telegram"
        )
    assert raised.value.tools_started
    assert raised.value.resume_ready is complete


@pytest.mark.parametrize(
    "envelope,stderr,expected_quota",
    [
        ({"is_error": True, "result": "You've hit your limit."}, b"", True),
        (None, b"connection refused", False),
    ],
)
async def test_pty_terminal_errors_keep_unknown_progress_conservative(
    tmp_path, monkeypatch, envelope, stderr, expected_quota
):
    from unittest.mock import AsyncMock, MagicMock

    from claude_on_the_fly.backends import claude

    process = MagicMock()
    process.returncode = 1
    process.communicate = AsyncMock(
        return_value=(
            json.dumps(envelope).encode() if envelope is not None else b"",
            stderr,
        )
    )
    monkeypatch.setattr(
        claude.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(claude.agent, "_kill_process_tree", AsyncMock())
    with pytest.raises(RuntimeError) as raised:
        await claude._exec_pty(tmp_path, ["claude-pty"])
    assert isinstance(raised.value, quota.AccountExhaustedError) is expected_quota
    if expected_quota:
        assert raised.value.tools_started
        assert not raised.value.resume_ready
