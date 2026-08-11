"""netns_relay: the agent's only route out of the Linux jail's netns.

Exercised against real sockets rather than mocks. This sits on the critical path
of every jailed turn, and the failures worth catching here -- a direction that
never half-closes, a handler task left pending at teardown -- are exactly the
ones a mocked stream would not reproduce.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import tempfile
from pathlib import Path

import pytest

from claude_on_the_fly import netns_relay


@pytest.fixture
def sockdir():
    """A short path: sun_path caps out around 104 bytes, and pytest's tmp_path on
    macOS is long enough to blow that on its own."""
    with tempfile.TemporaryDirectory(dir="/tmp") as name:
        yield Path(name)


async def _echo_server(banner: bytes):
    async def handle(reader, writer):
        data = await reader.read(64)
        writer.write(banner + data)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


# --- host side ---


async def test_relay_forwards_a_unix_connection_to_the_host_port(sockdir):
    server = await _echo_server(b"HOST:")
    port = server.sockets[0].getsockname()[1]
    relay = netns_relay.LoopbackRelay(sockdir)
    sockets = await relay.start([port])
    try:
        reader, writer = await asyncio.open_unix_connection(str(sockets[port]))
        writer.write(b"ping")
        await writer.drain()
        assert await reader.read(64) == b"HOST:ping"
        writer.close()
    finally:
        await relay.stop()
        server.close()


async def test_sockets_are_owner_only_and_named_for_their_port(sockdir):
    relay = netns_relay.LoopbackRelay(sockdir)
    sockets = await relay.start([19001, 19002])
    try:
        assert sorted(sockets) == [19001, 19002]
        for port, path in sockets.items():
            assert path.name == f"{port}.sock"
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        await relay.stop()


async def test_a_stale_socket_from_a_killed_daemon_does_not_block_bind(sockdir):
    """Nothing is listening on it, but bind still fails with EADDRINUSE."""
    (sockdir / "19003.sock").write_text("")
    relay = netns_relay.LoopbackRelay(sockdir)
    try:
        assert await relay.start([19003])
    finally:
        await relay.stop()


async def test_stop_removes_the_sockets_and_forgets_them(sockdir):
    relay = netns_relay.LoopbackRelay(sockdir)
    sockets = await relay.start([19004])
    path = sockets[19004]
    await relay.stop()
    assert not path.exists()
    assert relay.sockets == {}


async def test_an_unreachable_brokered_service_is_named_not_swallowed(sockdir, caplog):
    """From inside the jail a dead service and a misconfigured jail look
    identical, so the daemon side has to say which it was."""
    relay = netns_relay.LoopbackRelay(sockdir)
    sockets = await relay.start([1])  # nothing listens on port 1
    try:
        with caplog.at_level("WARNING", logger="claude_on_the_fly.netns_relay"):
            reader, writer = await asyncio.open_unix_connection(str(sockets[1]))
            assert await reader.read(16) == b""
            writer.close()
            await asyncio.sleep(0.05)
        assert "cannot reach 127.0.0.1:1" in "\n".join(
            r.getMessage() for r in caplog.records
        )
    finally:
        await relay.stop()


async def test_stop_cancels_a_connection_still_in_flight(sockdir):
    """Observed on the first live codex run: closing the servers alone left one
    "Task was destroyed but it is pending!" per open tunnel, because asyncio does
    not track the task it spawns per connection."""
    idle = await asyncio.start_server(lambda r, w: asyncio.sleep(300), "127.0.0.1", 0)
    port = idle.sockets[0].getsockname()[1]
    relay = netns_relay.LoopbackRelay(sockdir)
    sockets = await relay.start([port])
    _reader, writer = await asyncio.open_unix_connection(str(sockets[port]))
    writer.write(b"hold open")
    await writer.drain()
    await asyncio.sleep(0.05)
    assert relay._live, "handler task should be tracked while the tunnel is open"
    await relay.stop()
    assert relay._live == set()
    writer.close()
    idle.close()


# --- namespace side ---


async def test_inside_listener_bridges_the_port_to_the_bound_in_socket(sockdir):
    server = await _echo_server(b"VIA:")
    host_port = server.sockets[0].getsockname()[1]
    relay = netns_relay.LoopbackRelay(sockdir)
    sockets = await relay.start([host_port])
    inside = await netns_relay._serve_inside({host_port + 1: str(sockets[host_port])})
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", host_port + 1)
        writer.write(b"hop")
        await writer.drain()
        assert await reader.read(64) == b"VIA:hop"
        writer.close()
    finally:
        for srv in inside:
            srv.close()
        await relay.stop()
        server.close()


async def test_a_missing_relay_socket_is_reported_not_hung(sockdir, caplog):
    inside = await netns_relay._serve_inside({19010: str(sockdir / "absent.sock")})
    try:
        with caplog.at_level("WARNING", logger="claude_on_the_fly.netns_relay"):
            reader, writer = await asyncio.open_connection("127.0.0.1", 19010)
            assert await reader.read(16) == b""
            writer.close()
            await asyncio.sleep(0.05)
        assert "unavailable" in "\n".join(r.getMessage() for r in caplog.records)
    finally:
        for srv in inside:
            srv.close()


# --- supervisor ---


async def test_supervise_propagates_the_childs_exit_code():
    assert (
        await netns_relay._supervise([sys.executable, "-c", "raise SystemExit(7)"]) == 7
    )
    assert await netns_relay._supervise(["/bin/sh", "-c", "exit 0"]) == 0


async def test_supervise_forwards_a_signal_to_the_child():
    """The relay cannot exec the agent -- that would replace this process and take
    the listeners with it -- so it owes the child the signals a daemon sends.

    SIGUSR1 rather than the real SIGTERM/SIGINT: this raises the signal in the
    test process itself, and a stray SIGINT here takes pytest down with it.
    """
    task = asyncio.create_task(
        netns_relay._supervise(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            signals=(signal.SIGUSR1,),
        )
    )
    await asyncio.sleep(0.4)
    signal.raise_signal(signal.SIGUSR1)
    assert await asyncio.wait_for(task, timeout=10) != 0


async def test_supervise_does_not_keep_the_signal_handler_afterwards():
    """Loop signal handlers are process-global. A supervisor that has returned
    must not still own the daemon's SIGINT, forwarding it to a dead pid."""
    loop = asyncio.get_running_loop()
    await netns_relay._supervise(["/bin/sh", "-c", "exit 0"], signals=(signal.SIGUSR2,))
    # remove_signal_handler returns False when nothing was installed.
    assert loop.remove_signal_handler(signal.SIGUSR2) is False


# --- argv parsing ---


@pytest.mark.parametrize(
    ("argv", "mapping", "command"),
    [
        (
            ["--map", "1=/a.sock", "--", "codex", "exec"],
            {1: "/a.sock"},
            ["codex", "exec"],
        ),
        (
            ["--map", "1=/a.sock", "--map", "2=/b.sock", "--", "x"],
            {1: "/a.sock", 2: "/b.sock"},
            ["x"],
        ),
        (["--", "bare"], {}, ["bare"]),
    ],
)
def test_parse_splits_the_mapping_from_the_command(argv, mapping, command):
    assert netns_relay._parse(argv) == (mapping, command)


async def test_no_command_is_an_error_not_a_silent_success(capsys):
    assert await netns_relay._main(["--map", "1=/a.sock"]) == 2
    assert "no command" in capsys.readouterr().err


async def test_main_runs_the_command_and_tears_the_listeners_down(sockdir):
    code = await netns_relay._main(
        ["--map", f"19020={sockdir / 'x.sock'}", "--", "/bin/sh", "-c", "exit 3"]
    )
    assert code == 3
    # The port is free again, so the servers really were closed.
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 19020)
    probe.close()


# --- stream copying ---


async def test_pump_half_closes_so_the_peer_sees_eof(sockdir):
    """A relay that never half-closes turns every request into a hang: the
    upstream waits for a terminator the downstream already stopped sending."""
    server = await _echo_server(b"E:")
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    source = asyncio.StreamReader()
    source.feed_data(b"body")
    source.feed_eof()
    await netns_relay._pump(source, writer)
    assert await reader.read(64) == b"E:body"
    writer.close()
    server.close()


async def test_pump_survives_a_writer_that_is_already_gone():
    server = await _echo_server(b"")
    port = server.sockets[0].getsockname()[1]
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.close()
    await writer.wait_closed()
    source = asyncio.StreamReader()
    source.feed_data(b"into the void")
    source.feed_eof()
    await netns_relay._pump(source, writer)  # must not raise
    server.close()
