"""Broker behavior: credential injection, caller-auth stripping, route
allowlisting, redirect safety. Each test drives a real in-process upstream over
a real HTTP round-trip; only the keychain read is faked."""

from __future__ import annotations

import os

import pytest
from aiohttp import ClientSession, web

from claude_on_the_fly import broker
from claude_on_the_fly.broker import Broker, Route


async def _start(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


def _echo_app(record: list[dict]) -> web.Application:
    """Upstream that records every request it receives and returns 200."""

    async def handler(request: web.Request) -> web.Response:
        record.append(
            {
                "method": request.method,
                "path": request.path,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "query": dict(request.query),
                "body": await request.text(),
            }
        )
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


@pytest.fixture
def fake_keychain(monkeypatch):
    secrets: dict[str, str] = {}
    monkeypatch.setattr(broker, "read_keychain", lambda service: secrets[service])
    return secrets


async def test_injects_credential_and_strips_caller_auth(fake_keychain):
    fake_keychain["cotf-anthropic"] = "REAL-INJECTED-KEY"
    received: list[dict] = []
    up_runner, up_port = await _start(_echo_app(received))
    bro = Broker(
        [
            Route(
                prefix="/anthropic",
                upstream=f"http://localhost:{up_port}",
                header="x-api-key",
                keychain_service="cotf-anthropic",
            )
        ]
    )
    port = await bro.start()
    try:
        async with ClientSession() as client:
            # Agent sends a forged key, as a poisoned file might coach it to.
            resp = await client.post(
                f"http://127.0.0.1:{port}/anthropic/v1/messages",
                headers={
                    "x-api-key": "ATTACKER-EMBEDDED",
                    "authorization": "Bearer EVIL",
                },
                json={"hi": "there"},
            )
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()

    assert len(received) == 1
    seen = received[0]
    # Path prefix stripped, request reached the right upstream path.
    assert seen["path"] == "/v1/messages"
    # The real key was injected.
    assert seen["headers"]["x-api-key"] == "REAL-INJECTED-KEY"
    # The forged credentials never reached upstream.
    assert "ATTACKER-EMBEDDED" not in seen["headers"].get("x-api-key", "")
    assert "authorization" not in seen["headers"]
    assert seen["body"] == '{"hi": "there"}'


async def test_bearer_value_prefix(fake_keychain):
    fake_keychain["cotf-openai"] = "sk-REAL"
    received: list[dict] = []
    up_runner, up_port = await _start(_echo_app(received))
    bro = Broker(
        [
            Route(
                prefix="/openai",
                upstream=f"http://localhost:{up_port}",
                header="authorization",
                keychain_service="cotf-openai",
                value_prefix="Bearer ",
            )
        ]
    )
    port = await bro.start()
    try:
        async with ClientSession() as client:
            resp = await client.get(f"http://127.0.0.1:{port}/openai/v1/models")
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()

    assert received[0]["headers"]["authorization"] == "Bearer sk-REAL"


async def test_unknown_route_is_refused(fake_keychain):
    fake_keychain["cotf-anthropic"] = "REAL"
    received: list[dict] = []
    up_runner, up_port = await _start(_echo_app(received))
    bro = Broker(
        [
            Route(
                prefix="/anthropic",
                upstream=f"http://localhost:{up_port}",
                header="x-api-key",
                keychain_service="cotf-anthropic",
            )
        ]
    )
    port = await bro.start()
    try:
        async with ClientSession() as client:
            resp = await client.get(f"http://127.0.0.1:{port}/evil/exfil")
            assert resp.status == 403
    finally:
        await bro.stop()
        await up_runner.cleanup()

    # The agent could reach nothing: upstream never saw the request.
    assert received == []


async def test_does_not_follow_redirects(fake_keychain):
    fake_keychain["cotf-anthropic"] = "REAL"
    redirect_targets: list[dict] = []

    async def redirector(request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": "/secret-elsewhere"})

    async def target(request: web.Request) -> web.Response:
        redirect_targets.append({"headers": dict(request.headers)})
        return web.json_response({"ok": True})

    up = web.Application()
    up.router.add_route("GET", "/start", redirector)
    up.router.add_route("GET", "/secret-elsewhere", target)
    up_runner, up_port = await _start(up)
    bro = Broker(
        [
            Route(
                prefix="/anthropic",
                upstream=f"http://localhost:{up_port}",
                header="x-api-key",
                keychain_service="cotf-anthropic",
            )
        ]
    )
    port = await bro.start()
    try:
        async with ClientSession() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/anthropic/start", allow_redirects=False
            )
            assert resp.status == 302
    finally:
        await bro.stop()
        await up_runner.cleanup()

    # Broker did not follow the redirect, so it never re-injected the key onto
    # the redirected request.
    assert redirect_targets == []


def test_imds_upstream_rejected_at_construction():
    with pytest.raises(ValueError, match="blocked range"):
        Broker(
            [
                Route(
                    prefix="/meta",
                    upstream="http://169.254.169.254/latest/meta-data",
                    header="x-api-key",
                    keychain_service="cotf-x",
                )
            ]
        )


def test_empty_routes_rejected():
    with pytest.raises(ValueError, match="at least one route"):
        Broker([])


async def test_missing_keychain_item_fails_loud(monkeypatch):
    def boom(service: str) -> str:
        raise KeyError(f"keychain item not found: service={service!r}")

    monkeypatch.setattr(broker, "read_keychain", boom)
    bro = Broker(
        [
            Route(
                prefix="/anthropic",
                upstream="https://api.anthropic.com",
                header="x-api-key",
                keychain_service="cotf-absent",
            )
        ]
    )
    with pytest.raises(KeyError, match="cotf-absent"):
        await bro.start()


async def test_base_url_env_after_start(fake_keychain):
    fake_keychain["cotf-x"] = "k"
    bro = Broker(
        [
            Route(
                prefix="/anthropic",
                upstream="https://api.anthropic.com",
                header="x-api-key",
                keychain_service="cotf-x",
                base_url_env_var="ANTHROPIC_BASE_URL",
            )
        ]
    )
    port = await bro.start()
    try:
        assert bro.base_url_env() == {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}/anthropic"
        }
    finally:
        await bro.stop()


def test_routes_from_keychain_filters_absent(monkeypatch):
    present = {"cotf-a"}
    monkeypatch.setattr(broker, "keychain_exists", lambda s: s in present)
    routes = [
        Route("/a", "https://a.example", "x-api-key", "cotf-a"),
        Route("/b", "https://b.example", "x-api-key", "cotf-b"),
    ]
    assert [r.prefix for r in broker.routes_from_keychain(routes)] == ["/a"]


async def test_start_default_broker_publishes_base_url(monkeypatch):
    monkeypatch.setattr(broker, "keychain_exists", lambda s: True)
    monkeypatch.setattr(broker, "read_keychain", lambda s: "REAL")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    bro = await broker.start_default_broker()
    assert bro is not None
    try:
        assert (
            os.environ["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{bro.port}/anthropic"
        )
    finally:
        await bro.stop()
        os.environ.pop("ANTHROPIC_BASE_URL", None)


async def test_start_default_broker_none_without_keychain(monkeypatch):
    monkeypatch.setattr(broker, "keychain_exists", lambda s: False)
    assert await broker.start_default_broker() is None


# --- Slice 1: per-route method / path sub-scoping ---


async def _scoped_broker(fake_keychain, received, **route_kwargs):
    """Start a broker with a single scoped route pointed at an echo upstream."""
    fake_keychain["cotf-scoped"] = "REAL"
    up_runner, up_port = await _start(_echo_app(received))
    bro = Broker(
        [
            Route(
                prefix="/scoped",
                upstream=f"http://localhost:{up_port}",
                header="x-api-key",
                keychain_service="cotf-scoped",
                **route_kwargs,
            )
        ]
    )
    port = await bro.start()
    return bro, up_runner, port


async def test_scoped_route_allows_listed_method_and_tail(fake_keychain):
    received: list[dict] = []
    bro, up_runner, port = await _scoped_broker(
        fake_keychain,
        received,
        methods=frozenset({"POST"}),
        allowed_tails=frozenset({"v1/messages"}),
    )
    try:
        async with ClientSession() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/scoped/v1/messages", json={"hi": 1}
            )
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
    # The in-scope call reached upstream with the injected key.
    assert received[0]["path"] == "/v1/messages"
    assert received[0]["headers"]["x-api-key"] == "REAL"


async def test_scoped_route_blocks_disallowed_method(fake_keychain):
    received: list[dict] = []
    bro, up_runner, port = await _scoped_broker(
        fake_keychain, received, methods=frozenset({"POST"})
    )
    try:
        async with ClientSession() as client:
            resp = await client.get(f"http://127.0.0.1:{port}/scoped/v1/messages")
            assert resp.status == 403
    finally:
        await bro.stop()
        await up_runner.cleanup()
    # Fail-closed: the disallowed method never reached upstream.
    assert received == []


async def test_scoped_route_blocks_disallowed_tail(fake_keychain):
    received: list[dict] = []
    bro, up_runner, port = await _scoped_broker(
        fake_keychain, received, allowed_tails=frozenset({"v1/messages"})
    )
    try:
        async with ClientSession() as client:
            resp = await client.post(f"http://127.0.0.1:{port}/scoped/v1/admin")
            assert resp.status == 403
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert received == []


async def test_unscoped_route_allows_any_method_and_tail(fake_keychain):
    # Empty methods/allowed_tails (the default) preserve today's behavior.
    received: list[dict] = []
    bro, up_runner, port = await _scoped_broker(fake_keychain, received)
    try:
        async with ClientSession() as client:
            for method, tail in (("GET", "anything"), ("DELETE", "v9/wild")):
                resp = await client.request(
                    method, f"http://127.0.0.1:{port}/scoped/{tail}"
                )
                assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert {r["path"] for r in received} == {"/anything", "/v9/wild"}
