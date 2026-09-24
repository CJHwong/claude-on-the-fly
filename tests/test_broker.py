"""Broker behavior: credential injection, caller-auth stripping, route
allowlisting, redirect safety. Each test drives a real in-process upstream over
a real HTTP round-trip; only the keychain read is faked."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web

from claude_on_the_fly import broker
from claude_on_the_fly.approvals import ApprovalBroker, RecordingGate
from claude_on_the_fly.broker import _MAX_BODY_BYTES, Broker, Route, blocked_host


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


def _url(bro: Broker, path: str) -> str:
    """Build an authenticated URL, including the broker's path capability."""
    return f"http://127.0.0.1:{bro.port}/_session/{bro._token}{path}"


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
    await bro.start()
    try:
        async with ClientSession() as client:
            # Agent sends a forged key, as a poisoned file might coach it to.
            resp = await client.post(
                _url(bro, "/anthropic/v1/messages"),
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
    await bro.start()
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/openai/v1/models"))
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
    await bro.start()
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/evil/exfil"))
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
    await bro.start()
    try:
        async with ClientSession() as client:
            resp = await client.get(
                _url(bro, "/anthropic/start"), allow_redirects=False
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
    await bro.start()
    try:
        assert bro.base_url_env() == {"ANTHROPIC_BASE_URL": _url(bro, "/anthropic")}
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
            os.environ["ANTHROPIC_BASE_URL"] == bro.base_url_env()["ANTHROPIC_BASE_URL"]
        )
    finally:
        await bro.stop()
        os.environ.pop("ANTHROPIC_BASE_URL", None)


async def test_start_default_broker_none_without_keychain(monkeypatch):
    monkeypatch.setattr(broker, "keychain_exists", lambda s: False)
    assert await broker.start_default_broker() is None


# --- Slice 1: per-route method / path sub-scoping ---


async def _scoped_broker(fake_keychain, received, approvals=None, **route_kwargs):
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
        ],
        approvals=approvals,
    )
    port = await bro.start()
    return bro, up_runner, port


async def test_scoped_route_allows_listed_method_and_tail(fake_keychain):
    received: list[dict] = []
    bro, up_runner, _port = await _scoped_broker(
        fake_keychain,
        received,
        methods=frozenset({"POST"}),
        allowed_tails=frozenset({"v1/messages"}),
    )
    try:
        async with ClientSession() as client:
            resp = await client.post(_url(bro, "/scoped/v1/messages"), json={"hi": 1})
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
    # The in-scope call reached upstream with the injected key.
    assert received[0]["path"] == "/v1/messages"
    assert received[0]["headers"]["x-api-key"] == "REAL"


async def test_scoped_route_blocks_disallowed_method(fake_keychain):
    received: list[dict] = []
    bro, up_runner, _port = await _scoped_broker(
        fake_keychain, received, methods=frozenset({"POST"})
    )
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/scoped/v1/messages"))
            assert resp.status == 403
    finally:
        await bro.stop()
        await up_runner.cleanup()
    # Fail-closed: the disallowed method never reached upstream.
    assert received == []


async def test_scoped_route_blocks_disallowed_tail(fake_keychain):
    received: list[dict] = []
    bro, up_runner, _port = await _scoped_broker(
        fake_keychain, received, allowed_tails=frozenset({"v1/messages"})
    )
    try:
        async with ClientSession() as client:
            resp = await client.post(_url(bro, "/scoped/v1/admin"))
            assert resp.status == 403
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert received == []


async def test_unscoped_route_allows_any_method_and_tail(fake_keychain):
    # Empty methods/allowed_tails (the default) preserve today's behavior.
    received: list[dict] = []
    bro, up_runner, _port = await _scoped_broker(fake_keychain, received)
    try:
        async with ClientSession() as client:
            for method, tail in (("GET", "anything"), ("DELETE", "v9/wild")):
                resp = await client.request(method, _url(bro, f"/scoped/{tail}"))
                assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert {r["path"] for r in received} == {"/anything", "/v9/wild"}


# --- Runtime approval of a scope miss ---


async def test_scope_miss_reaches_upstream_when_operator_approves(fake_keychain):
    """An approved method widening injects the real credential, same as an
    in-scope call: approval widens policy, it does not bypass the broker."""
    received: list[dict] = []
    gate = RecordingGate(default=True)
    bro, up_runner, _port = await _scoped_broker(
        fake_keychain,
        received,
        approvals=ApprovalBroker(gate),
        methods=frozenset({"POST"}),
    )
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/scoped/v1/messages"))
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert received[0]["headers"]["x-api-key"] == "REAL"
    assert gate.seen[0].kind == "route-scope"
    # The operator is told the route and the method actually observed.
    assert "GET" in gate.seen[0].detail


async def test_scope_miss_still_403s_when_operator_declines(fake_keychain):
    received: list[dict] = []
    gate = RecordingGate(default=False)
    bro, up_runner, _port = await _scoped_broker(
        fake_keychain,
        received,
        approvals=ApprovalBroker(gate),
        methods=frozenset({"POST"}),
    )
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/scoped/v1/messages"))
            assert resp.status == 403
            body = await resp.text()
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert received == []
    # The stale "retrying will not help" is gone: with an approval channel a
    # retry after a grant does succeed, and saying otherwise suppresses the one
    # useful action the agent could take.
    assert "retrying will not help" not in body
    assert "Do not loop on this" in body


async def test_approved_scope_is_cached_for_later_calls(fake_keychain):
    received: list[dict] = []
    gate = RecordingGate(default=True)
    bro, up_runner, _port = await _scoped_broker(
        fake_keychain,
        received,
        approvals=ApprovalBroker(gate),
        allowed_tails=frozenset({"v1/messages"}),
    )
    try:
        async with ClientSession() as client:
            for _ in range(3):
                resp = await client.post(_url(bro, "/scoped/v1/admin"))
                assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert len(received) == 3
    # Asked once, then served from the grant store.
    assert len(gate.seen) == 1


async def test_scope_miss_denies_without_an_approval_channel(fake_keychain):
    """No gate wired in keeps the original deny-only behavior."""
    received: list[dict] = []
    bro, up_runner, _port = await _scoped_broker(
        fake_keychain, received, methods=frozenset({"POST"})
    )
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/scoped/v1/messages"))
            assert resp.status == 403
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert received == []


async def test_in_scope_call_never_asks(fake_keychain):
    received: list[dict] = []
    gate = RecordingGate(default=True)
    bro, up_runner, _port = await _scoped_broker(
        fake_keychain,
        received,
        approvals=ApprovalBroker(gate),
        methods=frozenset({"POST"}),
        allowed_tails=frozenset({"v1/messages"}),
    )
    try:
        async with ClientSession() as client:
            resp = await client.post(_url(bro, "/scoped/v1/messages"))
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert gate.seen == []


async def test_add_route_widens_a_live_broker(fake_keychain):
    received: list[dict] = []
    fake_keychain["cotf-late"] = "LATE-KEY"
    bro, up_runner, _port = await _scoped_broker(fake_keychain, received)
    up2_runner, up2_port = await _start(_echo_app(received))
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/late/v1/thing"))
            assert resp.status == 403
            bro.add_route(
                Route(
                    prefix="/late",
                    upstream=f"http://localhost:{up2_port}",
                    header="x-api-key",
                    keychain_service="cotf-late",
                )
            )
            resp = await client.get(_url(bro, "/late/v1/thing"))
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
        await up2_runner.cleanup()
    assert received[-1]["headers"]["x-api-key"] == "LATE-KEY"


async def test_blocked_host_is_public_and_catches_metadata():
    # egress.py depends on this being importable rather than duplicating the
    # CIDR list, which is how two policy paths drift apart.
    assert blocked_host("169.254.169.254") is True
    assert blocked_host("10.1.2.3") is True
    assert blocked_host("127.0.0.1") is True
    assert blocked_host("8.8.8.8") is False
    assert blocked_host("api.anthropic.com") is False


@pytest.mark.parametrize(
    "address",
    ["::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:10.1.2.3", "0.0.0.0", "::"],
)
async def test_blocked_host_catches_the_other_spellings_of_a_blocked_address(
    address,
):
    """Measured on macOS and Linux: a connect to `::ffff:127.0.0.1` or to
    `0.0.0.0` reaches a listener on 127.0.0.1. Neither is inside a listed range
    as written, so both walked past this check."""
    assert blocked_host(address) is True


# --- diagnostic logging ---


def test_stripped_auth_headers_are_reported(caplog):
    """An agent sending its own credential is either a misconfigured SDK or a key
    an injected payload planted, and both are worth seeing."""
    from claude_on_the_fly.broker import _forward_request_headers

    with caplog.at_level("WARNING", logger="claude_on_the_fly.broker"):
        kept = _forward_request_headers(
            {
                "x-api-key": "sk-planted-by-an-injection",
                "authorization": "Bearer nope",
                "content-type": "application/json",
            }
        )
    assert kept == {"content-type": "application/json"}
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "stripped caller-supplied auth header" in logged
    assert "x-api-key" in logged
    # Names only: the record of a planted key must not republish it.
    assert "sk-planted-by-an-injection" not in logged
    assert "Bearer nope" not in logged


def test_clean_request_logs_no_strip_warning(caplog):
    from claude_on_the_fly.broker import _forward_request_headers

    with caplog.at_level("WARNING", logger="claude_on_the_fly.broker"):
        _forward_request_headers({"content-type": "application/json"})
    assert not [r for r in caplog.records if "stripped" in r.getMessage()]


# --- response body integrity ---


async def test_gzipped_upstream_response_survives_the_broker(monkeypatch):
    """A gzipped upstream response must reach the agent intact.

    aiohttp's ClientSession decompresses by default, and `content-encoding` was
    not in the strip set, so the broker forwarded a decompressed body still
    labelled `Content-Encoding: gzip`. Every client then failed with a zlib
    error. Found with a live `claude` run against the broker: "Decompression
    error: ZlibError". Essentially every real API gzips, so this path was broken
    for all of them.
    """
    import gzip

    from aiohttp import ClientSession, web

    from claude_on_the_fly.broker import Broker, Route

    payload = b'{"ok": true, "content": "' + b"x" * 4096 + b'"}'

    async def upstream(request: web.Request) -> web.Response:
        # Explicitly gzip with the header set, as a real API does.
        return web.Response(
            body=gzip.compress(payload),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    upstream_port = runner.addresses[0][1]

    monkeypatch.setattr(
        "claude_on_the_fly.broker.read_keychain", lambda _service: "real-key"
    )
    broker = Broker(
        [
            Route(
                prefix="/up",
                # A hostname, so the loopback guard (which only inspects IP
                # literals) does not reject the test route.
                upstream=f"http://localhost:{upstream_port}",
                header="x-api-key",
                keychain_service="svc",
            )
        ]
    )
    await broker.start()
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(broker, "/up/v1/thing"))
            assert resp.status == 200
            body = await resp.read()
        assert body == payload, "body did not survive the broker intact"
    finally:
        await broker.stop()
        await runner.cleanup()


# --- request body size ---


async def test_body_larger_than_aiohttps_default_cap_still_reaches_upstream(
    fake_keychain,
):
    """aiohttp's own default is 1 MiB, far below what this proxy carries: a long
    conversation, a pasted file, or an image attachment pushes a single
    /v1/messages POST past it. Left at the default, the agent got a 413 from its
    own credential proxy, indistinguishable from an upstream rejection."""
    fake_keychain["svc"] = "key"
    sizes: list[int] = []

    async def handler(request: web.Request) -> web.Response:
        sizes.append(len(await request.read()))
        return web.json_response({"ok": True})

    app = web.Application(client_max_size=_MAX_BODY_BYTES)
    app.router.add_post("/v1/messages", handler)
    runner, up_port = await _start(app)
    bro = Broker(
        [
            Route(
                prefix="/anthropic",
                upstream=f"http://localhost:{up_port}",
                header="x-api-key",
                keychain_service="svc",
            )
        ]
    )
    await bro.start()
    try:
        async with ClientSession() as client:
            for size in (2 * 1024 * 1024, 8 * 1024 * 1024):
                resp = await client.post(
                    _url(bro, "/anthropic/v1/messages"),
                    data=b"x" * size,
                )
                assert resp.status == 200, await resp.text()
    finally:
        await bro.stop()
        await runner.cleanup()
    assert sizes == [2 * 1024 * 1024, 8 * 1024 * 1024]


def test_body_cap_is_explicit_and_above_the_documented_provider_limit():
    """Explicit rather than unlimited, because `_handle` buffers the body in
    memory before forwarding; above Anthropic's documented 32 MB so a legitimate
    request is never the thing that trips it."""
    assert _MAX_BODY_BYTES > 32 * 1024 * 1024
    assert _MAX_BODY_BYTES > 1024 * 1024, "must beat aiohttp's own default"


# --- keychain access ---


@pytest.fixture
def on_macos(monkeypatch):
    """Pin the platform for the cases that exercise the `security` subprocess.

    Without this they pass on a developer mac and fail on the Linux runner,
    because the guard short-circuits before the patched `subprocess.run`. The
    subprocess path is macOS-only by definition, so saying so is the point.
    """
    monkeypatch.setattr(broker.sys, "platform", "darwin")


def test_a_host_without_a_keychain_reports_no_item_instead_of_crashing(monkeypatch):
    """The `security` binary is macOS-only, so on Linux `subprocess` raised
    FileNotFoundError and took the daemon down at startup, before anything was
    serving -- for every Linux deployment that turned the sandbox on. Observed
    on a real host: `FileNotFoundError: [Errno 2] No such file or directory:
    'security'` out of `routes_from_keychain`."""

    def never(*_a, **_kw):
        raise AssertionError("must not shell out where there is no keychain")

    monkeypatch.setattr(broker.sys, "platform", "linux")
    monkeypatch.setattr(broker.subprocess, "run", never)

    assert broker.has_keychain() is False
    assert broker.keychain_exists("cotf-anthropic") is False
    with pytest.raises(KeyError, match="no keychain on this platform"):
        broker.read_keychain("cotf-anthropic")


def test_routes_are_dropped_rather_than_raising_where_there_is_no_keychain(
    monkeypatch, caplog
):
    """The startup path that actually crashed. It must reach its own "nothing
    provisioned" branch instead of propagating an OSError."""
    monkeypatch.setattr(broker.sys, "platform", "linux")
    monkeypatch.setattr(
        broker.subprocess,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("security")),
    )
    with caplog.at_level("INFO", logger="claude_on_the_fly.broker"):
        assert broker.routes_from_keychain(broker.DEFAULT_ROUTES) == []
    assert "absent" in "\n".join(r.getMessage() for r in caplog.records)


def test_read_keychain_returns_the_value_without_its_trailing_newline(
    monkeypatch, on_macos
):
    """`security` prints a trailing newline, which would travel into the
    Authorization header and be rejected upstream."""
    monkeypatch.setattr(
        broker.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="secret-value\n"),
    )
    assert broker.read_keychain("cotf-anthropic") == "secret-value"


def test_read_keychain_raises_keyerror_when_the_item_is_absent(monkeypatch, on_macos):
    """Loudly at broker start rather than on the agent's first request."""
    monkeypatch.setattr(
        broker.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=44, stdout=""),
    )
    with pytest.raises(KeyError, match="cotf-missing"):
        broker.read_keychain("cotf-missing")


def test_keychain_exists_reports_presence_without_reading_the_value(
    monkeypatch, on_macos
):
    """The probe must not pass `-w`, or a presence check would pull the secret
    into this process for no reason."""
    seen: list[list[str]] = []

    def fake_run(argv, **_kw):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(broker.subprocess, "run", fake_run)
    assert broker.keychain_exists("cotf-anthropic") is True
    assert "-w" not in seen[0]


def test_keychain_exists_is_false_for_a_missing_item(monkeypatch, on_macos):
    monkeypatch.setattr(
        broker.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=44, stdout=""),
    )
    assert broker.keychain_exists("cotf-missing") is False


def test_port_before_start_raises_rather_than_returning_a_placeholder():
    """A zero or None here would be published into ANTHROPIC_BASE_URL and the
    agent would spend its turn talking to nothing."""
    bro = Broker(
        [
            Route(
                prefix="/anthropic",
                upstream="https://api.anthropic.com",
                header="x-api-key",
                keychain_service="svc",
            )
        ]
    )
    with pytest.raises(RuntimeError, match="not started"):
        _ = bro.port


class TestLoopbackAuthentication:
    """The bearer path capability is the only thing separating the agent's own
    requests from anything else that can reach loopback. Every rejection below
    must be a 403 with no upstream call and no credential in play."""

    @pytest.mark.parametrize(
        ("path", "why"),
        [
            pytest.param("/anthropic/v1/messages", "no session prefix", id="no-prefix"),
            pytest.param("/_session/", "no token at all", id="empty"),
            pytest.param(
                "/_session/sometoken", "token but no route tail", id="no-tail"
            ),
            pytest.param("/_session//v1/messages", "empty token", id="blank-token"),
            pytest.param(
                "/_session/wrong-token/v1/messages", "wrong token", id="wrong"
            ),
        ],
    )
    async def test_an_unauthenticated_request_is_refused(
        self, path, why, fake_keychain
    ):
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
        await bro.start()
        try:
            async with ClientSession() as client:
                resp = await client.post(f"http://127.0.0.1:{bro.port}{path}")
                body = await resp.text()
            assert resp.status == 403, why
            assert "authentication required" in body
            assert received == [], "upstream was reached despite a failed auth"
        finally:
            await bro.stop()
            await up_runner.cleanup()


# --- Header sources: a credential read per request, e.g. codex's ChatGPT login ---


class _Source:
    """A header source that rotates to a new token when asked to recover."""

    def __init__(self, *, recovers: bool = True, fails: bool = False) -> None:
        self.token = "first"
        self.recovers = recovers
        self.fails = fails
        self.recovered_from: list[dict] = []

    async def headers(self) -> dict[str, str]:
        if self.fails:
            raise OSError("auth file unreadable")
        return {"Authorization": f"Bearer {self.token}", "ChatGPT-Account-Id": "acct"}

    async def recover(self, sent) -> bool:
        self.recovered_from.append(dict(sent))
        if self.recovers:
            self.token = "second"
        return self.recovers


def _rejecting_app(record: list[dict], bad: str) -> web.Application:
    """Upstream that answers 401 to one bearer token and 200 to any other."""

    async def handler(request: web.Request) -> web.Response:
        record.append({k.lower(): v for k, v in request.headers.items()})
        if request.headers.get("Authorization") == f"Bearer {bad}":
            return web.Response(status=401, text="expired")
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


async def _source_broker(app: web.Application, source: _Source):
    up_runner, up_port = await _start(app)
    bro = Broker(
        [
            Route(
                prefix="/chatgpt",
                upstream=f"http://localhost:{up_port}",
                header="authorization",
                keychain_service="",
                source=source,
            )
        ]
    )
    await bro.start()
    return bro, up_runner


async def test_a_source_route_injects_its_headers_without_a_keychain(fake_keychain):
    received: list[dict] = []
    bro, up_runner = await _source_broker(_echo_app(received), _Source())
    try:
        async with ClientSession() as client:
            resp = await client.post(
                _url(bro, "/chatgpt/backend-api/codex/responses"),
                headers={"chatgpt-account-id": "planted", "authorization": "Bearer x"},
                json={},
            )
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()

    headers = received[0]["headers"]
    assert headers["authorization"] == "Bearer first"
    # The caller's own copy, in another case, does not travel beside the real one.
    assert headers["chatgpt-account-id"] == "acct"
    assert fake_keychain == {}


async def test_a_401_is_retried_once_with_the_recovered_token():
    seen: list[dict] = []
    source = _Source()
    bro, up_runner = await _source_broker(_rejecting_app(seen, "first"), source)
    try:
        async with ClientSession() as client:
            resp = await client.post(_url(bro, "/chatgpt/x"), json={"n": 1})
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()

    assert [h["authorization"] for h in seen] == ["Bearer first", "Bearer second"]
    assert source.recovered_from == [
        {"Authorization": "Bearer first", "ChatGPT-Account-Id": "acct"}
    ]


async def test_a_401_the_source_cannot_recover_is_passed_back_unretried():
    seen: list[dict] = []
    source = _Source(recovers=False)
    bro, up_runner = await _source_broker(_rejecting_app(seen, "first"), source)
    try:
        async with ClientSession() as client:
            resp = await client.post(_url(bro, "/chatgpt/x"), json={})
            assert resp.status == 401
            assert await resp.text() == "expired"
    finally:
        await bro.stop()
        await up_runner.cleanup()

    assert len(seen) == 1


async def test_a_keychain_route_401_is_passed_back_unretried(fake_keychain):
    fake_keychain["cotf-k"] = "first"
    seen: list[dict] = []
    up_runner, up_port = await _start(_rejecting_app(seen, "first"))
    bro = Broker(
        [
            Route(
                prefix="/k",
                upstream=f"http://localhost:{up_port}",
                header="authorization",
                value_prefix="Bearer ",
                keychain_service="cotf-k",
            )
        ]
    )
    await bro.start()
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/k/x"))
            assert resp.status == 401
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert len(seen) == 1


async def test_an_unloadable_source_answers_502_and_names_no_value(caplog):
    seen: list[dict] = []
    bro, up_runner = await _source_broker(_echo_app(seen), _Source(fails=True))
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/chatgpt/x"))
            assert resp.status == 502
            assert "could not load the credential" in await resp.text()
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert seen == []
    assert "cannot load the credential for /chatgpt" in caplog.text


async def test_add_route_with_a_source_reads_no_keychain(fake_keychain):
    fake_keychain["cotf-anthropic"] = "REAL"
    bro = Broker(
        [
            Route(
                prefix="/anthropic",
                upstream="https://api.anthropic.com",
                header="x-api-key",
                keychain_service="cotf-anthropic",
            )
        ]
    )
    bro.add_route(
        Route(
            prefix="/chatgpt",
            upstream="https://chatgpt.com",
            header="authorization",
            keychain_service="",
            source=_Source(),
        )
    )
    assert bro._match("/chatgpt/x").label == "/chatgpt source"
    assert bro._match("/anthropic/x").label == "cotf-anthropic"


def _chatgpt_route() -> Route:
    return Route(
        prefix="/chatgpt",
        upstream="https://chatgpt.com",
        header="authorization",
        keychain_service="",
        base_url_env_var="COTF_CHATGPT_BASE_URL",
        source=_Source(),
    )


async def test_default_broker_without_keychain_serves_only_the_chatgpt_login(
    monkeypatch,
):
    from claude_on_the_fly import codex_auth

    monkeypatch.setattr(broker, "keychain_exists", lambda s: True)
    monkeypatch.setattr(codex_auth, "enabled", lambda: True)
    monkeypatch.setattr(codex_auth, "route", _chatgpt_route)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("COTF_CHATGPT_BASE_URL", raising=False)
    bro = await broker.start_default_broker(keychain=False)
    assert bro is not None
    try:
        assert "COTF_CHATGPT_BASE_URL" in os.environ
        assert "ANTHROPIC_BASE_URL" not in os.environ
    finally:
        await bro.stop()
        os.environ.pop("COTF_CHATGPT_BASE_URL", None)


async def test_default_broker_adds_the_chatgpt_login_beside_keychain_routes(
    monkeypatch,
):
    from claude_on_the_fly import codex_auth

    monkeypatch.setattr(broker, "keychain_exists", lambda s: True)
    monkeypatch.setattr(broker, "read_keychain", lambda s: "REAL")
    monkeypatch.setattr(codex_auth, "enabled", lambda: True)
    monkeypatch.setattr(codex_auth, "route", _chatgpt_route)
    bro = await broker.start_default_broker()
    assert bro is not None
    try:
        assert set(bro.base_url_env()) == {
            "ANTHROPIC_BASE_URL",
            "COTF_CHATGPT_BASE_URL",
        }
    finally:
        await bro.stop()
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("COTF_CHATGPT_BASE_URL", None)


async def test_default_broker_without_keychain_or_chatgpt_starts_nothing(monkeypatch):
    monkeypatch.setattr(broker, "keychain_exists", lambda s: True)
    assert await broker.start_default_broker(keychain=False) is None


async def test_a_caller_hanging_up_mid_stream_is_not_an_error(
    fake_keychain, monkeypatch, caplog
):
    """codex closes a streamed model call before the chunked terminator. The
    answer was delivered, so the broker must not surface it as a handler error."""
    from aiohttp.client_exceptions import ClientConnectionResetError

    fake_keychain["cotf-scoped"] = "REAL"
    received: list[dict] = []
    bro, up_runner, _ = await _scoped_broker(fake_keychain, received)

    original = web.StreamResponse.write_eof

    async def closed(self, data=b""):
        # Only the broker's own streamed response; the fake upstream's
        # json_response is a StreamResponse subclass and must still finish.
        if type(self) is web.StreamResponse:
            raise ClientConnectionResetError("Cannot write to closing transport")
        await original(self, data)

    monkeypatch.setattr(web.StreamResponse, "write_eof", closed)
    caplog.set_level("DEBUG", logger="claude_on_the_fly.broker")
    try:
        async with ClientSession() as client:
            resp = await client.get(_url(bro, "/scoped/x"))
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert "closed by the caller mid-stream" in caplog.text
    assert "Error handling request" not in caplog.text


async def test_a_caller_hanging_up_before_the_headers_is_not_an_error(
    fake_keychain, monkeypatch, caplog
):
    """codex can close a call while the broker still waits on the upstream
    headers, so prepare() is the first write to hit the closed transport."""
    from aiohttp.client_exceptions import (
        ClientConnectionResetError,
        ServerDisconnectedError,
    )

    fake_keychain["cotf-scoped"] = "REAL"
    received: list[dict] = []
    bro, up_runner, _ = await _scoped_broker(fake_keychain, received)

    original = web.StreamResponse.prepare

    async def closed(self, request):
        if type(self) is web.StreamResponse:
            raise ClientConnectionResetError("Cannot write to closing transport")
        return await original(self, request)

    monkeypatch.setattr(web.StreamResponse, "prepare", closed)
    caplog.set_level("DEBUG", logger="claude_on_the_fly.broker")
    try:
        async with ClientSession() as client:
            # aiohttp retries prepare() on the dead transport and drops it.
            with pytest.raises(ServerDisconnectedError):
                await client.get(_url(bro, "/scoped/x"))
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert "closed by the caller before the response started" in caplog.text
    assert "Error handling request" not in caplog.text


async def test_logs_never_carry_the_session_token(fake_keychain, caplog):
    """The token in the path is the loopback capability. The broker's allow
    line and aiohttp's access log both name the path, so both must cut it."""
    fake_keychain["cotf-scoped"] = "REAL"
    received: list[dict] = []
    bro, up_runner, _ = await _scoped_broker(fake_keychain, received)
    caplog.set_level("INFO")
    try:
        async with ClientSession() as client:
            ok = await client.get(_url(bro, "/scoped/x?q=1"))
            assert ok.status == 200
            wrong = f"http://127.0.0.1:{bro.port}/_session/not-the-token/scoped/x"
            assert (await client.get(wrong)).status == 403
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert bro._token not in caplog.text
    assert "not-the-token" not in caplog.text
    allow = [r for r in caplog.records if "broker: allow" in r.getMessage()]
    assert allow and "/scoped/x" in allow[0].getMessage()
    access = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert any('"GET /_session/<redacted>/scoped/x?q=1" 200' in m for m in access)
    assert any('"GET /_session/<redacted>/scoped/x" 403' in m for m in access)


def test_an_expected_auth_header_is_replaced_quietly(caplog):
    """A source route's client may hold its own login. Stripping that is
    routine, while an unexpected auth header on the same request still warns."""
    from claude_on_the_fly.broker import _forward_request_headers

    caplog.set_level("DEBUG", logger="claude_on_the_fly.broker")
    kept = _forward_request_headers(
        {"Authorization": "Bearer own", "x-api-key": "planted", "accept": "*/*"},
        injected={"Authorization": "Bearer real"},
        expected={"Authorization": "Bearer real"},
    )
    assert kept == {"accept": "*/*"}
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        "broker: stripped caller-supplied auth header(s) ['x-api-key'] before forwarding"
    ]
    assert "replaced the caller's own ['Authorization']" in caplog.text


async def test_a_source_route_replaces_the_callers_login_without_warning(caplog):
    received: list[dict] = []
    bro, up_runner = await _source_broker(_echo_app(received), _Source())
    try:
        async with ClientSession() as client:
            resp = await client.get(
                _url(bro, "/chatgpt/x"), headers={"Authorization": "Bearer own"}
            )
            assert resp.status == 200
    finally:
        await bro.stop()
        await up_runner.cleanup()
    assert received[0]["headers"]["authorization"] == "Bearer first"
    assert "stripped caller-supplied" not in caplog.text
