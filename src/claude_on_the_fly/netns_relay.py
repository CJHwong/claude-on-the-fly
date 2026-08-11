"""The agent's only route out of the Linux jail's network namespace.

`--unshare-net` gives the agent a fresh network stack: `lo` comes up on its own,
so its own dev servers and tests still work, but the host's loopback is refused
and the internet is unreachable. That is the whole egress guarantee, and it is
also a problem, because every service the agent legitimately needs lives on the
host's loopback: the credential broker, the CONNECT egress proxy, the command
broker, and the approval service.

This module is the bridge, and it is deliberately the *only* one. Two ends that
must agree on one convention:

  * Host side (`LoopbackRelay`), in the daemon's event loop. One unix socket per
    brokered port, each forwarding to `127.0.0.1:<port>` on the host.
  * Namespace side (`python -m claude_on_the_fly.netns_relay`), spawned inside
    the jail. Binds `127.0.0.1:<port>` -- the *same* port -- forwards to the
    bound-in unix socket, then supervises the real agent.

Same port on both ends is what lets `*_BASE_URL`, `HTTPS_PROXY`,
`COTF_CMD_ENDPOINT` and `COTF_APPROVE_URL` keep the values the broker already
published. Nothing in broker.py, egress.py, commands.py or approvals.py knows
this module exists.

Why a unix socket rather than a port mapper like pasta: a socket is a file, so
the mount namespace scopes it exactly, and the design fails *closed*. If this
relay is broken or absent the agent gets connection-refused and says so. A port
mapper's job is to grant connectivity, so its failure mode is an agent with a
working route to the internet, which is the one outcome the jail exists to
prevent.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# Copy chunk size. Matches the CONNECT proxy's, so a tunnelled body moves through
# both hops in the same-sized pieces.
_CHUNK = 64 * 1024


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction until EOF, then half-close so the peer sees it.

    A relay that never half-closes turns every request into a hang: the upstream
    is waiting for a body terminator the downstream already stopped sending.
    """
    try:
        while chunk := await reader.read(_CHUNK):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(ConnectionError, OSError):
            if writer.can_write_eof():
                writer.write_eof()


async def _splice(
    client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
) -> None:
    """Run both directions until both finish, then close both sides."""
    client_reader, client_writer = client
    up_reader, up_writer = upstream
    try:
        await asyncio.gather(
            _pump(client_reader, up_writer),
            _pump(up_reader, client_writer),
        )
    finally:
        for writer in (client_writer, up_writer):
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()


class LoopbackRelay:
    """Host end: unix sockets that forward to the daemon's own loopback ports.

    Lives as long as the session that owns it. Sockets go under a per-session
    directory so two concurrent sessions cannot reach each other's services,
    which matters because the egress proxy is per-session and a grant approved
    in one chat must not be usable from another.
    """

    def __init__(self, socket_dir: Path) -> None:
        self.socket_dir = socket_dir
        self._servers: list[asyncio.Server] = []
        # Connections still in flight. asyncio spawns a task per accepted
        # connection and does not track them, so closing the servers alone leaves
        # them pending: the turn ends, the loop tears down, and every open tunnel
        # prints "Task was destroyed but it is pending!". Observed on the first
        # live codex run, once per in-flight request.
        self._live: set[asyncio.Task] = set()
        self.sockets: dict[int, Path] = {}

    async def start(self, ports: Iterable[int]) -> dict[int, Path]:
        """Listen on one unix socket per port. Returns port -> host socket path."""
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        for port in sorted(set(ports)):
            path = self.socket_dir / f"{port}.sock"
            # A stale socket from a killed daemon makes bind fail with EADDRINUSE
            # even though nothing is listening.
            path.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(
                self._handler(port), path=str(path)
            )
            # The agent runs as the same user, so owner-only is both sufficient
            # and the tightest thing that still works.
            path.chmod(0o600)
            self._servers.append(server)
            self.sockets[port] = path
        logger.info(
            "sandbox: relay listening for %d brokered port(s) %s under %s",
            len(self.sockets),
            sorted(self.sockets),
            self.socket_dir,
        )
        return dict(self.sockets)

    def _handler(self, port: int):
        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            task = asyncio.current_task()
            if task is not None:
                self._live.add(task)
                task.add_done_callback(self._live.discard)
            try:
                upstream = await asyncio.open_connection("127.0.0.1", port)
            except OSError as exc:
                # The brokered service died or never bound. Say which, because
                # from inside the jail this is indistinguishable from the jail
                # itself being misconfigured.
                logger.warning(
                    "sandbox: relay cannot reach 127.0.0.1:%d (%s)", port, exc
                )
                writer.close()
                return
            await _splice((reader, writer), upstream)

        return handle

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
        # Cancel BEFORE wait_closed, not after. Since 3.12 `Server.wait_closed()`
        # blocks until every active handler has finished, so with an open tunnel
        # the two waits deadlock: wait_closed is waiting for the handler and the
        # handler is waiting for a peer that has gone. Cancel rather than await
        # for the same reason -- the agent is already gone, so anything still open
        # is a tunnel with no reader.
        #
        # Snapshot first: each task's done callback discards it from _live, so
        # iterating the live set while awaiting it mutates it underneath.
        pending = list(self._live)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._live.clear()
        for server in self._servers:
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for path in self.sockets.values():
            path.unlink(missing_ok=True)
        self._servers.clear()
        self.sockets.clear()


async def _serve_inside(mapping: Mapping[int, str]) -> list[asyncio.Server]:
    """Namespace end: bind each port on the namespace's own loopback."""
    servers: list[asyncio.Server] = []
    for port, socket_path in sorted(mapping.items()):

        def handler(sock: str):
            async def handle(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                try:
                    upstream = await asyncio.open_unix_connection(path=sock)
                except OSError as exc:
                    logger.warning(
                        "sandbox: relay socket %s unavailable (%s)", sock, exc
                    )
                    writer.close()
                    return
                await _splice((reader, writer), upstream)

            return handle

        servers.append(
            await asyncio.start_server(handler(socket_path), "127.0.0.1", port)
        )
    return servers


async def _supervise(
    argv: list[str], signals: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT)
) -> int:
    """Run the agent as a child and return its exit code.

    A child rather than an `exec` because the relay has to stay alive underneath
    it, and `exec` would replace this process and take the servers with it. That
    makes this a supervisor on the critical path of every turn, so it does the
    three things a supervisor owes its child: inherit stdio untouched (the
    backends parse that stream), forward the signals a daemon actually sends,
    and propagate the real exit code rather than inventing one.
    """
    proc = await asyncio.create_subprocess_exec(*argv)
    loop = asyncio.get_running_loop()

    def forward(signum: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signum)

    installed: list[int] = []
    for signum in signals:
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, forward, signum)
            installed.append(signum)
    try:
        return await proc.wait()
    finally:
        # Loop signal handlers are process-global and outlive this call. Leaving
        # them behind means a supervisor that has already returned still owns the
        # daemon's SIGINT, forwarding it to a pid that no longer exists.
        for signum in installed:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)


def _parse(argv: list[str]) -> tuple[dict[int, str], list[str]]:
    parser = argparse.ArgumentParser(prog="netns_relay", add_help=False)
    parser.add_argument("--map", action="append", default=[], metavar="PORT=SOCKET")
    known, rest = parser.parse_known_args(argv)
    if rest and rest[0] == "--":
        rest = rest[1:]
    mapping: dict[int, str] = {}
    for entry in known.map:
        port, _, socket_path = entry.partition("=")
        mapping[int(port)] = socket_path
    return mapping, rest


async def _main(argv: list[str]) -> int:
    mapping, command = _parse(argv)
    if not command:
        print("netns_relay: no command to run", file=sys.stderr)
        return 2
    servers = await _serve_inside(mapping)
    try:
        return await _supervise(command)
    finally:
        for server in servers:
            server.close()


def main() -> int:  # pragma: no cover - process entry point, covered end-to-end
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    return asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
