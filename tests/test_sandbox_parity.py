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
    # --- the daemons' own bookkeeping ---
    # The journal is the sharp one, and the write side is sharper than the read
    # side: its entries are replayed as user messages at the next start, so a
    # jailed turn that could write one would be scheduling a prompt for itself
    # past whatever approval the operator set. The session map and the event log
    # describe every *other* conversation the daemon serves, which is the same
    # boundary `sandbox.scope_sessions` draws between sessions.
    Case(
        "pending-turn journal", "{data}/state/slack.turns.json", read=DENY, write=DENY
    ),
    Case("session map", "{data}/state/telegram-sessions.json", read=DENY, write=DENY),
    Case("daemon event log", "{data}/state/events.jsonl", read=DENY, write=DENY),
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
        data / "state",
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
        # Seeded for the same reason as everything else here: a read that fails
        # because nothing is there proves nothing about the boundary.
        data / "state/slack.turns.json": "[]\n",
        data / "state/telegram-sessions.json": "{}\n",
        data / "state/events.jsonl": "{}\n",
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


def test_git_can_start_a_repository_inside_the_workspace(world):
    """The workspace is a work surface for git, not only for `cat` and `echo`.

    git canonicalizes its cwd on every command, which stats each directory from
    the root down. Under an opaque $HOME that walk used to die at the home
    directory on macOS, so no git command worked inside the workspace at all.
    """
    project = world["project"]
    proc = subprocess.run(
        sandbox.wrap(
            [
                "/bin/sh",
                "-c",
                f"git init -q {project}/fresh && git -C {project}/fresh status --porcelain",
            ],
            project,
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_the_ancestor_grant_is_metadata_on_the_path_and_nothing_beside_it(world):
    project, home = world["project"], world["home"]
    # Each directory on the way down is visible to stat()...
    assert (
        _run(
            ["/bin/sh", "-c", f"test -d {home} && test -d {home}/.claude-on-the-fly"],
            project,
        )
        == 0
    )
    # ...a sibling that is not on the path is not. (A listing of the home is not
    # part of the contract: Linux mounts an empty tmpfs there, which lists fine
    # and shows nothing, while seatbelt refuses the readdir outright.)
    assert _run(["/bin/sh", "-c", f"test -e {home}/.ssh"], project) != 0


# The daemons' bookkeeping, as the paths the state cases above use.
_STATE_FILES = (
    "state/slack.turns.json",
    "state/telegram-sessions.json",
    "state/events.jsonl",
)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="allow-reads is a macOS-only shape"
)
@pytest.mark.parametrize("name", _STATE_FILES)
def test_the_state_dir_is_denied_under_allow_reads_too(name, world, monkeypatch):
    """The contract above runs under deny-most, where the whole data dir is
    opaque and this holds for free. `allow-reads` is the default and the opposite
    shape -- read everything, deny a list -- so the rule that covers `state/`
    there is the one that can be missing, and was.

    Write is asserted alongside read because the write is the dangerous half: a
    journal entry is replayed as a user message at the next start.
    """
    monkeypatch.setenv("COTF_SANDBOX_FS", "allow-reads")
    path = str(world["data"] / name)
    project = world["project"]

    assert not _can_read(path, project), f"{name} is readable under allow-reads"
    assert not _can_write(path, project), f"{name} is writable under allow-reads"


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


def test_an_entry_the_codex_home_links_out_to_is_readable(world, monkeypatch):
    """Sharing one set of skills between backends is done with a symlink out of
    ~/.codex, and both jails have to see through it.

    Neither does so for free. Seatbelt matches the path the kernel resolves, so
    the grant on the codex home covers the link and not the file; bubblewrap
    mounts, so the target dangles unless it is mounted too. Both report the file
    as missing rather than as denied, which is how this survived two readings of
    the profile. Measured before the fix: codex exited 1 with "Operation not
    permitted (os error 1)" naming no path, and the kernel logged
    `deny(1) file-read-data .../.agents/agents`.

    `skills` rather than `agents`, though `agents` is what found it. A protected
    entry cannot be a symlink on Linux at all -- bwrap answers "Can't mount on
    symlink destination" and the preflight refuses the layout outright, which is
    a deliberate platform difference and has its own test. `skills` is not
    protected (codex writes its own tree inside it), so it is the entry where
    both platforms promise the same thing.
    """
    home, project = world["home"], world["project"]
    shared = home / ".agents" / "skills"
    shared.mkdir(parents=True)
    (shared / "review.md").write_text("how to review\n")
    # Same parent, not linked from the codex home. The grant is for what the
    # operator wired in, not for the tree that happens to contain it.
    beside = home / ".agents" / "private"
    beside.mkdir()
    (beside / "secret.txt").write_text("PARITY\n")
    codex = home / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    (codex / "skills").symlink_to(shared)
    # conftest pins CODEX_HOME at its own tmp home so no test can touch the real
    # ~/.codex, and that pin wins over the redirected HOME. Point it at this
    # world's copy, or the grant is computed for a directory nothing here made.
    monkeypatch.setenv("CODEX_HOME", str(codex))

    assert _can_read(str(shared / "review.md"), project)
    assert not _can_read(str(beside / "secret.txt"), project)


def _grant_with_a_repo(world, monkeypatch) -> dict[str, Path]:
    """A write grant laid out the way a deployment keeps one: a repository at
    the grant, another one level down, and the data dir's schedule linked into
    it."""
    home, data = world["home"], world["data"]
    soul = home / "soul"
    for repo in (soul, soul / "tool"):
        (repo / ".git" / "hooks").mkdir(parents=True)
        (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
        (repo / ".git" / "config").write_text("[core]\n")
    (soul / "config").mkdir()
    schedule = soul / "config" / "cotf-cron.yaml"
    schedule.write_text("jobs: []\n")
    (data / "cron.yaml").symlink_to(schedule)
    monkeypatch.setenv("COTF_SANDBOX_WRITE_PATHS", str(soul))
    return {"soul": soul, "tool": soul / "tool", "schedule": schedule}


@pytest.mark.parametrize(
    "target",
    [
        "{soul}/.git/hooks/pre-commit",
        "{soul}/.git/hooks/post-checkout",
        "{soul}/.git/config",
        "{tool}/.git/hooks/pre-commit",
        "{tool}/.git/config",
        "{schedule}",
    ],
)
def test_a_write_grant_keeps_what_runs_outside_the_jail(target, world, monkeypatch):
    """git runs a granted repository's hooks and honours its config on the
    operator's next command, and the cron daemon runs the linked schedule. All
    of them outside the jail."""
    paths = _grant_with_a_repo(world, monkeypatch)
    path = target.format(**{k: str(v) for k, v in paths.items()})
    project = world["project"]
    assert _can_read(path, project) or not Path(path).exists()
    assert not _can_write(path, project), f"{path} is writable"


def test_a_write_grant_still_takes_ordinary_writes(world, monkeypatch):
    """The denies are narrow: a note, a new file beside the schedule, and git's
    own objects under `.git` are what the grant is for."""
    paths = _grant_with_a_repo(world, monkeypatch)
    soul, project = paths["soul"], world["project"]
    for path in (
        soul / "notes.md",
        soul / "config" / "other.yaml",
        soul / ".git" / "HEAD",
        paths["tool"] / ".git" / "index",
    ):
        assert _can_write(str(path), project), f"{path} is not writable"


@pytest.mark.parametrize(
    "script",
    [
        # Move the parent aside, edit the file there, move it back.
        "mv {soul}/.git {soul}/moved && echo x >> {soul}/moved/config"
        " && mv {soul}/moved {soul}/.git",
        "mv {tool} {soul}/moved && echo x >> {soul}/moved/.git/hooks/pre-commit"
        " && mv {soul}/moved {tool}",
        "mv {soul}/config {soul}/moved && echo x >> {soul}/moved/cotf-cron.yaml"
        " && mv {soul}/moved {soul}/config",
        # Move the parent aside and put a fresh one where it was.
        "mv {soul}/.git {soul}/moved && mkdir -p {soul}/.git/hooks"
        " && echo x > {soul}/.git/hooks/pre-commit",
        "mv {soul}/config {soul}/moved && mkdir {soul}/config && echo x > {schedule}",
    ],
    ids=["git-dir", "repo-dir", "schedule-dir", "fresh-git-dir", "fresh-schedule"],
)
def test_a_rename_does_not_carry_the_protection_away(script, world, monkeypatch):
    """A bind moves with its parent under bwrap, so renaming the parent and
    putting a fresh file where the protected one was is the obvious way round.
    Seatbelt matches paths, so the shape to defeat there is renaming the parent
    out of the pattern and back."""
    paths = _grant_with_a_repo(world, monkeypatch)
    command = script.format(**{k: str(v) for k, v in paths.items()})
    assert _run(["/bin/sh", "-c", command], world["project"]) != 0
    # Whichever step refused, nothing at a path git or cron reads holds the
    # agent's content. Seatbelt lets a whole repository move, which is harmless:
    # its hooks go with it and stay under the pattern, and a fresh `.git` at the
    # old path is refused.
    for path in (
        paths["soul"] / ".git" / "config",
        paths["soul"] / ".git" / "hooks" / "pre-commit",
        paths["tool"] / ".git" / "hooks" / "pre-commit",
        paths["schedule"],
    ):
        assert not path.exists() or "x" not in path.read_text(), path


def test_naming_a_protected_path_opens_only_that_path(world, monkeypatch):
    paths = _grant_with_a_repo(world, monkeypatch)
    soul, project = paths["soul"], world["project"]
    hooks = soul / ".git" / "hooks"
    monkeypatch.setenv("COTF_SANDBOX_WRITE_PATHS", f"{soul}:{hooks}")
    assert _can_write(str(hooks / "pre-commit"), project)
    assert not _can_write(str(soul / ".git" / "config"), project)


def test_an_entry_the_claude_config_links_out_to_is_readable(world, monkeypatch):
    """The claude side of the codex case above: a skill linked out of the claude
    config dir. Linux mounts the target; seatbelt needs a read grant on it."""
    home, project = world["home"], world["project"]
    shared = home / "soul" / "skills" / "review"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("how to review\n")
    beside = home / "soul" / "private.md"
    beside.write_text("PARITY\n")
    config = home / ".claude"
    (config / "skills").mkdir(parents=True, exist_ok=True)
    (config / "skills" / "review").symlink_to(shared)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    assert _can_read(str(config / "skills" / "review" / "SKILL.md"), project)
    assert not _can_read(str(beside), project)


@pytest.fixture
def codex_login(world, monkeypatch):
    """The operator's codex home with a ChatGPT login in it."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    codex = world["home"] / ".codex"
    codex.mkdir(exist_ok=True)
    (codex / "auth.json").write_text('{"tokens": "PARITY"}\n')
    (codex / "config.toml").write_text('model = "parity"\n')
    return codex


def test_the_brokered_codex_login_is_hidden_from_the_turn(
    world, codex_login, monkeypatch
):
    from claude_on_the_fly import codex_auth

    monkeypatch.setenv(codex_auth.BASE_URL_ENV, "http://127.0.0.1:1/_session/t/chatgpt")
    project = world["project"]
    auth = codex_login / "auth.json"
    # A link the turn plants in its own workspace resolves onto the same file.
    (project / "login.json").symlink_to(auth)

    assert not _can_read(str(auth), project)
    assert not _can_write(str(auth), project)
    assert not _can_read(str(project / "login.json"), project)
    # Only the login: codex still reads the config beside it.
    assert _can_read(str(codex_login / "config.toml"), project)


def test_the_codex_login_stays_readable_when_the_broker_does_not_hold_it(
    world, codex_login, monkeypatch
):
    from claude_on_the_fly import codex_auth

    monkeypatch.delenv(codex_auth.BASE_URL_ENV, raising=False)
    assert _can_read(str(codex_login / "auth.json"), world["project"])


def test_a_relocated_codex_home_keeps_its_state_writable(world, monkeypatch):
    """codex opens `state_N.sqlite` in its home on every run. The rule granting
    it named `$HOME/.codex`, so a relocated CODEX_HOME made a jailed pty turn
    die with "attempt to write a readonly database". The login beside it stays
    governed by the same home."""
    relocated = world["home"] / "elsewhere" / "codex"
    relocated.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(relocated))
    project = world["project"]
    assert _can_write(str(relocated / "state_5.sqlite"), project)
    assert _can_write(str(relocated / "state_5.sqlite-wal"), project)
    assert not _can_write(str(relocated / "config.toml"), project)


def test_a_refresh_during_a_turn_does_not_expose_the_login(
    world, codex_login, monkeypatch, tmp_path
):
    """The broker rewrites the login while a jailed turn is running. On Linux the
    file is hidden by a bind mount, and a rename over the path on the host drops
    that mount in the turn's namespace: measured, the turn then read the new
    login. The broker writes in place, so the mask has to survive the refresh."""
    import json
    import time

    from claude_on_the_fly import codex_auth

    monkeypatch.setenv(codex_auth.BASE_URL_ENV, "http://127.0.0.1:1/_session/t/chatgpt")
    auth = codex_login / "auth.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": "OLD", "refresh_token": "R"}})
    )
    project = world["project"]
    ready = project / "ready"
    script = f"cat '{auth}' >/dev/null 2>&1; touch '{ready}'; sleep 3; cat '{auth}'"
    proc = subprocess.Popen(
        sandbox.wrap(["/bin/sh", "-c", script], project),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "the jailed process never started"

    login = codex_auth.ChatGPTLogin(auth, tmp_path / "codex-auth.lock")
    login._write(
        json.loads(auth.read_text()),
        {"access_token": "REFRESHED", "refresh_token": "ROTATED"},
    )
    out, _ = proc.communicate(timeout=30)

    assert "REFRESHED" in auth.read_text()
    assert "REFRESHED" not in out
    assert proc.returncode != 0
