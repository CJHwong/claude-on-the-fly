"""Tests for runtime permission grants.

Every test that matters here is a negative: the gate must deny on timeout, on a
raised exception, on a never-ask subject, and past the rate limit. A gate that
accidentally grants is the whole risk of this feature existing.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_on_the_fly.approvals import (
    ApprovalBroker,
    ApprovalPolicy,
    ApprovalRequest,
    DenyAllGate,
    GrantStore,
    RecordingGate,
    gate_from_frontend,
)


def make_request(subject: str = "example.com:443", **kwargs) -> ApprovalRequest:
    return ApprovalRequest(
        kind="host", subject=subject, detail="observed a tunnel attempt", **kwargs
    )


class FakeClock:
    """Manually advanced monotonic clock so TTL tests don't sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- GrantStore ---


def test_grant_store_denies_unknown_key():
    assert GrantStore().allows("host:example.com:443") is False


def test_grant_store_allows_within_ttl():
    clock = FakeClock()
    store = GrantStore(clock=clock)
    store.grant("host:example.com:443", 60.0)
    clock.advance(59.0)
    assert store.allows("host:example.com:443") is True


def test_grant_store_expires_and_forgets():
    clock = FakeClock()
    store = GrantStore(clock=clock)
    store.grant("host:example.com:443", 60.0)
    clock.advance(60.0)
    assert store.allows("host:example.com:443") is False
    # The expired entry is dropped, not just reported false.
    assert store.active() == []


def test_grant_store_active_lists_only_live_grants():
    clock = FakeClock()
    store = GrantStore(clock=clock)
    store.grant("host:short.example:443", 10.0)
    store.grant("host:long.example:443", 100.0)
    clock.advance(50.0)
    assert store.active() == ["host:long.example:443"]


# --- DenyAllGate ---


async def test_deny_all_gate_never_grants():
    assert await DenyAllGate().request(make_request()) is False


# --- ApprovalBroker happy path ---


async def test_granted_request_is_cached_and_asked_only_once():
    gate = RecordingGate(answers={"example.com:443": True})
    broker = ApprovalBroker(gate)
    assert await broker.check(make_request()) is True
    assert await broker.check(make_request()) is True
    # Second call served from the store, so the operator saw one question.
    assert len(gate.seen) == 1


async def test_denial_suppresses_the_retry_storm():
    """Regression from a live codex run: a denied host was re-asked on every
    retry, producing 50 prompts for one decision."""
    clock = FakeClock()
    gate = RecordingGate(default=False)
    broker = ApprovalBroker(gate, clock=clock)
    for _ in range(20):
        assert await broker.check(make_request()) is False
    assert len(gate.seen) == 1


async def test_denial_expires_so_the_operator_can_change_their_mind():
    clock = FakeClock()
    gate = RecordingGate(default=False)
    broker = ApprovalBroker(
        gate, policy=ApprovalPolicy(deny_cooldown_seconds=60.0), clock=clock
    )
    await broker.check(make_request())
    clock.advance(61.0)
    await broker.check(make_request())
    # The "no" was never permanent, just quiet for a while.
    assert len(gate.seen) == 2


async def test_deny_cooldown_is_per_subject():
    clock = FakeClock()
    gate = RecordingGate(default=False)
    broker = ApprovalBroker(gate, clock=clock)
    await broker.check(make_request("a.example:443"))
    await broker.check(make_request("b.example:443"))
    # Declining one host must not silently decline a different one.
    assert len(gate.seen) == 2


async def test_deny_cooldown_never_becomes_a_grant():
    clock = FakeClock()
    gate = RecordingGate(default=False)
    broker = ApprovalBroker(gate, clock=clock)
    await broker.check(make_request())
    assert broker.allows("host:example.com:443") is False
    assert broker.store.active() == []


async def test_allows_reports_store_state_without_asking():
    gate = RecordingGate(answers={"example.com:443": True})
    broker = ApprovalBroker(gate)
    assert broker.allows("host:example.com:443") is False
    await broker.check(make_request())
    assert broker.allows("host:example.com:443") is True
    assert len(gate.seen) == 1


async def test_grant_expires_and_asks_again():
    clock = FakeClock()
    gate = RecordingGate(answers={"example.com:443": True})
    broker = ApprovalBroker(gate, clock=clock)
    await broker.check(make_request(ttl_seconds=60.0))
    clock.advance(61.0)
    await broker.check(make_request(ttl_seconds=60.0))
    assert len(gate.seen) == 2


# --- Failure modes, all of which must deny ---


async def test_timeout_denies():
    class HangingGate:
        async def request(self, req):
            await asyncio.sleep(10)
            return True

    broker = ApprovalBroker(HangingGate(), timeout_seconds=0.05)
    assert await broker.check(make_request()) is False


async def test_gate_exception_denies():
    class BrokenGate:
        async def request(self, req):
            raise RuntimeError("frontend is down")

    broker = ApprovalBroker(BrokenGate())
    # A frontend outage must not become an accidental grant.
    assert await broker.check(make_request()) is False


async def test_timeout_leaves_no_grant_behind():
    class HangingGate:
        async def request(self, req):
            await asyncio.sleep(10)
            return True

    broker = ApprovalBroker(HangingGate(), timeout_seconds=0.05)
    await broker.check(make_request())
    assert broker.store.active() == []


# --- never-ask tier ---


async def test_never_ask_subject_is_refused_without_asking():
    gate = RecordingGate(default=True)
    broker = ApprovalBroker(
        gate, policy=ApprovalPolicy(never_ask=frozenset({"evil.example:443"}))
    )
    assert await broker.check(make_request("evil.example:443")) is False
    # The operator is never shown the question, so consent fatigue can't grant it.
    assert gate.seen == []


async def test_never_ask_supports_prefix_wildcard():
    gate = RecordingGate(default=True)
    broker = ApprovalBroker(
        gate, policy=ApprovalPolicy(never_ask=frozenset({"metadata.*"}))
    )
    assert await broker.check(make_request("metadata.google.internal:80")) is False
    assert gate.seen == []


async def test_never_ask_does_not_block_other_subjects():
    gate = RecordingGate(answers={"good.example:443": True})
    broker = ApprovalBroker(
        gate, policy=ApprovalPolicy(never_ask=frozenset({"evil.example:443"}))
    )
    assert await broker.check(make_request("good.example:443")) is True


# --- rate limiting ---


async def test_rate_limit_stops_asking_after_threshold():
    gate = RecordingGate(default=False)
    broker = ApprovalBroker(gate, policy=ApprovalPolicy(rate_limit=3))
    for index in range(5):
        await broker.check(make_request(f"host{index}.example:443"))
    # Three questions reached the operator; the burst past that was auto-denied.
    assert len(gate.seen) == 3


async def test_rate_limit_window_rolls_off():
    clock = FakeClock()
    gate = RecordingGate(default=False)
    broker = ApprovalBroker(
        gate, policy=ApprovalPolicy(rate_limit=2, window_seconds=100.0), clock=clock
    )
    await broker.check(make_request("a.example:443"))
    await broker.check(make_request("b.example:443"))
    assert await broker.check(make_request("c.example:443")) is False
    clock.advance(101.0)
    await broker.check(make_request("d.example:443"))
    assert len(gate.seen) == 3


async def test_cached_grant_does_not_consume_rate_budget():
    gate = RecordingGate(answers={"example.com:443": True})
    broker = ApprovalBroker(gate, policy=ApprovalPolicy(rate_limit=1))
    assert await broker.check(make_request()) is True
    # Served from the store, so it must not count against the budget.
    assert await broker.check(make_request()) is True
    assert len(gate.seen) == 1


# --- concurrent duplicate collapsing ---


async def test_concurrent_duplicates_ask_once():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class SlowGate:
        async def request(self, req):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return True

    broker = ApprovalBroker(SlowGate())
    first = asyncio.create_task(broker.check(make_request()))
    await started.wait()
    second = asyncio.create_task(broker.check(make_request()))
    await asyncio.sleep(0)
    release.set()
    assert await first is True
    assert await second is True
    # An agent retrying a blocked host must not multiply the prompts.
    assert calls == 1


# --- frontend adapter ---


async def test_gate_from_frontend_denies_when_method_missing():
    class Bare:
        pass

    gate = gate_from_frontend(Bare())
    assert await gate.request(make_request()) is False


async def test_gate_from_frontend_forwards_to_frontend():
    seen: list[tuple[ApprovalRequest, int | None]] = []

    class Chatty:
        async def ask_approval(self, request, chat_id=None):
            seen.append((request, chat_id))
            return True

    gate = gate_from_frontend(Chatty())
    assert await gate.request(make_request()) is True
    assert seen[0][0].subject == "example.com:443"
    # No session bound, so the frontend falls back to its operator destination.
    assert seen[0][1] is None


async def test_gate_from_frontend_binds_the_session():
    """Per-session gate: the prompt must be routable to the chat that caused it."""
    seen: list[int | None] = []

    class Chatty:
        async def ask_approval(self, request, chat_id=None):
            seen.append(chat_id)
            return True

    gate = gate_from_frontend(Chatty(), chat_id=4242)
    assert await gate.request(make_request()) is True
    assert seen == [4242]


@pytest.mark.parametrize("answer", [True, False])
async def test_gate_from_frontend_passes_verdict_through(answer):
    class Fixed:
        async def ask_approval(self, request, chat_id=None):
            return answer

    assert await gate_from_frontend(Fixed()).request(make_request()) is answer


# --- Slack routing: session thread only, no fallback channel ---


async def test_slack_denies_when_the_session_has_no_thread(monkeypatch):
    """No configured fallback channel by design: an unattended job (cron, the
    job queue) has nobody to ask and must not acquire egress it was never
    granted."""
    from claude_on_the_fly.slack import SlackFrontend

    monkeypatch.setenv("COTF_APPROVAL_CHANNEL", "C-should-be-ignored")
    frontend = SlackFrontend(app_token="xapp-x", token="xoxb-x", user_id="U1")
    assert frontend._approval_target(None) is None
    assert frontend._approval_target(999) is None
    assert await frontend.ask_approval(make_request(), None) is False


async def test_slack_routes_to_the_session_thread():
    from claude_on_the_fly.slack import SlackFrontend

    frontend = SlackFrontend(app_token="xapp-x", token="xoxb-x", user_id="U1")
    frontend._sessions[7] = ("C0FEED", "1700000000.123")
    assert frontend._approval_target(7) == ("C0FEED", "1700000000.123")


async def test_slack_denies_on_a_user_token():
    """Interaction payloads only reach a bot-token install, so a user-token
    deployment would post a prompt nobody can answer."""
    from claude_on_the_fly.slack import SlackFrontend

    frontend = SlackFrontend(app_token="xapp-x", token="xoxp-user", user_id="U1")
    frontend._sessions[7] = ("C0FEED", None)
    assert await frontend.ask_approval(make_request(), 7) is False


# --- diagnostic logging ---


async def test_standing_grant_is_recorded(caplog):
    """Reusing a grant used to log nothing, which left the busiest outcome in a
    long run with no record at all."""
    broker = ApprovalBroker(RecordingGate(default=True))
    req = ApprovalRequest(kind="host", subject="example.com:443", detail="d")
    await broker.check(req)
    with caplog.at_level("DEBUG", logger="claude_on_the_fly.approvals"):
        assert await broker.check(req) is True
    assert any("standing grant" in r.getMessage() for r in caplog.records)


async def test_label_identifies_the_session_store(caplog):
    broker = ApprovalBroker(RecordingGate(default=True), label="chat 7")
    with caplog.at_level("WARNING", logger="claude_on_the_fly.approvals"):
        await broker.check(ApprovalRequest(kind="host", subject="a:443", detail="d"))
    assert any(
        "approval[chat 7]: operator GRANTED" in r.getMessage() for r in caplog.records
    )


async def test_rate_limit_line_shows_the_active_grants(caplog):
    """Diagnosing a rate-limit deny needs to distinguish a probing agent from a
    session that legitimately needed many hosts."""
    broker = ApprovalBroker(
        RecordingGate(default=True), policy=ApprovalPolicy(rate_limit=2)
    )
    for index in range(2):
        await broker.check(
            ApprovalRequest(kind="host", subject=f"h{index}:443", detail="d")
        )
    with caplog.at_level("WARNING", logger="claude_on_the_fly.approvals"):
        assert (
            await broker.check(
                ApprovalRequest(kind="host", subject="h9:443", detail="d")
            )
            is False
        )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "rate limit" in logged
    assert "host:h0:443" in logged


async def test_deny_cooldown_line_reports_remaining_time(caplog):
    broker = ApprovalBroker(
        RecordingGate(default=False), policy=ApprovalPolicy(deny_cooldown_seconds=60)
    )
    req = ApprovalRequest(kind="host", subject="nope:443", detail="d")
    await broker.check(req)
    with caplog.at_level("INFO", logger="claude_on_the_fly.approvals"):
        assert await broker.check(req) is False
    assert any("of cooldown left" in r.getMessage() for r in caplog.records)


async def test_gate_type_is_named_when_asking(caplog):
    """Which gate answered matters: DenyAllGate means no frontend was attached."""
    broker = ApprovalBroker(DenyAllGate())
    with caplog.at_level("INFO", logger="claude_on_the_fly.approvals"):
        await broker.check(ApprovalRequest(kind="host", subject="x:443", detail="d"))
    assert any("via DenyAllGate" in r.getMessage() for r in caplog.records)
