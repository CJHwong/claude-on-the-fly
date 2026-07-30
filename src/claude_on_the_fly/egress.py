"""CONNECT-gating egress proxy: the agent's only route to the internet.

The credential broker (broker.py) covers hosts we hold a key for, addressed by
path prefix. That shape cannot gate anything else, because the agent never names
a host: it asks for a prefix, and an unmapped prefix carries no information about
where the agent wanted to go. So `git`, `pip`, `curl`, and `gh` have nothing to
talk to under the jail, which denies all non-loopback egress.

This module closes that gap. The agent gets `HTTPS_PROXY` pointed here, so every
outbound connection arrives as a cleartext `CONNECT host:port` line naming its
destination. That name is the thing we can gate on, and an unknown host becomes
an operator question (see approvals.py) rather than a failed run.

**No TLS interception.** Gating a host needs only the CONNECT line; reading the
body would need a private CA, a synthesized leaf cert per host, and every client
in the sandbox trusting it. That buys credential injection, which the broker
already does for the hosts that need it, at the cost of a CA the agent could be
tricked into trusting and a trust-store problem for any Go binary on macOS
(Go reads the system store and ignores SSL_CERT_FILE). So this proxy blind-pipes
bytes after the gate: it learns *where*, never *what*.

Consequence worth stating plainly: an approved host is a covert channel. This
bounds which hosts the agent can reach, and nothing about what it sends there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
from dataclasses import dataclass

from claude_on_the_fly import settings
from claude_on_the_fly.approvals import ApprovalBroker, ApprovalRequest

logger = logging.getLogger(__name__)

# Strict DNS hostname grammar. Every byte outside this set is refused, which
# forecloses a family of parser-differential attacks in one check rather than
# blocklisting each: a NUL truncates in libc getaddrinfo but not in Python
# str comparisons, percent-encoding decodes inconsistently between the client
# and this gate, and CR/LF smuggles headers. IPv6 literals and non-punycode
# IDNs are rejected as a documented consequence.
_DNS_SAFE_HOST = re.compile(r"\A[A-Za-z0-9.\-]+\Z")


def parse_hosts(section: object, key: str, *, source: str) -> frozenset[str]:
    """One host list out of an `egress:` section, lowercased. Raises ValueError.

    Hosts are validated here rather than at CONNECT time so a typo is a startup
    error naming the file, not a silently dead entry that turns into an approval
    prompt months later for a host the operator believes they already allowed.
    """
    if not isinstance(section, dict):
        raise ValueError(f"{source}: the egress section must be a mapping")
    value = section.get(key)
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError(f"{source}: egress.{key} must be a list")
    hosts = set()
    for item in value:
        host = str(item).strip().lower()
        if not is_dns_safe_host(host):
            raise ValueError(f"{source}: egress.{key} entry {item!r} is not a hostname")
        hosts.add(host)
    return frozenset(hosts)


def _egress_hosts(key: str) -> frozenset[str]:
    """Bundled hosts for `key`, unioned with the operator's own.

    Union, never a subtraction, and that matters most for `never_ask`: the bundled
    entries are the cloud metadata endpoints, which hand instance credentials to
    anything that can reach them. Letting a config edit remove one would make the
    only unconditional refusal in the system optional.

    A malformed operator section falls back to bundled-only and logs at ERROR,
    matching `commands.load_tools`: the operator loses their additions, loudly,
    rather than the whole policy silently emptying.
    """
    bundled = parse_hosts(
        settings.bundled("egress"), key, source=str(settings.BUNDLED_SETTINGS)
    )
    section = settings.operator("egress")
    if not section:
        return bundled
    path = settings.operator_settings()
    try:
        return bundled | parse_hosts(section, key, source=str(path))
    except ValueError as exc:
        logger.error(
            "egress: ignoring egress.%s from %s (%s); bundled hosts only",
            key,
            path,
            exc,
        )
        return bundled


def default_allowed_hosts() -> frozenset[str]:
    """Hosts always tunnelled without asking, from `egress.allow`.

    The bundled criterion is narrow on purpose: a host earns a place only if a
    supported backend cannot function at all without it, so the model APIs and
    nothing else. Gating those would stop every fresh deployment on an approval
    for the agent's own LLM call. Package registries, github.com, and telemetry
    are deliberately absent because each is a real decision an operator makes.
    """
    return _egress_hosts("allow")


def default_never_ask() -> frozenset[str]:
    """Hosts refused without asking, from `egress.never_ask`.

    IP-literal metadata addresses are covered separately by `broker.blocked_host`.
    """
    return _egress_hosts("never_ask")


def never_ask_subjects() -> frozenset[str]:
    """`default_never_ask()` as ApprovalPolicy subject patterns.

    An ApprovalRequest subject for a host is "<host>:<port>", so handing the bare
    hostnames to `ApprovalPolicy(never_ask=...)` matched nothing at all and the
    policy tier was silently dead. `ApprovalPolicy.refuses` treats a trailing "*"
    as a prefix match, so the port-suffixed form is what actually refuses.

    The EgressProxy checks the never-ask set itself before it ever reaches the
    broker, so this is the defense-in-depth copy: it is what stops any *other*
    requester (or a proxy wired without a never-ask set) from offering a metadata
    endpoint to an operator.
    """
    return frozenset(f"{host}:*" for host in default_never_ask())


_MAX_REQUEST_LINE = 8192
_CHUNK = 64 * 1024
_CONNECT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class _Refusal:
    """What a refused CONNECT tells the agent. Two texts, because they differ in
    reach.

    **No client surfaces a CONNECT response body.** Verified against curl, httpx,
    and stdlib urllib: each reports the status line and discards everything after
    it (`curl: (56) CONNECT tunnel failed, response 403`). So a body alone reaches
    nobody, which is why the elaborate one this module used to send was invisible
    regardless of what it said.

    The *reason phrase* does get through, on all three, and httpx and urllib put it
    straight into the exception text an agent reads:

        ProxyError | 403 Forbidden by egress policy: host permanently blocked...

    So `hint` is the reason phrase and carries the part that must arrive. It opens
    with "Forbidden" so the status line still reads correctly, and contains "egress
    policy" so it matches the tag the sandbox guidance teaches the agent to look
    for. `body` keeps the full instruction for a raw reader and a packet capture;
    it costs one write and is the only place there is room to say *why*.

    Neither text ever interpolates the requested host. They are module constants,
    so no agent-controlled bytes reach the status line, where a CR/LF would be
    header injection.

    Length is not a constraint at this size: a 204-character phrase arrived intact
    on all three clients.
    """

    hint: str
    body: str


# One per cause, and each tells the agent something different to *do* — that is
# the only reason to have more than one.
_DENIED = _Refusal(
    "Forbidden by egress policy: host not approved, an operator declined it",
    "[sandbox] egress policy: this host is not approved for this session. "
    "An operator was asked and did not approve it. This is policy, not an "
    "outage. If it is genuinely required, tell the user which host you need "
    "and why, and let them approve it.",
)
_NEVER_ASK = _Refusal(
    "Forbidden by egress policy: host permanently blocked, cannot be approved",
    "[sandbox] egress policy: this host is permanently blocked and cannot be "
    "approved. Do not retry and do not look for another route to it; tell the "
    "user what you were trying to do instead.",
)
_MALFORMED_HOST = _Refusal(
    "Forbidden by egress policy: not a valid hostname, check the URL you built",
    "[sandbox] egress policy: that is not a valid hostname, so it was refused "
    "without being looked up. This is a problem with the request rather than a "
    "policy block: check the URL you built. IPv6 literals and non-ASCII domain "
    "names are not supported here.",
)
_NO_PUBLIC_ADDRESS = _Refusal(
    "Forbidden by egress policy: no usable public address, retrying will not help",
    "[sandbox] egress policy: that host does not resolve to a usable public "
    "address — either the lookup failed, or it points into private address space, "
    "which is refused so a sandboxed agent cannot reach services on the host "
    "machine. No operator was asked, and retrying will not change it. Say what you "
    "were trying to reach.",
)

# Stands in for "this decision is not a refusal". A sentinel rather than `None` so
# the field is never optional: with `_Refusal | None`, every use site had to narrow
# a type the class already guarantees, and the obvious guard (`if refusal is not
# None`) stopped narrowing `address` for the allow path.
_ALLOWED = _Refusal("", "")

# Not a CONNECT, so this one is an ordinary HTTP response and its body does reach
# the client. No hint needed.
_PLAIN_HTTP_BODY = (
    "[sandbox] egress policy: this proxy only tunnels HTTPS via CONNECT. "
    "Retry the same request over https://."
)


@dataclass(frozen=True)
class _Decision:
    """One gate outcome, the reason for it, and what the agent is told.

    `because` exists for the log. "allow" alone cannot be reviewed: a pre-approved
    host, a standing grant, and a decision an operator just made are three
    different facts about a run, and only one of them means a human was in the loop.

    The agent-facing `refusal` lives here rather than at the refusal site, because
    that split *was* a bug: the site that knew why a host was refused was not the
    site that chose the message, so every 403 claimed an operator had declined —
    for never-ask hosts and unresolvable names alike, which no operator is ever
    offered. An agent told a human said no will reasonably retry or look for
    another route, which is the behaviour these messages exist to prevent.
    """

    address: str | None
    because: str
    refusal: _Refusal = _ALLOWED

    def __post_init__(self) -> None:
        # Belt to the docstring's braces: a refusal with nothing to say would send
        # a bare 403, which is the same "no information" failure in a new shape.
        # Cheaper to make impossible than to review for.
        if self.address is None and self.refusal is _ALLOWED:
            raise ValueError("a refusal must carry what the agent is shown")


def is_dns_safe_host(host: str) -> bool:
    """True if `host` contains only DNS-safe ASCII. See `_DNS_SAFE_HOST`."""
    return bool(host) and _DNS_SAFE_HOST.match(host) is not None


def parse_connect_target(target: str) -> tuple[str, int] | None:
    """Split a CONNECT target into (host, port), or None if malformed.

    A CONNECT target is always authority-form (`host:port`) per RFC 7231. The
    port is mandatory in practice and required here, because defaulting it would
    let a caller widen a grant scoped to one port.
    """
    host, separator, port_text = target.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    return host, port


class EgressProxy:
    """Loopback CONNECT proxy that gates each destination host.

    Lifecycle mirrors Broker: `start()` binds a loopback port (0 for an
    OS-assigned one) and returns it; `stop()` tears the listener down. Killing
    the proxy revokes every route out of the sandbox at once.
    """

    def __init__(
        self,
        approvals: ApprovalBroker,
        *,
        allowed_hosts: frozenset[str] = frozenset(),
        never_ask: frozenset[str] | None = None,
        grant_ttl_seconds: float = 3600.0,
        label: str = "",
    ) -> None:
        self._approvals = approvals
        # Which session this proxy serves. Proxies are per-session, so this is
        # the only thing that attributes a CONNECT to a conversation: the
        # protocol carries a hostname and nothing else, and two chats reaching
        # the same host are otherwise indistinguishable in the log.
        self._label = label
        # Pre-approved hosts skip the operator entirely: `egress.allow` from the
        # settings file (the model APIs, plus whatever the operator added) unioned
        # with anything the caller front-loaded.
        self._allowed = default_allowed_hosts() | frozenset(
            host.lower() for host in allowed_hosts
        )
        # None rather than a frozenset default so the settings file is read per
        # instance instead of once at import, which is what lets an operator edit
        # take effect on the next session rather than on the next daemon restart.
        if never_ask is None:
            never_ask = default_never_ask()
        self._never_ask = frozenset(host.lower() for host in never_ask)
        self._ttl = grant_ttl_seconds
        self._server: asyncio.Server | None = None
        self._port: int | None = None
        # In-flight tunnel handlers, held so stop() can cancel them. A tunnel
        # blocks until *both* directions EOF, and a client that stops reading
        # while its upstream stays quiet never reaches that, so
        # Server.wait_closed() (which joins handler tasks on 3.12+) would hang
        # shutdown indefinitely. Also a strong-reference set: asyncio only
        # weak-refs handler tasks, so without this a tunnel can be collected
        # mid-copy.
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("egress proxy not started")
        return self._port

    @property
    def _tag(self) -> str:
        """Log prefix identifying which session's proxy this is."""
        return f"egress[{self._label}]" if self._label else "egress"

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        self._port = self._server.sockets[0].getsockname()[1]
        logger.info(
            "%s: CONNECT proxy on %s:%d (%d pre-approved host(s))",
            self._tag,
            host,
            self._port,
            len(self._allowed),
        )
        # The allowlist decides which hosts never reach the operator, so it is
        # the first thing to check when a host was tunnelled without a prompt.
        logger.debug("%s: pre-approved hosts %s", self._tag, sorted(self._allowed))
        logger.debug("%s: never-ask hosts %s", self._tag, sorted(self._never_ask))
        return self._port

    async def stop(self) -> None:
        """Stop listening and tear down every open tunnel. Idempotent.

        Cancelling in-flight handlers first is what makes this terminate: an
        open tunnel has no deadline of its own, so joining them would hang.
        Revoking egress is more urgent than draining it politely.
        """
        if self._server is None:
            return
        self._server.close()
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        self._handlers.clear()
        await self._server.wait_closed()
        self._server = None
        self._port = None

    def proxy_env(self) -> dict[str, str]:
        """Proxy env vars pointing every HTTP client in the sandbox at us.

        NO_PROXY exempts loopback, which is not optional. Agents run local helper
        processes and poll them over plain HTTP: codex stands up an app-server and
        hits `http://127.0.0.1:<port>/health` in a loop. Without this those calls
        arrive here as non-CONNECT requests, get a 405, and the agent fails to
        start while the log fills with denials.

        This is not a hole in the egress policy. Which loopback ports the agent
        may reach is decided by the seatbelt (see COTF_SANDBOX_BROKER_ONLY_LOOPBACK),
        not by whether traffic detours through this proxy, and a proxy that
        tunnelled loopback would only be laundering connections the kernel is
        already ruling on.
        """
        url = f"http://127.0.0.1:{self.port}"
        no_proxy = "localhost,127.0.0.1,::1"
        return {
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "http_proxy": url,
            "https_proxy": url,
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)
        try:
            await self._serve(reader, writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:
            logger.exception("%s: connection handler failed", self._tag)
        finally:
            writer.close()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
        except ValueError:
            # A StreamReader raises rather than returning once its buffer limit is
            # passed, so a request line over the stream limit never reached the
            # length check below: it surfaced as an unhandled exception in
            # _handle, with no response written to the client at all.
            await self._refuse(writer, 414, "request line too long")
            return
        if not request_line:
            return
        if len(request_line) > _MAX_REQUEST_LINE:
            await self._refuse(writer, 414, "request line too long")
            return
        parts = request_line.decode("latin-1").split()
        if len(parts) < 2:
            await self._refuse(writer, 400, "malformed request line")
            return
        method, target = parts[0].upper(), parts[1]
        if method != "CONNECT":
            logger.warning("%s: deny %s %s (not CONNECT)", self._tag, method, target)
            await self._refuse(writer, 405, _PLAIN_HTTP_BODY)
            return
        logger.debug("%s: CONNECT %s received", self._tag, target)
        await self._drain_headers(reader)
        await self._connect(reader, writer, target)

    @staticmethod
    async def _drain_headers(reader: asyncio.StreamReader) -> None:
        """Consume the CONNECT request's headers up to the blank line.

        Their contents are irrelevant: the tunnel carries TLS, so nothing here
        is forwarded upstream. They just have to leave the stream.
        """
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                return

    async def _connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: str,
    ) -> None:
        """Gate `target`, then splice the client to it on approval."""
        parsed = parse_connect_target(target)
        if parsed is None:
            await self._refuse(writer, 400, f"malformed CONNECT target {target!r}")
            return
        host, port = parsed
        decision = await self._permitted(host, port)
        if decision.address is None:
            await self._refuse(
                writer, 403, decision.refusal.body, hint=decision.refusal.hint
            )
            return
        target = decision.address
        try:
            upstream = await asyncio.wait_for(
                asyncio.open_connection(target, port),
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "%s: upstream %s:%d unreachable: %s", self._tag, host, port, exc
            )
            await self._refuse(writer, 502, f"cannot reach {host}:{port}")
            return
        # `because` is the part that matters when reviewing a run: a host that
        # was pre-approved, one an operator granted earlier, and one an operator
        # just approved are three different facts, and the old line conflated
        # all three into "allow".
        logger.info(
            "%s: allow CONNECT %s:%d via %s (%s)",
            self._tag,
            host,
            port,
            target,
            decision.because,
        )
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._tunnel(reader, writer, upstream)

    async def _permitted(self, host: str, port: int) -> _Decision:
        """Where to connect and why, or a refusal carrying its reason.

        Order matters. The operator allowlist wins first and is returned by name
        without a private-address check: an operator writing `localhost` into
        `egress.allow` so the agent can hit its own dev server means it, and that
        is a deliberate config act rather than something the agent induced.

        Everything else must clear the never-ask tier *and* resolve entirely to
        public addresses, and is then pinned to the address we validated.
        """
        lowered = host.lower()
        if not is_dns_safe_host(host):
            logger.warning("%s: refuse %r (host is not DNS-safe)", self._tag, host)
            return _Decision(None, "host is not DNS-safe", _MALFORMED_HOST)
        if lowered in self._allowed:
            return _Decision(host, "pre-approved host")
        if lowered in self._never_ask:
            logger.warning("%s: refuse %s:%d (never-ask policy)", self._tag, host, port)
            return _Decision(None, "never-ask policy", _NEVER_ASK)
        pinned = await self._resolve_public(lowered, port)
        if pinned is None:
            return _Decision(None, "no usable public address", _NO_PUBLIC_ADDRESS)
        subject = f"{lowered}:{port}"
        # Asked before deciding so the log distinguishes a standing grant from a
        # fresh operator decision; check() itself cannot report which it was.
        already = self._approvals.allows(f"host:{subject}")
        granted = await self._approvals.check(
            ApprovalRequest(
                kind="host",
                subject=subject,
                detail=(
                    f"The sandboxed agent opened a TLS tunnel to {lowered} on port "
                    f"{port} ({pinned}). Approving lets it exchange any data with "
                    f"that host for the grant's lifetime; the contents are not "
                    f"inspected."
                ),
                ttl_seconds=self._ttl,
            )
        )
        if not granted:
            return _Decision(None, "operator declined or gate denied", _DENIED)
        return _Decision(pinned, "standing grant" if already else "operator approved")

    async def _resolve_public(self, host: str, port: int) -> str | None:
        """Resolve `host` and return one validated public address, else None.

        Two jobs beyond rejecting private space. First, `blocked_host` only
        inspects IP *literals*, so a hostname pointing into private space (a
        DNS-rebinding setup, or just `localhost`) would otherwise walk straight
        past it and reach a service on the parent's loopback.

        Second, the returned address is what the caller connects to, instead of
        letting `open_connection` perform its own second lookup. Check-then-use
        across two resolutions is the rebinding window itself: an attacker
        controlling DNS could answer publicly here and privately there. Every
        address is validated even after one passes, so a mixed public/private
        answer fails closed. Cert validation is unaffected because the client
        does its own TLS through the tunnel against the name it asked for.
        """
        # Deferred to avoid a module cycle: broker imports approvals, and this
        # module is imported from the same wiring point as broker.
        from claude_on_the_fly.broker import blocked_host

        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            # Fail closed: deferring to the connect would reintroduce the
            # second, unvalidated lookup this method exists to prevent.
            logger.warning(
                "%s: refuse %s:%d (DNS failed: %s)", self._tag, host, port, exc
            )
            return None
        addresses = [str(info[4][0]).split("%", 1)[0] for info in infos]
        # Every address, not just the pinned one: a mixed public/private answer
        # is what a rebinding attempt looks like, and the refusal below names
        # only the first offender.
        logger.debug("%s: %s resolved to %s", self._tag, host, addresses)
        pinned: str | None = None
        for address in addresses:
            if blocked_host(address):
                logger.warning(
                    "%s: refuse %s:%d (resolves to non-public %s)",
                    self._tag,
                    host,
                    port,
                    address,
                )
                return None
            if pinned is None:
                pinned = address
        if pinned is None:
            logger.warning(
                "%s: refuse %s:%d (no usable address)", self._tag, host, port
            )
        return pinned

    @staticmethod
    async def _refuse(
        writer: asyncio.StreamWriter, status: int, message: str, hint: str = ""
    ) -> None:
        """Write a refusal. `hint` replaces the reason phrase when given.

        Only the CONNECT gate passes one, and it has to: a client discards a CONNECT
        response body, so the reason phrase is the sole channel to the agent (see
        `_Refusal`). Every other status here answers an ordinary HTTP request, whose
        body the client does read, so they keep the plain phrase.
        """
        reason = hint or {
            400: "Bad Request",
            403: "Forbidden",
            405: "Method Not Allowed",
            414: "URI Too Long",
            502: "Bad Gateway",
        }.get(status, "Forbidden")
        body = message.encode()
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Content-Type: text/plain\r\n"
            "Connection: close\r\n\r\n".encode()
            + body
        )
        # The peer that triggered the refusal may already be gone; there is
        # nothing left to tell it.
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await writer.drain()

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    ) -> None:
        """Copy bytes both ways until either side closes."""
        upstream_reader, upstream_writer = upstream
        await asyncio.gather(
            self._pipe(client_reader, upstream_writer),
            self._pipe(upstream_reader, client_writer),
            return_exceptions=True,
        )
        upstream_writer.close()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(_CHUNK)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
