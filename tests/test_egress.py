"""Tests for the CONNECT-gating egress proxy.

The tunnel tests run against a real loopback echo server through a real socket
rather than a mocked stream: the whole point of this module is byte-for-byte
splicing after a policy decision, and a mock would prove only that the policy
branch was taken.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import pytest

from claude_on_the_fly import egress, settings
from claude_on_the_fly.approvals import (
    ApprovalBroker,
    ApprovalPolicy,
    RecordingGate,
)
from claude_on_the_fly.egress import (
    _MAX_REQUEST_LINE,
    EgressProxy,
    default_allowed_hosts,
    default_never_ask,
    is_dns_safe_host,
    never_ask_subjects,
    parse_connect_target,
)

# --- pure helpers ---


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("api.github.com:443", ("api.github.com", 443)),
        ("localhost:8080", ("localhost", 8080)),
        ("host:1", ("host", 1)),
        ("host:65535", ("host", 65535)),
    ],
)
def test_parse_connect_target_accepts_authority_form(target, expected):
    assert parse_connect_target(target) == expected


@pytest.mark.parametrize(
    "target",
    [
        "no-port",  # port is mandatory: defaulting it would widen a port-scoped grant
        "host:",
        ":443",
        "host:0",
        "host:65536",
        "host:notaport",
        "",
    ],
)
def test_parse_connect_target_rejects_malformed(target):
    assert parse_connect_target(target) is None


@pytest.mark.parametrize(
    "host", ["api.github.com", "a", "xn--bcher-kva.example", "1.2.3.4", "A-B.example"]
)
def test_dns_safe_accepts_hostnames(host):
    assert is_dns_safe_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "",
        "evil.com\x00.allowed.com",  # NUL truncates in getaddrinfo, not in Python
        "evil.com%2e.allowed.com",  # percent-decoding differential
        "evil.com\r\nX-Injected: 1",  # CRLF smuggling
        "host with space",
        "[::1]",  # IPv6 literals unsupported, documented
        "host:extra",
        "bücher.example",  # non-punycode IDN
    ],
)
def test_dns_safe_rejects_smuggling_vectors(host):
    assert is_dns_safe_host(host) is False


def test_default_never_ask_covers_metadata_hostnames():
    assert "metadata.google.internal" in default_never_ask()


# --- harness ---


async def start_echo_server() -> tuple[int, asyncio.Server]:
    """Loopback server that echoes back whatever it receives, uppercased.

    Uppercasing makes the assertion prove the bytes made a round trip through
    the tunnel rather than being reflected by the proxy.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.read(1024)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server.sockets[0].getsockname()[1], server


def pin_public_resolution(monkeypatch, echo_port: int) -> None:
    """Make resolution answer with a public address while the connect still
    lands on the local echo server.

    The approval path requires a public address, so a loopback echo server can't
    be reached by name without this. Patching resolution rather than the connect
    keeps the validate-then-pin path under test: the proxy connects to whatever
    address it validated, which here is remapped back to loopback.
    """
    loop = asyncio.get_running_loop()
    real_open_connection = asyncio.open_connection

    async def resolve_public(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))]

    async def open_local(host, port, **kwargs):
        # Only remap the proxy's upstream leg. The test client's own connection
        # to the proxy goes through this same patched function and must not be
        # redirected, or it would never reach the proxy at all.
        if host == "203.0.113.7":
            return await real_open_connection("127.0.0.1", echo_port, **kwargs)
        return await real_open_connection(host, port, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", resolve_public, raising=False)
    monkeypatch.setattr(asyncio, "open_connection", open_local)


async def connect_through(
    proxy_port: int, target: str, payload: bytes = b""
) -> tuple[bytes, bytes]:
    """Speak CONNECT to the proxy. Returns (status_block, tunnel_body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    status = await reader.readuntil(b"\r\n\r\n")
    body = b""
    if payload and status.startswith(b"HTTP/1.1 200"):
        writer.write(payload)
        await writer.drain()
        body = await reader.read(1024)
    elif not status.startswith(b"HTTP/1.1 200"):
        body = await reader.read(4096)
    writer.close()
    return status, body


# --- tunnelling ---


async def test_preapproved_host_tunnels_bytes_end_to_end():
    echo_port, echo = await start_echo_server()
    approvals = ApprovalBroker(RecordingGate(default=False))
    proxy = EgressProxy(approvals, allowed_hosts=frozenset({"127.0.0.1"}))
    port = await proxy.start()
    try:
        status, body = await connect_through(
            port, f"127.0.0.1:{echo_port}", b"hello tunnel"
        )
        assert status.startswith(b"HTTP/1.1 200")
        assert body == b"HELLO TUNNEL"
    finally:
        await proxy.stop()
        echo.close()


async def test_operator_approval_opens_the_tunnel(monkeypatch):
    echo_port, echo = await start_echo_server()
    # A public address is required on the approval path, so stand in for one:
    # resolution says 203.0.113.7 while the connect still lands on the local
    # echo server. This exercises the real pin-and-connect path.
    pin_public_resolution(monkeypatch, echo_port)
    gate = RecordingGate(default=True)
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        status, body = await connect_through(
            port, f"upstream.example:{echo_port}", b"granted"
        )
        assert status.startswith(b"HTTP/1.1 200")
        assert body == b"GRANTED"
        # The operator saw the real destination, including the port.
        assert gate.seen[0].subject == f"upstream.example:{echo_port}"
        assert gate.seen[0].kind == "host"
        # ...and the resolved address, so a name pointing somewhere unexpected
        # is visible at approval time rather than after the fact.
        assert "203.0.113.7" in gate.seen[0].detail
    finally:
        await proxy.stop()
        echo.close()


async def test_second_connection_reuses_the_grant(monkeypatch):
    echo_port, echo = await start_echo_server()
    pin_public_resolution(monkeypatch, echo_port)
    gate = RecordingGate(default=True)
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        await connect_through(port, f"upstream.example:{echo_port}", b"one")
        status, body = await connect_through(
            port, f"upstream.example:{echo_port}", b"two"
        )
        assert status.startswith(b"HTTP/1.1 200")
        assert body == b"TWO"
        assert len(gate.seen) == 1
    finally:
        await proxy.stop()
        echo.close()


async def test_hostname_resolving_to_loopback_is_refused_without_asking(monkeypatch):
    """The DNS-rebinding guard: blocked_host only sees IP literals, so a name
    pointing into private space must be caught at resolution instead."""
    gate = RecordingGate(default=True)

    async def resolve_to_loopback(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", resolve_to_loopback, raising=False
    )
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        status, _ = await connect_through(port, "sneaky.example:443")
        assert status.startswith(b"HTTP/1.1 403")
        # Never offered to the operator, so no amount of consent fatigue grants it.
        assert gate.seen == []
    finally:
        await proxy.stop()


async def test_mixed_public_and_private_resolution_fails_closed(monkeypatch):
    gate = RecordingGate(default=True)

    async def resolve_mixed(host, port, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
        ]

    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", resolve_mixed, raising=False
    )
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        status, _ = await connect_through(port, "mixed.example:443")
        assert status.startswith(b"HTTP/1.1 403")
        assert gate.seen == []
    finally:
        await proxy.stop()


async def test_dns_failure_is_refused_without_asking(monkeypatch):
    gate = RecordingGate(default=True)

    async def resolve_fails(host, port, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", resolve_fails, raising=False
    )
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        status, _ = await connect_through(port, "nonexistent.example:443")
        assert status.startswith(b"HTTP/1.1 403")
        assert gate.seen == []
    finally:
        await proxy.stop()


async def test_preapproved_host_skips_the_private_address_check():
    """An operator naming localhost in `egress.allow` means it: that is a
    config act, not something the agent can induce."""
    echo_port, echo = await start_echo_server()
    gate = RecordingGate(default=False)
    proxy = EgressProxy(ApprovalBroker(gate), allowed_hosts=frozenset({"localhost"}))
    port = await proxy.start()
    try:
        status, body = await connect_through(
            port, f"localhost:{echo_port}", b"dev server"
        )
        assert status.startswith(b"HTTP/1.1 200")
        assert body == b"DEV SERVER"
        assert gate.seen == []
    finally:
        await proxy.stop()
        echo.close()


# --- denials ---


async def test_denied_host_gets_403_with_actionable_body():
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=False)))
    port = await proxy.start()
    try:
        status, body = await connect_through(port, "example.com:443")
        assert status.startswith(b"HTTP/1.1 403")
        assert b"egress policy" in body
        # The agent is told not to loop and to escalate to the human instead.
        assert b"tell the user" in body.lower()
    finally:
        await proxy.stop()


async def test_never_ask_host_is_refused_without_asking():
    gate = RecordingGate(default=True)
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        status, _ = await connect_through(port, "metadata.google.internal:80")
        assert status.startswith(b"HTTP/1.1 403")
        assert gate.seen == []
    finally:
        await proxy.stop()


async def test_private_ip_literal_is_refused_without_asking():
    gate = RecordingGate(default=True)
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        # Cloud metadata lives in link-local; approving it must not be offered.
        status, _ = await connect_through(port, "169.254.169.254:80")
        assert status.startswith(b"HTTP/1.1 403")
        assert gate.seen == []
    finally:
        await proxy.stop()


async def test_non_dns_safe_host_is_refused_without_asking():
    gate = RecordingGate(default=True)
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        status, _ = await connect_through(port, "evil.com%2e.ok.com:443")
        assert status.startswith(b"HTTP/1.1 403")
        assert gate.seen == []
    finally:
        await proxy.stop()


async def test_plain_http_is_rejected_with_405():
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=True)))
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
        await writer.drain()
        status = await reader.readuntil(b"\r\n\r\n")
        body = await reader.read(4096)
        writer.close()
        assert status.startswith(b"HTTP/1.1 405")
        assert b"https://" in body
    finally:
        await proxy.stop()


async def test_malformed_connect_target_gets_400():
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=True)))
    port = await proxy.start()
    try:
        status, _ = await connect_through(port, "no-port-here")
        assert status.startswith(b"HTTP/1.1 400")
    finally:
        await proxy.stop()


async def test_unreachable_upstream_gets_502_after_approval():
    # Port 1 on loopback has nothing listening. Pre-approve the host so the
    # failure is attributable to the connect, not to policy.
    proxy = EgressProxy(
        ApprovalBroker(RecordingGate(default=True)),
        allowed_hosts=frozenset({"127.0.0.1"}),
    )
    port = await proxy.start()
    try:
        status, _ = await connect_through(port, "127.0.0.1:1")
        assert status.startswith(b"HTTP/1.1 502")
    finally:
        await proxy.stop()


async def test_rate_limited_burst_stops_reaching_the_operator(monkeypatch):
    gate = RecordingGate(default=False)

    # Hosts must resolve publicly to reach the gate at all: an unresolvable name
    # is refused before the operator is bothered, which is deliberate but would
    # otherwise mask what this test is checking.
    async def resolve_public(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))]

    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", resolve_public, raising=False
    )
    approvals = ApprovalBroker(gate, policy=ApprovalPolicy(rate_limit=2))
    proxy = EgressProxy(approvals)
    port = await proxy.start()
    try:
        for index in range(5):
            await connect_through(port, f"host{index}.example:443")
        assert len(gate.seen) == 2
    finally:
        await proxy.stop()


async def test_unresolvable_host_never_reaches_the_operator(monkeypatch):
    """Resolution runs before the question, so a typo does not become a prompt."""
    gate = RecordingGate(default=True)
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        status, _ = await connect_through(port, "nx.invalid:443")
        assert status.startswith(b"HTTP/1.1 403")
        assert gate.seen == []
    finally:
        await proxy.stop()


# --- wiring ---


async def test_proxy_env_points_every_client_at_the_proxy():
    proxy = EgressProxy(ApprovalBroker(RecordingGate()))
    port = await proxy.start()
    try:
        env = proxy.proxy_env()
        assert env["HTTPS_PROXY"] == f"http://127.0.0.1:{port}"
        # Lower-case variants matter: curl and requests read those.
        assert env["https_proxy"] == env["HTTPS_PROXY"]
        assert env["HTTP_PROXY"] == env["HTTPS_PROXY"]
    finally:
        await proxy.stop()


async def test_proxy_env_exempts_loopback():
    """Regression from a live codex run: codex polls its own app-server over
    plain HTTP on loopback. Routing that through here 405s it and the agent
    never starts. Which loopback ports are reachable is the seatbelt's call."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate()))
    await proxy.start()
    try:
        env = proxy.proxy_env()
        for value in (env["NO_PROXY"], env["no_proxy"]):
            assert "127.0.0.1" in value
            assert "localhost" in value
    finally:
        await proxy.stop()


def test_port_before_start_raises():
    proxy = EgressProxy(ApprovalBroker(RecordingGate()))
    with pytest.raises(RuntimeError, match="not started"):
        _ = proxy.port


async def test_stop_is_idempotent():
    proxy = EgressProxy(ApprovalBroker(RecordingGate()))
    await proxy.start()
    await proxy.stop()
    await proxy.stop()


async def test_stop_does_not_hang_on_an_open_tunnel():
    """Regression: a tunnel blocks until both directions EOF, so an upstream
    that stays quiet never finishes. Joining handlers on shutdown would hang the
    daemon on Ctrl+C with a connection open."""

    async def silent(reader, writer):
        # Accept, then never send and never close.
        await asyncio.sleep(3600)

    quiet = await asyncio.start_server(silent, "127.0.0.1", 0)
    quiet_port = quiet.sockets[0].getsockname()[1]
    proxy = EgressProxy(
        ApprovalBroker(RecordingGate()), allowed_hosts=frozenset({"127.0.0.1"})
    )
    port = await proxy.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT 127.0.0.1:{quiet_port} HTTP/1.1\r\n\r\n".encode())
    await writer.drain()
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    try:
        await asyncio.wait_for(proxy.stop(), timeout=5.0)
    finally:
        writer.close()
        quiet.close()


# --- per-session isolation ---


async def test_grants_do_not_leak_between_sessions(monkeypatch):
    """Each session has its own grant store, so approving a host in one chat
    must not silently authorize it for another chat or for cron."""
    from claude_on_the_fly.approvals import ApprovalRequest

    def make_proxy():
        gate = RecordingGate(default=True)
        return gate, EgressProxy(ApprovalBroker(gate))

    gate_a, proxy_a = make_proxy()
    gate_b, proxy_b = make_proxy()

    async def resolve_public(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))]

    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", resolve_public, raising=False
    )
    await proxy_a.start()
    await proxy_b.start()
    try:
        # Session A approves the host.
        req = ApprovalRequest(kind="host", subject="shared.example:443", detail="x")
        assert await proxy_a._approvals.check(req) is True
        assert await proxy_a._approvals.check(req) is True
        assert len(gate_a.seen) == 1

        # Session B has never been asked, so it must ask for itself.
        assert proxy_b._approvals.allows("host:shared.example:443") is False
        assert await proxy_b._approvals.check(req) is True
        assert len(gate_b.seen) == 1
    finally:
        await proxy_a.stop()
        await proxy_b.stop()


async def test_sessions_get_distinct_ports():
    """The port is the only label a CONNECT carries, so it is what attributes a
    request to a session."""
    proxy_a = EgressProxy(ApprovalBroker(RecordingGate()))
    proxy_b = EgressProxy(ApprovalBroker(RecordingGate()))
    port_a = await proxy_a.start()
    port_b = await proxy_b.start()
    try:
        assert port_a != port_b
        assert proxy_a.proxy_env()["HTTPS_PROXY"] != proxy_b.proxy_env()["HTTPS_PROXY"]
    finally:
        await proxy_a.stop()
        await proxy_b.stop()


# --- built-in model-API allowlist ---


def test_default_allowed_hosts_cover_every_backend_model_api():
    # Without these the agent cannot complete a turn, so gating them would stop
    # every fresh deployment on its own first LLM call.
    for host in (
        "api.anthropic.com",
        "api.openai.com",
        "chatgpt.com",
        "ab.chatgpt.com",
    ):
        assert host in default_allowed_hosts()


def test_defaults_exclude_hosts_that_are_real_decisions():
    # A package registry grants arbitrary code execution via install; github.com
    # grants writes; telemetry is optional by definition. Operator's call.
    for host in (
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "github.com",
        "api.github.com",
        "downloads.claude.ai",
    ):
        assert host not in default_allowed_hosts()


async def test_model_api_tunnels_without_asking(monkeypatch):
    echo_port, echo = await start_echo_server()
    gate = RecordingGate(default=False)
    real_open = asyncio.open_connection

    async def open_local(host, port, **kwargs):
        # Remap on the hostname, not on a pinned IP: an allowlisted host is
        # connected to by name and never goes through resolve-and-pin, which is
        # exactly the behavior under test.
        if host == "api.anthropic.com":
            return await real_open("127.0.0.1", echo_port, **kwargs)
        return await real_open(host, port, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", open_local)
    # No allowed_hosts passed at all: the default set must still let it through.
    proxy = EgressProxy(ApprovalBroker(gate))
    port = await proxy.start()
    try:
        status, body = await connect_through(port, "api.anthropic.com:443", b"ping")
        assert status.startswith(b"HTTP/1.1 200")
        assert body == b"PING"
        assert gate.seen == []
    finally:
        await proxy.stop()
        echo.close()


async def test_operator_hosts_are_unioned_with_the_defaults():
    proxy = EgressProxy(
        ApprovalBroker(RecordingGate()), allowed_hosts=frozenset({"pypi.org"})
    )
    assert "pypi.org" in proxy._allowed
    assert "api.anthropic.com" in proxy._allowed


# --- host lists come from the settings file ---


def test_operator_file_adds_an_allowed_host(operator_settings):
    operator_settings.write_text("egress:\n  allow:\n    - pypi.org  # uv installs\n")
    hosts = default_allowed_hosts()
    assert "pypi.org" in hosts
    # Bundled entries survive: adding a host must not cost you the model APIs.
    assert "api.anthropic.com" in hosts


def test_operator_file_adds_a_never_ask_host(operator_settings):
    operator_settings.write_text("egress:\n  never_ask:\n    - internal.corp.example\n")
    assert "internal.corp.example" in default_never_ask()


def test_the_operator_cannot_remove_a_bundled_never_ask_host(operator_settings):
    """The bundled entries are the cloud metadata endpoints, which hand instance
    credentials to anything that reaches them. The merge is a union on purpose, so
    no config edit can re-open one."""
    operator_settings.write_text("egress:\n  never_ask:\n    - only.this.example\n")
    assert "metadata.google.internal" in default_never_ask()


def test_hosts_are_lowercased_and_stripped(operator_settings):
    operator_settings.write_text('egress:\n  allow:\n    - "  PyPI.ORG  "\n')
    assert "pypi.org" in default_allowed_hosts()


@pytest.mark.parametrize(
    "entry",
    [
        "https://pypi.org",  # a URL, not a hostname
        "pypi.org:443",  # port belongs in the CONNECT target, not here
        "pypi org",  # whitespace
        "",  # empty
    ],
)
def test_a_bad_host_is_rejected_at_load_not_at_connect_time(operator_settings, entry):
    """A silently dead entry would surface months later as an approval prompt for
    a host the operator believes they already allowed."""
    operator_settings.write_text(f'egress:\n  allow:\n    - "{entry}"\n')
    with pytest.raises(ValueError, match="is not a hostname"):
        egress.parse_hosts({"allow": [entry]}, "allow", source=str(operator_settings))
    # And the loader falls back rather than propagating.
    assert "api.anthropic.com" in default_allowed_hosts()


def test_a_malformed_egress_section_falls_back_to_bundled(operator_settings, caplog):
    operator_settings.write_text('egress:\n  allow: "not-a-list"\n')
    with caplog.at_level("ERROR", logger="claude_on_the_fly.egress"):
        hosts = default_allowed_hosts()
    assert hosts == egress.parse_hosts(
        settings.bundled("egress"), "allow", source="bundled"
    )
    assert "bundled hosts only" in caplog.text


def test_parse_hosts_rejects_a_section_that_is_not_a_mapping():
    with pytest.raises(ValueError, match="must be a mapping"):
        egress.parse_hosts([], "allow", source="test.yaml")


def test_a_missing_key_is_an_empty_set_not_an_error():
    assert egress.parse_hosts({}, "allow", source="test.yaml") == frozenset()


def test_a_caller_can_still_front_load_hosts(operator_settings):
    """`allowed_hosts` is the constructor's injection seam, unioned with the file.
    Nothing in production passes it now that the env var is gone; the tunnel tests
    are its users, and it is how any future caller would front-load a host."""
    operator_settings.write_text("egress:\n  allow:\n    - from-file.example\n")
    proxy = EgressProxy(
        ApprovalBroker(RecordingGate(default=False)),
        allowed_hosts=frozenset({"From-Caller.Example"}),
    )
    assert {
        "from-file.example",
        "from-caller.example",
        "api.anthropic.com",
    } <= proxy._allowed


def test_an_explicit_never_ask_argument_still_wins(operator_settings):
    """The parameter is what lets a caller wire a proxy with a narrower policy;
    resolving from the file must only be the default."""
    proxy = EgressProxy(
        ApprovalBroker(RecordingGate(default=False)),
        never_ask=frozenset({"Only.This.Example"}),
    )
    assert proxy._never_ask == frozenset({"only.this.example"})


# --- diagnostic logging ---


async def test_allow_line_names_why_preapproved(caplog):
    """ "allow" alone cannot be reviewed: pre-approved, standing grant, and a
    fresh operator decision are three different facts about a run."""
    gate = RecordingGate(default=False)
    broker = ApprovalBroker(gate)
    proxy = EgressProxy(broker, allowed_hosts=frozenset({"example.com"}))
    decision = await proxy._permitted("example.com", 443)
    assert decision.address == "example.com"
    assert decision.because == "pre-approved host"
    # Never reached the operator, so no prompt was raised for it.
    assert gate.seen == []


async def test_allow_line_distinguishes_fresh_grant_from_standing_one(monkeypatch):
    # Resolution is pinned. Both calls used to hit real DNS and the test asserted
    # they agreed, which is a property of the resolver rather than of this code:
    # example.com sits behind a round-robin, so CI got two different addresses.
    loop = asyncio.get_running_loop()

    async def resolve_fixed(_host, port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))]

    monkeypatch.setattr(loop, "getaddrinfo", resolve_fixed, raising=False)
    gate = RecordingGate(answers={"example.com:443": True})
    broker = ApprovalBroker(gate)
    proxy = EgressProxy(broker)
    first = await proxy._permitted("example.com", 443)
    assert first.because == "operator approved"
    second = await proxy._permitted("example.com", 443)
    # Second time the store answered, so no human was in the loop.
    assert second.because == "standing grant"
    assert second.address == first.address
    assert len(gate.seen) == 1


async def test_refusal_reasons_are_distinct():
    gate = RecordingGate(default=False)
    proxy = EgressProxy(ApprovalBroker(gate))
    assert (await proxy._permitted("bad_host!", 443)).because == "host is not DNS-safe"
    assert (
        await proxy._permitted("metadata.google.internal", 80)
    ).because == "never-ask policy"
    declined = await proxy._permitted("example.com", 443)
    assert declined.address is None
    assert declined.because == "operator declined or gate denied"


# --- what the agent is told, per cause ---


async def test_only_an_operator_decline_says_an_operator_declined():
    """The bug this pins: every 403 used to claim a human had said no, including
    for hosts no human is ever offered. An agent told that will reasonably retry
    or look for another route, which is what these messages exist to prevent."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=False)))
    declined = await proxy._permitted("example.com", 443)
    assert "an operator declined it" in declined.refusal.hint

    for host, port in (("bad_host!", 443), ("metadata.google.internal", 80)):
        other = await proxy._permitted(host, port)
        assert "operator declined" not in other.refusal.hint, host
        assert "operator was asked and did not approve" not in other.refusal.body, host


async def test_a_never_ask_host_is_told_it_can_never_be_approved():
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=True)))
    decision = await proxy._permitted("metadata.google.internal", 80)
    assert "cannot be approved" in decision.refusal.hint
    assert "do not look for another route" in decision.refusal.body


async def test_a_malformed_host_is_told_it_is_a_request_problem():
    """Distinct action: fix the URL, not report a policy block to the user."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=True)))
    decision = await proxy._permitted("bad_host!", 443)
    assert "not a valid hostname" in decision.refusal.hint
    assert "problem with the request" in decision.refusal.body


async def test_an_unresolvable_host_is_told_retrying_will_not_help(monkeypatch):
    loop = asyncio.get_running_loop()

    async def fail(*_a, **_kw):
        raise OSError("nodename nor servname provided")

    monkeypatch.setattr(loop, "getaddrinfo", fail, raising=False)
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=True)))
    decision = await proxy._permitted("nowhere.invalid", 443)
    assert decision.because == "no usable public address"
    assert "retrying will not help" in decision.refusal.hint
    assert "No operator was asked" in decision.refusal.body


async def test_the_cause_reaches_the_client_in_the_reason_phrase():
    """The load-bearing one. No client surfaces a CONNECT response body -- curl,
    httpx and urllib all report the status line and discard the rest -- so the
    reason phrase is the only channel to the agent. If this regresses, every
    refusal becomes a bare "403 Forbidden" again."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=True)))
    port = await proxy.start()
    try:
        status, body = await connect_through(port, "metadata.google.internal:80")
        assert status.startswith(b"HTTP/1.1 403 Forbidden by egress policy:")
        assert b"cannot be approved" in status.split(b"\r\n")[0]
        # The body still carries the long form for anything that does read it.
        assert b"do not look for another route" in body
    finally:
        await proxy.stop()


@pytest.mark.parametrize(
    "refusal",
    [
        egress._DENIED,
        egress._NEVER_ASK,
        egress._MALFORMED_HOST,
        egress._NO_PUBLIC_ADDRESS,
    ],
)
def test_every_hint_is_a_legal_reason_phrase(refusal):
    """A CR or LF here would be header injection, and a non-ASCII byte is obs-text
    that clients are free to mangle. Also checks the two things the agent's own
    guidance keys off: the phrase opens with the status word and carries the tag."""
    assert "\r" not in refusal.hint and "\n" not in refusal.hint
    assert refusal.hint.isascii()
    assert refusal.hint.startswith("Forbidden")
    assert "egress policy" in refusal.hint
    # Verified: 204 chars arrived intact on curl, httpx and urllib. Well inside it.
    assert len(refusal.hint) < 120


def test_a_refusal_without_a_message_is_a_programming_error():
    """A bare 403 is the same "no information" failure in a new shape, so a new
    refusal site cannot forget it."""
    with pytest.raises(ValueError, match="must carry what the agent is shown"):
        egress._Decision(None, "some new reason")


def test_an_allow_needs_no_refusal():
    assert (
        egress._Decision("203.0.113.7", "pre-approved host").refusal is egress._ALLOWED
    )


async def test_label_tags_every_line(caplog):
    """Proxies are per-session; without the label two chats reaching the same
    host are indistinguishable in the log."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate()), label="chat 42")
    with caplog.at_level("WARNING", logger="claude_on_the_fly.egress"):
        await proxy._permitted("metadata.google.internal", 80)
    assert any("egress[chat 42]" in r.getMessage() for r in caplog.records)


async def test_unlabelled_proxy_keeps_the_plain_prefix(caplog):
    proxy = EgressProxy(ApprovalBroker(RecordingGate()))
    with caplog.at_level("WARNING", logger="claude_on_the_fly.egress"):
        await proxy._permitted("metadata.google.internal", 80)
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("egress: refuse") for m in messages)


async def test_resolved_addresses_are_logged_for_rebinding_review(caplog):
    proxy = EgressProxy(ApprovalBroker(RecordingGate()))
    with caplog.at_level("DEBUG", logger="claude_on_the_fly.egress"):
        await proxy._resolve_public("localhost", 443)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "resolved to" in logged


# --- request-line length ---


async def test_over_long_request_line_gets_414_not_a_dropped_connection():
    """A StreamReader raises once past its buffer limit rather than returning, so
    the `len(request_line) > _MAX_REQUEST_LINE` check below it was unreachable at
    the sizes that matter: the ValueError surfaced as an unhandled exception and
    the client got no response at all."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=False)))
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT " + b"a" * 200_000 + b":443 HTTP/1.1\r\n\r\n")
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        status = await asyncio.wait_for(reader.read(64), timeout=5)
        writer.close()
    finally:
        await proxy.stop()
    assert status.startswith(b"HTTP/1.1 414"), status


async def test_request_line_just_over_the_limit_also_gets_414():
    """The in-band check still has to work for a line long enough to matter but
    short enough that the reader returns it normally."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=False)))
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"CONNECT " + b"a" * (_MAX_REQUEST_LINE + 10) + b":443 HTTP/1.1\r\n"
        )
        await writer.drain()
        status = await asyncio.wait_for(reader.read(64), timeout=5)
        writer.close()
    finally:
        await proxy.stop()
    assert status.startswith(b"HTTP/1.1 414"), status


# --- never-ask, as a policy subject ---


def test_never_ask_subjects_match_the_subject_form_a_requester_actually_sends():
    """An ApprovalRequest subject for a host is "<host>:<port>", so the bare
    hostname set matched nothing and the policy tier was silently dead."""
    policy = ApprovalPolicy(never_ask=never_ask_subjects())
    for host in default_never_ask():
        assert policy.refuses(f"{host}:443"), host
        assert policy.refuses(f"{host}:80"), host


def test_bare_hostnames_are_not_a_working_never_ask_set():
    """Pins the bug this replaced, so nobody wires the raw set back in."""
    policy = ApprovalPolicy(never_ask=default_never_ask())
    assert policy.refuses("metadata.google.internal:443") is False


def test_never_ask_subjects_do_not_overmatch_a_neighbouring_host():
    policy = ApprovalPolicy(never_ask=never_ask_subjects())
    assert policy.refuses("metadata.google.internal.evil.com:443") is False


# --- malformed and truncated requests ---


async def test_client_that_opens_and_closes_without_sending_gets_no_response():
    """A health-check style probe (connect, close) must not log or reply."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=False)))
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write_eof()
        await writer.drain()
        assert await asyncio.wait_for(reader.read(64), timeout=5) == b""
        writer.close()
    finally:
        await proxy.stop()


async def test_single_token_request_line_gets_400():
    """`parts < 2` is a different shape from a malformed CONNECT target: there is
    no target to report at all."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=False)))
    port = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.read(128), timeout=5)
        writer.close()
    finally:
        await proxy.stop()
    assert status.startswith(b"HTTP/1.1 400"), status
    assert b"malformed request line" in status


async def test_unexpected_handler_failure_is_logged_not_swallowed(monkeypatch, caplog):
    """`_handle`'s catch-all exists so one bad connection cannot take the proxy
    down, but a silent one would make the cause unfindable."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=False)))

    async def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(proxy, "_serve", explode)
    port = await proxy.start()
    try:
        with caplog.at_level("ERROR", logger="claude_on_the_fly.egress"):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            # The handler writes nothing and closes, so the read is as likely to
            # reset as it is to return empty. Either way the log line is the point.
            with contextlib.suppress(ConnectionError):
                await asyncio.wait_for(reader.read(64), timeout=5)
            writer.close()
            await asyncio.sleep(0.05)
    finally:
        await proxy.stop()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "connection handler failed" in logged


async def test_resolution_returning_nothing_is_refused_with_a_reason(
    monkeypatch, caplog
):
    """An empty getaddrinfo is not the same as a resolution error, and it must
    not fall through to a connect against None."""
    proxy = EgressProxy(ApprovalBroker(RecordingGate(default=True)))
    loop = asyncio.get_running_loop()

    async def resolve_nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(loop, "getaddrinfo", resolve_nothing, raising=False)
    with caplog.at_level("WARNING", logger="claude_on_the_fly.egress"):
        assert await proxy._resolve_public("example.com", 443) is None
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "no usable address" in logged


async def test_pipe_survives_a_peer_that_vanishes_mid_stream():
    """Half of a tunnel dying is ordinary, so `_pipe` must return rather than
    raise into the handler and log a traceback for every closed connection."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"payload")

    class ResettingWriter:
        def write(self, _chunk: bytes) -> None:
            raise ConnectionResetError("peer gone")

    await EgressProxy._pipe(reader, ResettingWriter())  # type: ignore[arg-type]
