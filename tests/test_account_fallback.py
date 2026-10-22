"""Simulate account exhaustion without contacting a provider."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from claude_on_the_fly import account_fallback, accounts
from claude_on_the_fly.agent import AgentProfile, Response
from claude_on_the_fly.quota import AccountExhaustedError


@pytest.fixture
def pool(monkeypatch):
    registry = {
        name: accounts.Account(name, "codex") for name in ("first", "second", "third")
    }
    monkeypatch.setattr(accounts, "named", lambda name, backend: registry[name])
    monkeypatch.setattr(accounts, "saved_selection", lambda *args: None)
    monkeypatch.setattr(
        accounts,
        "apply_profile",
        lambda profile, account: replace(profile, account=account.name),
    )
    remembered = []
    monkeypatch.setattr(accounts, "remember", lambda *args: remembered.append(args))
    return AgentProfile(
        "codex",
        "native",
        "",
        "low",
        account="first",
        account_pool=("first", "second", "third"),
    ), remembered


async def execute(profile, provider, tmp_path, timeout=30):
    return await account_fallback.run(
        profile, tmp_path, "session", "Do the task", "slack", provider, timeout
    )


async def test_fallback_preserves_durable_progress_without_repeated_tools(
    pool, tmp_path
):
    profile, remembered = pool
    attempts = []
    side_effects = []

    async def provider(candidate, prompt, timeout):
        attempts.append((candidate.account, prompt))
        if candidate.account == "first":
            side_effects.append("created ticket")
            raise AccountExhaustedError(
                "codex", tools_started=True, resume_ready=True, thread_id="native"
            )
        assert prompt != "Do the task"
        assert "Do the task" not in prompt
        if candidate.account == "second":
            raise AccountExhaustedError("codex")
        return Response("Ticket already created")

    response = await execute(profile, provider, tmp_path)
    assert response.body == "Ticket already created"
    assert side_effects == ["created ticket"]
    assert [name for name, _ in attempts] == ["first", "second", "third"]
    assert attempts[1][1] == attempts[2][1]
    assert remembered == [("slack", "session", "codex", "third", profile.account_pool)]


async def test_no_tools_retries_original_request(pool, tmp_path):
    profile, remembered = pool
    provider = AsyncMock(side_effect=[AccountExhaustedError("codex"), Response("done")])
    await execute(profile, provider, tmp_path)
    assert [call.args[1] for call in provider.call_args_list] == ["Do the task"] * 2
    assert remembered[0][3] == "second"


async def test_missing_checkpoint_stops_before_repeated_side_effect(pool, tmp_path):
    profile, remembered = pool
    provider = AsyncMock(side_effect=AccountExhaustedError("codex", tools_started=True))
    with pytest.raises(account_fallback.AccountFallbackError) as failure:
        await execute(profile, provider, tmp_path)
    assert "progress" in failure.value.public_message
    assert provider.await_count == 1
    assert remembered == []


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("rate limit"),
        ValueError("auth"),
        AccountExhaustedError("claude"),
        asyncio.CancelledError(),
    ],
)
async def test_unrelated_failures_and_cancellation_do_not_switch(
    pool, tmp_path, failure
):
    profile, remembered = pool
    provider = AsyncMock(side_effect=failure)
    with pytest.raises(type(failure)):
        await execute(profile, provider, tmp_path)
    assert provider.await_count == 1
    assert remembered == []


async def test_fixed_account_has_no_retry_or_persistence(pool, tmp_path):
    profile, remembered = pool
    profile = replace(profile, account_pool=())
    failure = AccountExhaustedError("codex")
    provider = AsyncMock(side_effect=failure)
    with pytest.raises(AccountExhaustedError):
        await execute(profile, provider, tmp_path)
    provider.assert_awaited_once_with(profile, "Do the task", 30)
    assert remembered == []


async def test_all_exhausted_attempts_each_account_once(pool, tmp_path):
    profile, remembered = pool
    provider = AsyncMock(side_effect=AccountExhaustedError("codex"))
    with pytest.raises(account_fallback.AccountFallbackError) as failure:
        await execute(profile, provider, tmp_path)
    assert [call.args[0].account for call in provider.call_args_list] == list(
        profile.account_pool
    )
    assert all(name in failure.value.public_message for name in profile.account_pool)
    assert remembered == []


async def test_shared_timeout_stops_later_attempt(pool, tmp_path):
    profile, remembered = pool
    timeouts = []

    async def provider(candidate, prompt, timeout):
        timeouts.append(timeout)
        if candidate.account == "first":
            await asyncio.sleep(0.01)
            raise AccountExhaustedError("codex")
        await asyncio.sleep(1)
        return Response("too late")

    with pytest.raises(TimeoutError):
        await execute(profile, provider, tmp_path, timeout=0.05)
    assert len(timeouts) == 2
    assert timeouts[1] < timeouts[0] <= 0.05
    assert remembered == []


async def test_saved_replacement_starts_first_and_rotates_once(
    pool, tmp_path, monkeypatch
):
    profile, remembered = pool
    monkeypatch.setattr(
        accounts,
        "saved_selection",
        lambda *args: accounts.Selection(
            accounts.Account("second", "codex"), profile.account_pool
        ),
    )
    provider = AsyncMock(side_effect=[AccountExhaustedError("codex"), Response("done")])
    await execute(profile, provider, tmp_path)
    assert [call.args[0].account for call in provider.call_args_list] == [
        "second",
        "third",
    ]
    assert remembered[0][3] == "third"


async def test_different_saved_pool_does_not_override_frozen_pool(
    pool, tmp_path, monkeypatch
):
    profile, remembered = pool
    monkeypatch.setattr(
        accounts,
        "saved_selection",
        lambda *args: accounts.Selection(
            accounts.Account("third", "codex"), ("third",)
        ),
    )
    provider = AsyncMock(return_value=Response("done"))
    await execute(profile, provider, tmp_path)
    assert provider.call_args.args[0].account == "first"
    assert remembered == []


async def test_unlimited_deadline_passes_none(pool, tmp_path):
    profile, _ = pool
    provider = AsyncMock(return_value=Response("done"))
    await execute(profile, provider, tmp_path, timeout=None)
    assert provider.call_args.args[2] is None


async def test_expired_deadline_never_starts_provider(pool, tmp_path):
    profile, remembered = pool
    provider = AsyncMock()
    with pytest.raises(TimeoutError):
        await execute(profile, provider, tmp_path, timeout=0)
    provider.assert_not_awaited()
    assert remembered == []


async def test_invalid_current_alias_fails_closed(pool, tmp_path):
    profile, _ = pool
    provider = AsyncMock()
    with pytest.raises(accounts.AccountError, match="outside"):
        await execute(replace(profile, account="missing"), provider, tmp_path)
    provider.assert_not_awaited()
