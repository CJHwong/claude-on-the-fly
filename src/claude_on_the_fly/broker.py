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

import hmac
import ipaddress
import logging
import os
import secrets
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from aiohttp import ClientResponse, ClientSession, ClientTimeout, web

from claude_on_the_fly.approvals import ApprovalBroker, ApprovalRequest

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
        # The unspecified address. A connect to it reaches the local host on
        # macOS and Linux, measured against a listener on 127.0.0.1.
        "0.0.0.0/8",
        "::/128",
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

# Largest request body the broker will relay. aiohttp's own default is 1 MiB,
# which is far below what this proxy actually carries: a long conversation, a
# pasted file, or an image attachment pushes a single /v1/messages POST past it,
# and the agent then gets a 413 from its own credential proxy with no way to tell
# that apart from an upstream rejection. Sized to Anthropic's documented 32 MB
# request limit with headroom, and kept explicit rather than unlimited because
# `_handle` buffers the body in memory before forwarding.
_MAX_BODY_BYTES = 64 * 1024 * 1024
_SESSION_PREFIX = "/_session/"


class HeaderSource(Protocol):
    """A credential read per request instead of once from the keychain.

    For a credential another process can rotate while the broker runs, such as
    codex's ChatGPT login (`codex_auth.ChatGPTLogin`).
    """

    async def headers(self) -> dict[str, str]:
        """The headers to inject on this request."""
        ...

    async def recover(self, sent: Mapping[str, str]) -> bool:
        """After an upstream 401: True when a retry would carry new headers."""
        ...


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
    # Optional sub-scoping. Both empty = today's behavior (any method, any
    # path under the prefix). Set to narrow a delegated credential: methods is
    # the allowed HTTP verbs; allowed_tails is the exact path tails (path minus
    # prefix, e.g. "v1/messages") the route may reach. Exact strings, not
    # patterns, so a typo can never silently reopen the prefix.
    methods: frozenset[str] = frozenset()
    allowed_tails: frozenset[str] = frozenset()
    # Set to read the credential per request instead of from the keychain;
    # `header`, `value_prefix` and `keychain_service` are then unused.
    source: HeaderSource | None = None

    @property
    def label(self) -> str:
        """Where the credential comes from, for logs. Never the value."""
        return self.keychain_service or f"{self.prefix} source"


def has_keychain() -> bool:
    """Whether this host has a macOS login keychain to consult.

    `security` ships with macOS and nowhere else, so on Linux `subprocess`
    raises FileNotFoundError naming a binary the operator has never heard of.
    That took the whole daemon down at startup for any Linux deployment that
    turned the sandbox on, because `routes_from_keychain` runs before anything
    is serving.

    The guard lives here rather than at each call site. It was already written
    once, inline, in `sandbox._claude_oauth_from_keychain`, and the identical
    crash survived one function call away in the broker's own startup path.
    A second copy is how that happens again.

    There is no keychain on Linux, so "no item" is the honest answer rather
    than an error: the operator provisions credentials another way, and the
    broker starts with whatever routes it has.
    """
    return sys.platform == "darwin"


def read_keychain(service: str) -> str:
    """Read a generic-password value from the macOS keychain. Never logged.

    Raises KeyError if the item is absent so misconfiguration fails loudly at
    broker start rather than on the first agent request.
    """
    if not has_keychain():
        raise KeyError(f"no keychain on this platform: service={service!r}")
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
    if not has_keychain():
        return False
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", service],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def blocked_host(host: str) -> bool:
    """True if host is a literal IP inside a blocked range. Hostnames pass.

    Public because egress.py applies the same never-ask judgement to CONNECT
    targets; duplicating the CIDR list in two policy paths is how they drift.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # `::ffff:127.0.0.1` is 127.0.0.1 to the kernel, but an IPv6Address is never
    # inside an IPv4 network, so without this every range above has a second
    # spelling that passes.
    addr = getattr(addr, "ipv4_mapped", None) or addr
    return any(addr in net for net in _BLOCKED_NETS)


def _forward_request_headers(
    headers, injected: Iterable[str] = (), expected: Iterable[str] = ()
) -> dict[str, str]:
    """Copy request headers minus hop-by-hop and any caller-supplied auth.

    A header the broker injects is dropped too, whatever its case. Headers go
    upstream as a plain dict, so a caller's `chatgpt-account-id` beside the
    broker's `ChatGPT-Account-Id` would otherwise travel as two headers.

    `expected` names auth headers the caller sends legitimately, logged at DEBUG
    when stripped instead of WARNING.
    """
    replaced = {name.lower() for name in injected}
    kept = {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP
        and key.lower() not in _STRIP_REQUEST_HEADERS
        and key.lower() not in replaced
    }
    # Names only, never values. A stripped auth header means the agent sent a
    # credential of its own, which is either a misconfigured SDK or a key an
    # injected payload planted, and both are worth seeing. Logged at WARNING for
    # that reason rather than folded into the debug stream.
    stripped = [key for key in headers if key.lower() in _STRIP_REQUEST_HEADERS]
    quiet = {name.lower() for name in expected}
    loud = [key for key in stripped if key.lower() not in quiet]
    if loud:
        logger.warning(
            "broker: stripped caller-supplied auth header(s) %s before forwarding",
            loud,
        )
    if len(loud) < len(stripped):
        logger.debug(
            "broker: replaced the caller's own %s",
            [key for key in stripped if key.lower() in quiet],
        )
    return kept


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

    def __init__(
        self, routes: list[Route], approvals: ApprovalBroker | None = None
    ) -> None:
        if not routes:
            raise ValueError("Broker needs at least one route")
        for route in routes:
            host = urlsplit(route.upstream).hostname or ""
            if blocked_host(host):
                raise ValueError(
                    f"route {route.prefix!r} upstream host {host!r} is in a blocked range"
                )
        # Longest prefix first so /anthropic/v1 wins over /anthropic.
        self._routes = sorted(routes, key=lambda r: len(r.prefix), reverse=True)
        self._creds: dict[str, str] = {}
        self._session: ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._port: int | None = None
        # When present, a method/path scope miss becomes an operator question
        # instead of a flat 403. None keeps the original deny-only behavior.
        self._approvals = approvals
        # The route is on loopback, but loopback is not authentication. A
        # random path capability keeps an unrelated local process that merely
        # discovers the port from invoking a credentialed upstream request.
        self._token = secrets.token_urlsafe(32)

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("broker not started")
        return self._port

    def base_url_env(self) -> dict[str, str]:
        """Env overrides pointing each provider SDK at this broker.

        For every route that declares a base_url_env_var, maps it to a
        token-scoped URL. The agent sends plain HTTP there and the broker injects
        the real key on the broker->upstream leg. The unscoped loopback root is
        never a usable route.
        """
        return {
            route.base_url_env_var: (
                f"http://127.0.0.1:{self.port}{_SESSION_PREFIX}"
                f"{self._token}{route.prefix}"
            )
            for route in self._routes
            if route.base_url_env_var
        }

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        for route in self._routes:
            if route.source is None:
                self._creds[route.keychain_service] = read_keychain(
                    route.keychain_service
                )
        # auto_decompress=False keeps this byte-transparent. aiohttp decompresses
        # by default, which combined with forwarding the upstream's
        # `Content-Encoding: gzip` handed every client a decompressed body still
        # labelled compressed; they all failed with a zlib error. Passing the
        # bytes through untouched keeps the header truthful and skips a
        # decompress/recompress round trip a proxy has no reason to do.
        self._session = ClientSession(
            timeout=ClientTimeout(total=None), auto_decompress=False
        )
        app = web.Application(client_max_size=_MAX_BODY_BYTES)
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

    def add_route(self, route: Route) -> None:
        """Insert a route into the live table and load its credential.

        Lets an operator widen the broker without a restart. Ordering is
        re-established on insert because _match relies on longest-prefix-first.
        """
        if route.source is None:
            self._creds[route.keychain_service] = read_keychain(route.keychain_service)
        self._routes = sorted(
            [*self._routes, route], key=lambda r: len(r.prefix), reverse=True
        )
        logger.warning("broker: route %s added at runtime", route.prefix)

    async def _scope_denial(
        self, route: Route, method: str, tail: str
    ) -> web.StreamResponse | None:
        """None if the call is within the route's scope, else a 403 to return.

        Both sub-scoping sets fail closed and only ever narrow: empty means "no
        restriction", so an unscoped route behaves exactly as it did before
        scoping existed. When an approval broker is wired in, a miss becomes an
        operator question first and a 403 only if that question is declined.
        """
        if route.methods and method not in route.methods:
            granted = await self._ask_scope(
                route,
                subject=f"{route.prefix} {method}",
                detail=(
                    f"The sandboxed agent tried {method} on broker route "
                    f"{route.prefix}, which is scoped to "
                    f"{sorted(route.methods)}. Approving lets it use {method} "
                    f"with the route's injected credential."
                ),
            )
            if not granted:
                return web.Response(status=403, text=self._scope_body("method"))
        if route.allowed_tails and tail not in route.allowed_tails:
            granted = await self._ask_scope(
                route,
                subject=f"{route.prefix}/{tail}",
                detail=(
                    f"The sandboxed agent tried to reach {tail!r} on broker route "
                    f"{route.prefix}, which is scoped to "
                    f"{sorted(route.allowed_tails)}. Approving lets it call that "
                    f"path with the route's injected credential."
                ),
            )
            if not granted:
                return web.Response(status=403, text=self._scope_body("path"))
        return None

    async def _ask_scope(self, route: Route, *, subject: str, detail: str) -> bool:
        """Ask the operator to widen one route's scope. False without a gate."""
        if self._approvals is None:
            logger.warning("broker: deny %s (no approval channel)", subject)
            return False
        return await self._approvals.check(
            ApprovalRequest(kind="route-scope", subject=subject, detail=detail)
        )

    @staticmethod
    def _scope_body(dimension: str) -> str:
        """403 body for a declined scope widening.

        Deliberately does not say "retrying will not help": with an approval
        channel attached a retry after the operator grants it *does* succeed,
        and telling the agent otherwise would suppress the one useful action.
        """
        return (
            f"[sandbox] egress policy: this route does not permit this {dimension}. "
            "An operator was asked and did not approve it. Do not loop on this. "
            "Tell the user exactly what you need and why, then continue with "
            "whatever you can still do; if they approve, a later retry succeeds."
        )

    def _authorized_path(self, path: str) -> str | None:
        """Strip the bearer path capability, or return None when absent/bad."""
        if not path.startswith(_SESSION_PREFIX):
            return None
        rest = path[len(_SESSION_PREFIX) :]
        token, separator, route_tail = rest.partition("/")
        if not separator or not token or not hmac.compare_digest(token, self._token):
            return None
        return "/" + route_tail

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        route_path = self._authorized_path(request.path)
        if route_path is None:
            logger.warning("broker: deny unauthenticated loopback request")
            return web.Response(
                status=403, text="[sandbox] broker authentication required"
            )
        route = self._match(route_path)
        if route is None:
            logger.warning(
                "broker: deny %s %s (no matching route)", request.method, route_path
            )
            return web.Response(
                status=403,
                text=(
                    "[sandbox] egress policy: no allowlisted broker route for this "
                    "path. The broker only serves providers it holds a credential "
                    "for, and it cannot infer a host from an unmapped prefix, so "
                    "retrying this path will not help. For an ordinary HTTPS host, "
                    "use a normal https:// request instead: those go through the "
                    "egress proxy, which can ask the operator to approve the host "
                    "on the spot. If you genuinely need a new credentialed "
                    "provider, tell the user the operator must add a broker route."
                ),
            )

        tail = route_path[len(route.prefix) :].lstrip("/")
        denial = await self._scope_denial(route, request.method, tail)
        if denial is not None:
            return denial
        url = route.upstream.rstrip("/") + "/" + tail
        try:
            injected = await self._injected(route)
        except Exception:
            # The message names the route, not the credential. A traceback here
            # comes from reading a file or a token endpoint, never from a value.
            logger.exception("broker: cannot load the credential for %s", route.prefix)
            return web.Response(
                status=502,
                text=(
                    "[sandbox] broker could not load the credential for this route. "
                    "Retrying will not help; tell the user the operator must check "
                    "the daemon log."
                ),
            )
        # codex with the jail off still reads its own login and sends that
        # Authorization on its side calls: 15 WARNINGs a turn for a header the
        # source replaces anyway. A keychain route keeps the WARNING, because no
        # client of one holds a credential of its own.
        headers = _forward_request_headers(
            request.headers,
            injected,
            expected=injected if route.source is not None else (),
        )
        headers.update(injected)
        body = await request.read()
        # Header *names* and the injection target, so a "why is upstream 401"
        # question can be answered without the value ever being written down.
        logger.debug(
            "broker: %s -> %s, injecting %s from %s, forwarding headers %s, %d B body",
            route_path,
            url,
            sorted(injected),
            route.label,
            sorted(headers),
            len(body),
        )

        assert self._session is not None
        upstream = await self._send(request, url, headers, body)
        # One retry, with whatever the source now holds. The body was buffered
        # above, so the retry sends the same request.
        if (
            upstream.status == 401
            and route.source is not None
            and await route.source.recover(injected)
        ):
            upstream.release()
            injected = await route.source.headers()
            headers.update(injected)
            logger.info("broker: %s got 401, retrying once", route.prefix)
            upstream = await self._send(request, url, headers, body)
        try:
            logger.info(
                "broker: allow %s %s%s [%s] -> %d",
                request.method,
                urlsplit(route.upstream).hostname,
                request.path,
                route.label,
                upstream.status,
            )
            response = web.StreamResponse(
                status=upstream.status,
                headers=_forward_response_headers(upstream.headers),
            )
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_chunked(_CHUNK):
                    await response.write(chunk)
                await response.write_eof()
            except ConnectionResetError:
                # The caller hung up. codex does this on every streamed model call,
                # after the last event and before the chunked terminator, so the
                # answer was already delivered. aiohttp would log it as an
                # unhandled error with a traceback.
                logger.debug("broker: %s closed by the caller mid-stream", route.prefix)
            return response
        finally:
            upstream.release()

    async def _injected(self, route: Route) -> dict[str, str]:
        """The credential headers for one request on `route`."""
        if route.source is not None:
            return await route.source.headers()
        return {route.header: route.value_prefix + self._creds[route.keychain_service]}

    async def _send(
        self, request: web.Request, url: str, headers: dict[str, str], body: bytes
    ) -> ClientResponse:
        assert self._session is not None
        # allow_redirects=False: never follow a redirect, so we never re-inject
        # the credential onto a redirected request (the strands rule).
        return await self._session.request(
            request.method,
            url,
            headers=headers,
            params=request.query,
            data=body,
            allow_redirects=False,
        )


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
    # Delegate another provider by appending a Route and provisioning its
    # keychain item (`security add-generic-password -s cotf-<name> -w <key>`).
    # It stays inert until the item exists (routes_from_keychain filters it).
    # Narrow the grant with methods=/allowed_tails=, e.g. a read-only, single-
    # endpoint route:
    #   Route(prefix="/openai", upstream="https://api.openai.com",
    #         header="authorization", value_prefix="Bearer ",
    #         keychain_service="cotf-openai", base_url_env_var="OPENAI_BASE_URL",
    #         methods=frozenset({"POST"}),
    #         allowed_tails=frozenset({"v1/chat/completions"}))
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


async def start_default_broker(
    approvals: ApprovalBroker | None = None, *, keychain: bool = True
) -> Broker | None:
    """Start a broker for whichever DEFAULT_ROUTES have keychain items, publish
    their token-scoped base-urls into os.environ for sandbox.agent_env to forward,
    and return it.

    The codex ChatGPT route joins them when the operator turned it on. With
    `keychain=False` it is the only route: a daemon without a sandbox brokers
    the ChatGPT login and nothing else, because publishing ANTHROPIC_BASE_URL
    there would move every claude turn onto the broker as a side effect.

    Returns None when no route is provisioned (nothing to serve).
    """
    from claude_on_the_fly import codex_auth

    routes = routes_from_keychain(DEFAULT_ROUTES) if keychain else []
    if codex_auth.enabled():
        routes.append(codex_auth.route())
    if not routes:
        logger.warning("broker: no provisioned routes found; not starting")
        return None
    broker = Broker(routes, approvals=approvals)
    await broker.start()
    os.environ.update(broker.base_url_env())
    logger.info("broker: started with %d route(s)", len(routes))
    return broker
