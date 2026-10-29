"""Capacity recovery keeps the original thread and deadline."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from claude_on_the_fly.backends import codex


def capacity_failure(**fields):
    return codex._CapacityError(
        {"thread_id": "thread-1", "tools_started": False, **fields}
    )


async def invoke(backend, workspace, *, timeout=200):
    return await backend._run_with_capacity_retry(
        workspace,
        "session-1",
        ["codex", "exec", "-m", "selected-model"],
        None,
        "original request",
        timeout,
    )


async def test_capacity_retries_resume_and_keep_usage(tmp_path):
    failure = capacity_failure(
        tools_started=True,
        resume_ready=True,
        tool_counts={"exec": 3},
        usage={"input_tokens": 17},
    )
    success = {
        "thread_id": "thread-1",
        "body": "recovered",
        "usage": {"input_tokens": 5},
        "tool_counts": {"exec": 1},
    }
    with (
        patch.object(
            codex, "_run_codex_exec", AsyncMock(side_effect=[failure, success])
        ) as runner,
        patch.object(codex.asyncio, "sleep", AsyncMock()) as sleep,
    ):
        result = await invoke(codex.CodexBackend(), tmp_path)
    assert result["body"] == "recovered"
    assert result["tool_counts"] == {"exec": 4}
    assert result["usage"] == {"input_tokens": 22}
    sleep.assert_awaited_once_with(10)
    retry = runner.call_args_list[1]
    assert retry.args[1][:-1] == [
        "codex",
        "exec",
        "-m",
        "selected-model",
        "resume",
        "thread-1",
    ]
    assert retry.args[1][-1] == codex.CAPACITY_RESUME_PROMPT
    assert retry.kwargs["thread_id"] == "thread-1"
    assert codex.codex_state.read_thread_id(tmp_path, "session-1") == "thread-1"


async def test_capacity_stops_after_three_retries(tmp_path):
    with (
        patch.object(
            codex, "_run_codex_exec", AsyncMock(side_effect=capacity_failure())
        ) as runner,
        patch.object(codex.asyncio, "sleep", AsyncMock()) as sleep,
        pytest.raises(codex._CapacityError),
    ):
        await invoke(codex.CodexBackend(), tmp_path, timeout=None)
    assert runner.await_count == 4
    assert [call.args[0] for call in sleep.await_args_list] == [10, 30, 60]


@pytest.mark.parametrize("fields", [{"tools_started": True}, {"thread_id": None}])
async def test_capacity_requires_a_thread_and_complete_tool_results(tmp_path, fields):
    with (
        patch.object(
            codex, "_run_codex_exec", AsyncMock(side_effect=capacity_failure(**fields))
        ) as runner,
        patch.object(codex.asyncio, "sleep", AsyncMock()) as sleep,
        pytest.raises(codex._CapacityError),
    ):
        await invoke(codex.CodexBackend(), tmp_path)
    assert runner.await_count == 1
    sleep.assert_not_awaited()


async def test_capacity_preserves_deadline(tmp_path):
    with (
        patch.object(
            codex,
            "_run_codex_exec",
            AsyncMock(side_effect=[capacity_failure(), {"body": "ok"}]),
        ) as runner,
        patch.object(codex.asyncio, "sleep", AsyncMock()),
        patch.object(codex.time, "monotonic", side_effect=[100, 101, 102, 112]),
    ):
        await invoke(codex.CodexBackend(), tmp_path, timeout=50)
    assert runner.call_args_list[0].kwargs["timeout"] == 49
    assert runner.call_args_list[1].kwargs["timeout"] == 38


async def test_capacity_does_not_wait_beyond_deadline(tmp_path):
    with (
        patch.object(
            codex, "_run_codex_exec", AsyncMock(side_effect=capacity_failure())
        ) as runner,
        patch.object(codex.asyncio, "sleep", AsyncMock()) as sleep,
        pytest.raises(codex._CapacityError),
    ):
        await invoke(codex.CodexBackend(), tmp_path, timeout=5)
    assert runner.await_count == 1
    sleep.assert_not_awaited()


async def test_capacity_wait_can_be_cancelled(tmp_path):
    with (
        patch.object(
            codex, "_run_codex_exec", AsyncMock(side_effect=capacity_failure())
        ) as runner,
        patch.object(
            codex.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await invoke(codex.CodexBackend(), tmp_path)
    assert runner.await_count == 1


async def test_unrelated_errors_do_not_retry(tmp_path):
    with (
        patch.object(
            codex, "_run_codex_exec", AsyncMock(side_effect=RuntimeError("unrelated"))
        ) as runner,
        pytest.raises(RuntimeError, match="unrelated"),
    ):
        await invoke(codex.CodexBackend(), tmp_path)
    assert runner.await_count == 1


async def test_capacity_rebuilds_interactive_resume(tmp_path):
    backend = codex.CodexBackend(pty=True, model="selected-model")
    with (
        patch.object(
            codex,
            "_run_codex_exec",
            AsyncMock(side_effect=[capacity_failure(), {"body": "ok"}]),
        ) as runner,
        patch.object(codex.asyncio, "sleep", AsyncMock()),
    ):
        await invoke(backend, tmp_path)
    retry = runner.call_args.kwargs["interactive"]
    assert "resume" in retry
    assert retry[-2:] == ["thread-1", codex.CAPACITY_RESUME_PROMPT]
    assert retry[retry.index("-m") + 1] == "selected-model"


async def test_quota_after_capacity_preserves_completed_work(tmp_path):
    exhausted = codex.quota.AccountExhaustedError("codex", thread_id="thread-1")
    with (
        patch.object(
            codex,
            "_run_codex_exec",
            AsyncMock(
                side_effect=[
                    capacity_failure(
                        tools_started=True, resume_ready=True, tool_counts={"exec": 1}
                    ),
                    exhausted,
                ]
            ),
        ),
        patch.object(codex.asyncio, "sleep", AsyncMock()),
        pytest.raises(codex.quota.AccountExhaustedError) as raised,
    ):
        await invoke(codex.CodexBackend(), tmp_path)
    assert raised.value.tools_started
    assert raised.value.resume_ready


async def test_deadline_expiry_after_wait_does_not_launch_another_process(tmp_path):
    with (
        patch.object(
            codex, "_run_codex_exec", AsyncMock(side_effect=capacity_failure())
        ) as runner,
        patch.object(codex.asyncio, "sleep", AsyncMock()),
        patch.object(codex.time, "monotonic", side_effect=[100, 101, 102, 151]),
        pytest.raises(RuntimeError, match="timed out during capacity recovery"),
    ):
        await invoke(codex.CodexBackend(), tmp_path, timeout=50)
    assert runner.await_count == 1


async def test_backend_delivers_recovered_reply_and_combined_usage(tmp_path):
    failed = capacity_failure(usage={"input_tokens": 17})
    success = {"body": "recovered", "usage": {"input_tokens": 5}}
    with (
        patch.object(
            codex, "_run_codex_exec", AsyncMock(side_effect=[failed, success])
        ),
        patch.object(codex.asyncio, "sleep", AsyncMock()),
        patch.object(codex.pricing, "cost_for", return_value=0),
    ):
        response = await codex.CodexBackend().run(
            tmp_path, "session-1", "hello", "telegram"
        )
    assert response.body == "recovered"
    assert response.tokens_in == 22


async def test_capacity_during_empty_reply_recovery_also_retries(tmp_path):
    with (
        patch.object(
            codex,
            "_run_codex_exec",
            AsyncMock(
                side_effect=[
                    {"thread_id": "thread-1", "body": ""},
                    capacity_failure(),
                    {"body": "recovered"},
                ]
            ),
        ) as runner,
        patch.object(codex.asyncio, "sleep", AsyncMock()) as sleep,
        patch.object(codex.pricing, "cost_for", return_value=0),
    ):
        response = await codex.CodexBackend().run(
            tmp_path, "session-1", "hello", "telegram"
        )
    assert response.body == "recovered"
    assert runner.await_count == 3
    sleep.assert_awaited_once_with(10)
