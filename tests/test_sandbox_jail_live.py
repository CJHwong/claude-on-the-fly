"""The Linux jail's contract, asserted against a real bubblewrap jail.

Everything else about sandbox_linux is a pure function producing argv, and argv
is not a boundary. These tests run the argv. They are the only place that answers
"does the kernel actually refuse this", which is the question the whole module
exists to make true.

Skipped wherever the mechanism is absent, and the skip reason says which piece is
missing rather than reporting a pass. A machine without unprivileged user
namespaces cannot run a jail at all, and quietly counting that as green is the
failure mode this suite is guarding against everywhere else.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from claude_on_the_fly import netns_relay, sandbox, sandbox_linux


def _why_not() -> str | None:
    if not sys.platform.startswith("linux"):
        return "the bubblewrap jail is Linux-only"
    import shutil

    if not shutil.which("bwrap"):
        return "bubblewrap is not installed"
    probe = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "/bin/true"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        # Not necessarily user namespaces: on a host with the Ubuntu AppArmor
        # restriction the namespace is created and then netlink is refused
        # ("Failed RTM_NEWADDR"), which is a different remedy. Report what
        # bwrap actually said rather than naming a cause.
        return f"bubblewrap cannot start a jail here: {probe.stderr.strip()[:160]}"
    return None


# Set by CI. A skipped boundary suite reads exactly like a passing one, so where
# the jail is *expected* to work, its absence has to be an error rather than a
# skip. Raising at import fails collection loudly and names the missing piece.
#
# Scoped to the mechanism, deliberately. An individual case may still skip for a
# reason that says nothing about the boundary -- there is no ssh-agent to test
# against on a CI runner -- and an earlier version of this guard grepped the log
# for "skipped", which could not tell those apart and failed a run where all 29
# real assertions had passed.
_REASON = _why_not()
if _REASON is not None and os.environ.get("COTF_REQUIRE_JAIL"):
    raise RuntimeError(f"COTF_REQUIRE_JAIL is set but {_REASON}")

pytestmark = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")


@pytest.fixture
def jail(tmp_path):
    """A workspace plus the grant set the real profile uses, minus the daemon."""
    home = tmp_path / "home"
    data = home / ".claude-on-the-fly"
    workspace = data / "workspaces" / "proj"
    workspace.mkdir(parents=True)
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("aws_secret_access_key=LIVE\n")
    (data / ".env").write_text("TELEGRAM_BOT_TOKEN=xxx\n")
    (data / "memory").mkdir()
    places = sandbox_linux.prepare_placeholders(data / "jail")
    return {
        "home": home,
        "data": data,
        "workspace": workspace,
        "grants": {
            "opaque": [home, data],
            "read_only": [*sandbox._runtime_read_paths([sys.executable])],
            "read_write": [workspace, data / "memory"],
            "write_denied": [workspace / ".mcp.json"],
            "masked": [],
        },
        "places": places,
    }


async def run_async(jail, shell: str, sockets=None) -> str:
    """Async spawn, for tests whose relay lives on this event loop.

    `subprocess.run` would block it, so the LoopbackRelay could never service the
    connection the jailed process is making and the probe would time out looking
    exactly like a boundary denial.
    """
    argv = sandbox_linux.jail_argv(
        ["/bin/sh", "-c", shell],
        **jail["grants"],
        sockets=sockets or {},
        placeholders=jail["places"],
    )
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    return (out + err).decode()


def run(jail, shell: str, sockets=None) -> subprocess.CompletedProcess:
    argv = sandbox_linux.jail_argv(
        ["/bin/sh", "-c", shell],
        **jail["grants"],
        sockets=sockets or {},
        placeholders=jail["places"],
    )
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def test_a_credential_outside_the_grant_set_is_unreachable(jail):
    out = run(jail, f"cat {jail['home']}/.aws/credentials")
    assert out.returncode != 0
    assert "LIVE" not in out.stdout


def test_the_daemons_own_env_stays_hidden_beneath_an_opaque_data_dir(jail):
    """memory/ is granted back at greater depth; the sibling .env must not be."""
    assert "TELEGRAM_BOT_TOKEN" not in run(jail, f"cat {jail['data']}/.env").stdout


def test_the_workspace_is_writable_and_everywhere_else_is_not(jail):
    assert (
        run(jail, f"echo ok > {jail['workspace']}/f.txt && echo WROTE").returncode == 0
    )
    outside = run(jail, f"echo x > {jail['home']}/escape.txt")
    assert outside.returncode != 0
    # A tmpfs is writable by default, so this is the --remount-ro pass working.
    assert "Read-only file system" in outside.stderr


def test_an_absent_write_deny_cannot_be_created(jail):
    sandbox_linux.ensure_write_deny_targets(
        jail["grants"]["write_denied"], jail["places"]
    )
    denied = run(jail, f"echo evil > {jail['workspace']}/.mcp.json")
    assert denied.returncode != 0
    assert "Read-only file system" in denied.stderr


def test_the_internet_is_unreachable_without_the_relay(jail):
    probe = (
        "import socket,sys\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1',443),5); sys.stdout.write('REACHED')\n"
        "except OSError: sys.stdout.write('BLOCKED')\n"
    )
    assert "BLOCKED" in run(jail, f'{sys.executable} -c "{probe}"').stdout


def test_the_agents_own_loopback_still_works(jail):
    """lo comes up on its own inside the namespace, so dev servers and tests keep
    working -- the capability the macOS default (`localhost:*`) preserves."""
    probe = (
        "import socket,threading,sys\n"
        "srv=socket.socket(); srv.bind(('127.0.0.1',0)); srv.listen(1)\n"
        "threading.Thread(target=lambda: srv.accept()[0].send(b'OWN'),daemon=True).start()\n"
        "s=socket.create_connection(('127.0.0.1',srv.getsockname()[1]),5)\n"
        "sys.stdout.write(s.recv(8).decode())\n"
    )
    assert "OWN" in run(jail, f'{sys.executable} -c "{probe}"').stdout


async def test_the_relay_is_the_only_way_to_a_host_service(jail):
    async def handle(reader, writer):
        writer.write(b"HOSTSVC")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    probe = (
        "import socket,sys\n"
        "try:\n"
        f"    s=socket.create_connection(('127.0.0.1',{port}),5)\n"
        "    sys.stdout.write(s.recv(16).decode())\n"
        "except OSError as e: sys.stdout.write('BLOCKED:%s' % e)\n"
    )
    command = f'{sys.executable} -c "{probe}"'
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        relay = netns_relay.LoopbackRelay(Path(short))
        sockets = await relay.start([port])
        try:
            jail["grants"]["read_only"].append(Path(sys.prefix))
            assert "HOSTSVC" in await run_async(jail, command, sockets=sockets)
            assert "BLOCKED" in await run_async(jail, command)
        finally:
            await relay.stop()
            server.close()


async def test_verify_denials_reports_denied_not_absent(jail, monkeypatch):
    """The bug this suite was written to catch. bubblewrap hides a path by not
    mounting it, so a denied read reports "No such file or directory" -- identical
    to a file that was never there. Classifying on the message would file every
    hidden credential as ABSENT, and ABSENT proves nothing."""
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    # The probes read the real home, not this fixture's. Absent-versus-denied is
    # settled outside the jail, so a spec that is not on disk is answered ABSENT
    # without proving anything -- put one there.
    for spec in sandbox._deny_probe_specs():
        target = Path(os.path.expanduser(spec))
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("probe fixture\n")
    outcomes = await sandbox.verify_denials(jail["workspace"])
    assert sandbox.READABLE not in outcomes.values()
    assert sandbox.DENIED in outcomes.values(), (
        "a real credential path must test as DENIED"
    )


async def test_the_group_kill_still_reaches_the_agent_through_the_jail(
    jail, monkeypatch
):
    """Security finding `normal-exit-descendant-survival`: an agent process group
    must be reaped whole, so no descendant outlives the turn.

    The Linux jail puts two processes between the daemon and the agent (bwrap,
    then the relay launcher), and the control depends on all three sharing the
    process group the daemon kills. That holds only because nothing here passes
    `--new-session` or `--unshare-pid`; either would move the agent out of reach
    of `killpg` and the daemon would go on believing it had cleaned up.
    """
    from claude_on_the_fly import agent, netns_relay, sandbox

    monkeypatch.setenv("COTF_SANDBOX", "jail")
    monkeypatch.setattr(sandbox, "_platform", lambda: "linux")
    workspace = jail["workspace"]
    pidfile = workspace / "inner.pid"

    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        relay = netns_relay.LoopbackRelay(Path(short))
        sockets = await relay.start([19556])
        token = sandbox._SESSION_SOCKETS.set(sockets)
        try:
            inner = (
                f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid()));"
                " time.sleep(600)"
            )
            argv = sandbox.wrap([sys.executable, "-c", inner], workspace)
            assert any("netns_relay" in a for a in argv), (
                "launcher missing; proves nothing"
            )

            proc = await asyncio.create_subprocess_exec(*argv, start_new_session=True)
            for _ in range(100):
                if pidfile.exists() and pidfile.read_text().strip():
                    break
                await asyncio.sleep(0.1)
            inner_pid = int(pidfile.read_text().strip())
            os.kill(inner_pid, 0)  # raises if the agent never came up

            await agent._kill_process_tree(proc)
            await asyncio.sleep(0.5)
            with pytest.raises(ProcessLookupError):
                os.kill(inner_pid, 0)
        finally:
            sandbox._SESSION_SOCKETS.reset(token)
            await relay.stop()
