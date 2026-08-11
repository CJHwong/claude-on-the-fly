"""One contract, both platforms, executed against a real jail.

The macOS and Linux jails are built from different primitives: seatbelt layers
rules over the real filesystem, bubblewrap builds a mount namespace. Nothing
makes them agree except this file. Two profiles maintained side by side drift,
and the drift is invisible until someone reads both and notices, which is not a
control.

So the contract lives here as data, and each platform has to satisfy it by
whatever means it has. The cases say what the *operator* was promised -- this is
readable, that is not, this is writable, that is not -- and deliberately say
nothing about errno or mechanism, because those legitimately differ (EPERM and a
present-but-refused path on macOS, ENOENT/EROFS and an absent one on Linux).

Skipped where no jail can run, with a reason that names the missing piece. A
skipped parity suite must never read as a passing one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from claude_on_the_fly import sandbox

ALLOW, DENY = "allow", "deny"


def _why_not() -> str | None:
    import shutil

    if sys.platform.startswith("linux"):
        if not shutil.which("bwrap"):
            return "bubblewrap is not installed"
        probe = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "/bin/true"],
            capture_output=True,
        )
        return (
            None if probe.returncode == 0 else "unprivileged user namespaces unusable"
        )
    if sys.platform == "darwin":
        return None if shutil.which("sandbox-exec") else "sandbox-exec is missing"
    return f"no jail implementation for {sys.platform}"


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


@dataclass(frozen=True)
class Case:
    """One promise, in the operator's terms. `path` is a template over the
    fixture's directories; `read`/`write` are the expected outcomes, or None
    where the contract says nothing."""

    what: str
    path: str
    read: str | None = None
    write: str | None = None


# The contract. Every entry here is something docs/explanation/security-model.md
# or docs/how-to/enable-sandboxing.md tells an operator, so a failure means the
# documentation is now a lie on at least one platform.
CONTRACT = (
    # --- the workspace is the work surface ---
    Case("workspace file", "{project}/note.txt", read=ALLOW, write=ALLOW),
    Case("agent memory", "{memory}/recall.md", read=ALLOW, write=ALLOW),
    # --- credentials the agent has no business reading ---
    Case("cloud credentials", "{home}/.aws/credentials", read=DENY),
    Case("ssh private key", "{home}/.ssh/id_rsa", read=DENY),
    Case("forge token", "{home}/.config/gh/hosts.yml", read=DENY),
    Case("npm token", "{home}/.npmrc", read=DENY),
    # --- this daemon's own secrets, at the root and one level down ---
    # The nested case is not hypothetical: a backup taken before an edit lands in
    # exactly that shape, and so does anything a syncer drops beside it.
    Case("daemon .env", "{data}/.env", read=DENY),
    Case("daemon .env one level down", "{data}/memory/.env", read=DENY),
    Case("daemon conversation logs", "{data}/logs/chat.log", read=DENY),
    # --- config that decides what runs on a LATER turn ---
    # Each of these outlives the session, so a jailed turn writing one is how an
    # injected agent leaves itself standing orders.
    Case(
        "global claude config", "{home}/.claude/settings.json", read=ALLOW, write=DENY
    ),
    Case("project MCP servers", "{project}/.mcp.json", write=DENY),
    Case("project git hooks", "{project}/.git/hooks/pre-commit", write=DENY),
    Case("project git config", "{project}/.git/config", write=DENY),
    Case("project editor tasks", "{project}/.vscode/tasks.json", write=DENY),
    Case("project shell rc", "{project}/.bashrc", write=DENY),
    Case("project zsh rc", "{project}/.zshrc", write=DENY),
    Case("project git identity", "{project}/.gitconfig", write=DENY),
    # --- outside the workspace is not a write surface ---
    Case("home directory", "{home}/escape.txt", write=DENY),
    Case("shell rc in home", "{home}/.bashrc", write=DENY),
)


@pytest.fixture
def world(monkeypatch):
    """A populated home the jail can be pointed at, plus the daemon's data dir.

    Every path the contract mentions is created, because absent-versus-denied is
    the distinction these tests exist to keep honest: a read that fails because
    nothing is there proves nothing at all.

    HOME and TMPDIR are made siblings, and that is load-bearing rather than
    tidiness. The suite's usual fake home lives *under* $TMPDIR on macOS, and both
    seatbelt profiles grant `_TMPDIR` after denying `_HOME`; last-match-wins then
    hands the whole home back and every deny in this file silently passes as an
    allow. The first run of this suite failed ten cases for exactly that reason,
    which is a good demonstration of why the contract is executed rather than
    read.
    """
    # Not tmp_path. On macOS that lives under /private/var/folders, which
    # fs-deny-most.sb grants writes to outright (alongside /private/tmp), so every
    # write-deny case in this file passes as an allow and the suite cannot tell a
    # real gap from its own fixture. Rooting under the invoking user's cache dir
    # puts the world outside both that grant and $TMPDIR. Removed at teardown.
    import pwd
    import shutil as _shutil

    from claude_on_the_fly import agent

    root = (
        Path(pwd.getpwuid(os.getuid()).pw_dir)
        / ".cache"
        / "cotf-parity"
        / str(os.getpid())
    )
    home = root / "home"
    tmpdir = root / "tmp"
    for directory in (home, tmpdir):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    data = home / ".claude-on-the-fly"
    monkeypatch.setattr(agent, "DATA_DIR", data)
    monkeypatch.setattr(agent, "MEMORY_DIR", data / "memory")
    home = Path(os.path.realpath(home))
    data = Path(os.path.realpath(data)) if data.exists() else data
    project = data / "workspaces" / "parity"
    memory = data / "memory"
    for directory in (
        project / ".git" / "hooks",
        project / ".vscode",
        memory,
        data / "logs",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    seed = {
        home / ".aws/credentials": "aws_secret_access_key=PARITY\n",
        # Not a PEM header: the fixture only needs a non-empty file at the path the
        # profile denies, and a realistic-looking one trips the detect-private-key
        # hook on every commit.
        home / ".ssh/id_rsa": "PARITY fake key material\n",
        home / ".config/gh/hosts.yml": "oauth_token: PARITY\n",
        home / ".npmrc": "//registry:_authToken=PARITY\n",
        home / ".bashrc": "export PARITY=1\n",
        home / ".claude/settings.json": "{}\n",
        data / ".env": "TELEGRAM_BOT_TOKEN=PARITY\n",
        data / "memory/.env": "TELEGRAM_BOT_TOKEN=PARITY\n",
        data / "logs/chat.log": "PARITY transcript\n",
        memory / "recall.md": "remembered\n",
        project / "note.txt": "work\n",
        project / ".mcp.json": "{}\n",
        project / ".git/hooks/pre-commit": "#!/bin/sh\n",
        project / ".git/config": "[core]\n",
        project / ".vscode/tasks.json": "{}\n",
        project / ".bashrc": "export X=1\n",
        project / ".zshrc": "export X=1\n",
        project / ".gitconfig": "[user]\n",
    }
    for path, body in seed.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    monkeypatch.setenv("COTF_SANDBOX", "jail")
    # deny-most is the only shape Linux has, so parity is only meaningful against
    # the macOS profile that matches it.
    monkeypatch.setenv("COTF_SANDBOX_FS", "deny-most")
    yield {
        "home": Path(os.path.realpath(home)),
        "data": Path(os.path.realpath(data)),
        "project": Path(os.path.realpath(project)),
        "memory": Path(os.path.realpath(memory)),
    }
    _shutil.rmtree(root, ignore_errors=True)


def _run(argv: list[str], project: Path) -> int:
    proc = subprocess.run(
        sandbox.wrap(argv, project),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode


def _can_read(path: str, project: Path) -> bool:
    return _run(["/bin/cat", path], project) == 0


def _can_write(path: str, project: Path) -> bool:
    return _run(["/bin/sh", "-c", f"echo parity >> {path}"], project) == 0


@pytest.mark.parametrize("case", CONTRACT, ids=lambda c: c.what)
def test_contract_holds_on_this_platform(case, world):
    path = case.path.format(**{k: str(v) for k, v in world.items()})
    project = world["project"]
    failures = []
    if case.read is not None:
        got = ALLOW if _can_read(path, project) else DENY
        if got != case.read:
            failures.append(f"read expected {case.read}, got {got}")
    if case.write is not None:
        got = ALLOW if _can_write(path, project) else DENY
        if got != case.write:
            failures.append(f"write expected {case.write}, got {got}")
    assert not failures, f"{case.what} ({path}): " + "; ".join(failures)


def test_the_agents_own_loopback_still_works(world):
    """Both platforms keep this: the agent runs dev servers and tests. macOS
    allows every loopback port by default, Linux gives the namespace its own."""
    probe = (
        "import socket,threading,sys\n"
        "srv=socket.socket(); srv.bind(('127.0.0.1',0)); srv.listen(1)\n"
        "threading.Thread(target=lambda: srv.accept()[0].send(b'OWN'),daemon=True).start()\n"
        "s=socket.create_connection(('127.0.0.1',srv.getsockname()[1]),5)\n"
        "sys.stdout.write(s.recv(8).decode())\n"
    )
    assert _run([sys.executable, "-c", probe], world["project"]) == 0


def test_the_internet_is_not_reachable_directly(world):
    """The load-bearing claim: the egress proxy cannot be bypassed."""
    probe = "import socket,sys\nsocket.create_connection(('1.1.1.1',443),5)\n"
    assert _run([sys.executable, "-c", probe], world["project"]) != 0


async def test_ssh_agent_is_not_reachable(world):
    """SSH_AUTH_SOCK is forwarded to the agent on both platforms, and on neither
    should the socket behind it be usable: it signs as the operator.

    macOS gets this for free because seatbelt permits no unix socket at all. On
    Linux the socket is a real path, so it depends on where it lives -- and
    $TMPDIR is a read-write bind, which is exactly where OpenSSH puts it.
    """
    sock = os.environ.get("SSH_AUTH_SOCK")
    if not sock:
        pytest.skip("no ssh-agent in this environment")
    probe = f"import socket,sys\ns=socket.socket(socket.AF_UNIX); s.connect({sock!r})\n"
    assert _run([sys.executable, "-c", probe], world["project"]) != 0


def test_the_jail_can_run_a_backend_installed_under_home(world):
    """The gap a code review caught and this suite had not: every profile makes
    $HOME opaque, and both the agent binary and the interpreter routinely live
    there (npm global, uv virtualenv). Measured before the fix: a backend under
    ~/.local/bin exited 126, and macOS refused the venv interpreter with rc 71,
    which made the startup egress probe block the daemon outright."""
    binary = world["home"] / ".local" / "bin" / "fake-backend"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\necho BACKEND_RAN\n")
    binary.chmod(0o755)
    project = world["project"]
    proc = subprocess.run(
        sandbox.wrap([str(binary)], project), capture_output=True, text=True, timeout=60
    )
    assert "BACKEND_RAN" in proc.stdout, proc.stderr[:300]


def test_the_jail_can_run_the_interpreter_it_was_started_from(world):
    """preflight's egress probe needs this, and it is the check that turns a
    misconfigured jail into a refused startup rather than a silent one."""
    proc = subprocess.run(
        sandbox.wrap(
            [sys.executable, "-c", "print('INTERPRETER_RAN')"], world["project"]
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "INTERPRETER_RAN" in proc.stdout, proc.stderr[:300]
