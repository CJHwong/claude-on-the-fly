"""Credential-injecting reverse proxy so the agent never holds an API key.

The agent's sandbox can reach exactly one network endpoint: this broker on
loopback. It speaks plain HTTP to the broker; the broker holds the real keys
(read from the macOS keychain), injects them on the broker->upstream leg, and
forwards over HTTPS. A hijacked agent cannot exfiltrate a key it never received,
and any request that doesn't match an allowlisted route is refused, so the agent
can reach nothing else.

Design notes live in docs/agent/broker.md. The threat model and the
reference architectures this follows (Anthropic's session-token MITM proxy,
strands-agents per-URL injection) are summarized there.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger(__name__)

# Caller-supplied auth headers stripped before forwarding: the broker injects
# its own credential, and an attacker-embedded key (e.g. from a poisoned file)
# must never reach upstream. Matched case-insensitively.
_STRIP_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "anthropic-api-key",
        "openai-api-key",
        "api-key",
        "x-goog-api-key",
    }
)

# RFC 7230 6.1 hop-by-hop headers, plus framing headers the stream layer owns.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

# Reject routing to link-local/metadata, loopback, and RFC1918 literals. The
# cloud metadata endpoint (169.254.169.254) lives in link-local. Routes are
# operator-controlled, so this is a config-time sanity guard, not an
# agent-exploitable SSRF surface (the agent cannot name an arbitrary upstream).
_BLOCKED_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)

_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Route:
    """One allowlisted upstream the agent may reach, keyed by path prefix.

    The agent calls ``http://127.0.0.1:<port><prefix>/...``; the broker forwards
    to ``<upstream>/...`` with ``header: <value_prefix><keychain value>`` added.
    Example (Anthropic): prefix="/anthropic", upstream="https://api.anthropic.com",
    header="x-api-key", keychain_service="cotf-anthropic". OpenAI-style uses
    header="authorization", value_prefix="Bearer ".
    """

    prefix: str
    upstream: str
    header: str
    keychain_service: str
    value_prefix: str = ""
    # Env var the agent's SDK reads to find this provider, e.g.
    # "ANTHROPIC_BASE_URL". Published by the broker pointing at itself.
    base_url_env_var: str = ""


def read_keychain(service: str) -> str:
    """Read a generic-password value from the macOS keychain. Never logged.

    Raises KeyError if the item is absent so misconfiguration fails loudly at
    broker start rather than on the first agent request.
    """
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise KeyError(f"keychain item not found: service={service!r}")
    return proc.stdout.rstrip("\n")


def keychain_exists(service: str) -> bool:
    """True if a generic-password item exists, without reading its value."""
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", service],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _blocked_host(host: str) -> bool:
    """True if host is a literal IP inside a blocked range. Hostnames pass."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _BLOCKED_NETS)


def _forward_request_headers(headers) -> dict[str, str]:
    """Copy request headers minus hop-by-hop and any caller-supplied auth."""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP and key.lower() not in _STRIP_REQUEST_HEADERS
    }


def _forward_response_headers(headers) -> dict[str, str]:
    """Copy upstream response headers minus framing/hop-by-hop ones."""
    return {
        key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP
    }


class Broker:
    """Loopback reverse proxy that injects keychain-backed credentials.

    Lifecycle: ``start()`` binds a loopback TCP port (0 = OS-assigned), loads
    every route's credential into memory once, and returns the bound port.
    ``stop()`` tears the listener down and clears creds from memory. Revocation
    of the whole capability is just ``stop()``.
    """

    def __init__(self, routes: list[Route]) -> None:
        if not routes:
            raise ValueError("Broker needs at least one route")
        for route in routes:
            host = urlsplit(route.upstream).hostname or ""
            if _blocked_host(host):
                raise ValueError(
                    f"route {route.prefix!r} upstream host {host!r} is in a blocked range"
                )
        # Longest prefix first so /anthropic/v1 wins over /anthropic.
        self._routes = sorted(routes, key=lambda r: len(r.prefix), reverse=True)
        self._creds: dict[str, str] = {}
        self._session: ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("broker not started")
        return self._port

    def base_url_env(self) -> dict[str, str]:
        """Env overrides pointing each provider SDK at this broker.

        For every route that declares a base_url_env_var, maps it to
        http://127.0.0.1:<port><prefix>. The agent sends plain HTTP there and
        the broker injects the real key on the broker->upstream leg.
        """
        return {
            route.base_url_env_var: f"http://127.0.0.1:{self.port}{route.prefix}"
            for route in self._routes
            if route.base_url_env_var
        }

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        for route in self._routes:
            self._creds[route.keychain_service] = read_keychain(route.keychain_service)
        self._session = ClientSession(timeout=ClientTimeout(total=None))
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        self._port = self._runner.addresses[0][1]
        logger.info(
            "broker: listening on %s:%d with %d route(s)",
            host,
            self._port,
            len(self._routes),
        )
        return self._port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._creds.clear()
        self._port = None

    def _match(self, path: str) -> Route | None:
        for route in self._routes:
            if path == route.prefix or path.startswith(route.prefix + "/"):
                return route
        return None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        route = self._match(request.path)
        if route is None:
            logger.warning(
                "broker: deny %s %s (no matching route)", request.method, request.path
            )
            return web.Response(status=403, text="no route for path")

        tail = request.path[len(route.prefix) :].lstrip("/")
        url = route.upstream.rstrip("/") + "/" + tail
        headers = _forward_request_headers(request.headers)
        headers[route.header] = route.value_prefix + self._creds[route.keychain_service]
        body = await request.read()

        assert self._session is not None
        upstream_host = urlsplit(route.upstream).hostname
        # allow_redirects=False: never follow a redirect, so we never re-inject
        # the credential onto a redirected request (the strands rule).
        async with self._session.request(
            request.method,
            url,
            headers=headers,
            params=request.query,
            data=body,
            allow_redirects=False,
        ) as upstream:
            logger.info(
                "broker: allow %s %s%s -> %d",
                request.method,
                upstream_host,
                request.path,
                upstream.status,
            )
            response = web.StreamResponse(
                status=upstream.status,
                headers=_forward_response_headers(upstream.headers),
            )
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(_CHUNK):
                await response.write(chunk)
            await response.write_eof()
            return response


# Provider routes the daemon offers by default. Extend with OpenAI / OpenRouter
# / etc. by adding Route entries; each activates only if its keychain item
# exists. Anthropic backs the default `claude` backend.
DEFAULT_ROUTES: list[Route] = [
    Route(
        prefix="/anthropic",
        upstream="https://api.anthropic.com",
        header="x-api-key",
        keychain_service="cotf-anthropic",
        base_url_env_var="ANTHROPIC_BASE_URL",
    ),
]


def routes_from_keychain(routes: list[Route]) -> list[Route]:
    """Keep only routes whose keychain item is present, so the broker starts
    with whatever credentials are provisioned rather than failing on an absent
    one."""
    live: list[Route] = []
    for route in routes:
        if keychain_exists(route.keychain_service):
            live.append(route)
        else:
            logger.info(
                "broker: skipping route %s (keychain item %r absent)",
                route.prefix,
                route.keychain_service,
            )
    return live


async def start_default_broker() -> Broker | None:
    """Start a broker for whichever DEFAULT_ROUTES have keychain items, publish
    their base-urls into os.environ for agent_env to forward, and return it.

    Returns None when no route is provisioned (nothing to serve).
    """
    routes = routes_from_keychain(DEFAULT_ROUTES)
    if not routes:
        logger.warning("broker: no provisioned routes found; not starting")
        return None
    broker = Broker(routes)
    await broker.start()
    os.environ.update(broker.base_url_env())
    logger.info("broker: started with %d route(s)", len(routes))
    return broker
