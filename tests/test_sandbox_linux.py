"""sandbox_linux: turning a path contract into bubblewrap argv.

Pure functions, so these run on either OS. That is deliberate: the platform is an
injectable seam precisely so the branch that only executes on Linux is not the
one branch nobody tests.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from claude_on_the_fly import sandbox, sandbox_linux


@pytest.fixture
def places(tmp_path):
    return sandbox_linux.prepare_placeholders(tmp_path / "jail")


def _argv(**kwargs):
    base = {
        "opaque": [],
        "read_only": [],
        "read_write": [],
        "write_denied": [],
        "masked": [],
        "sockets": {},
    }
    base.update(kwargs)
    return base


# --- placeholders ---


def test_placeholder_kind_follows_the_extension(places):
    """A single `{}` stand-in for every file made codex exit with "Error loading
    config.toml: invalid key-value pair" the moment it was mounted over an absent
    config.toml. An empty file is valid TOML, Markdown and YAML; JSON is the only
    format that needs a body."""
    assert places.for_path(Path("/x/.mcp.json")) == places.json
    assert places.for_path(Path("/x/config.toml")) == places.empty
    assert places.for_path(Path("/x/AGENTS.md")) == places.empty
    assert places.json.read_text() == "{}\n"
    assert places.empty.read_text() == ""


def test_directory_placeholders_are_declared_not_guessed(places):
    """`.vscode` is a directory and `.bashrc` is a file, and neither has a
    suffix. Guessing from the name made every extension-less deny a directory,
    so a jailed turn left a directory called `.bashrc` in the workspace and a
    directory at `.git/config`, which makes `git init` there fail outright."""
    assert places.for_path(Path("/x/.vscode"), directory=True) == places.directory
    assert places.for_path(Path("/x/.bashrc")) == places.empty
    assert places.for_path(Path("/x/.git/config")) == places.empty


def test_prepare_placeholders_is_idempotent(tmp_path):
    first = sandbox_linux.prepare_placeholders(tmp_path / "jail")
    first.empty.write_text("do not clobber me")
    second = sandbox_linux.prepare_placeholders(tmp_path / "jail")
    assert second == first
    assert first.empty.read_text() == "do not clobber me"


# --- write-deny materialisation ---


def test_absent_write_denies_are_materialised(tmp_path, places):
    """A read-only bind needs something to mount over. Without a target an absent
    .mcp.json is simply creatable, and MCP config decides which tool servers later
    runs load."""
    project = tmp_path / "ws"
    project.mkdir()
    made = sandbox_linux.ensure_write_deny_targets(
        [project / ".mcp.json", project / ".vscode", project / ".bashrc"],
        places,
        [project / ".vscode"],
    )
    assert made == [project / ".mcp.json", project / ".vscode", project / ".bashrc"]
    assert (project / ".mcp.json").read_text() == "{}\n"
    assert (project / ".vscode").is_dir()
    # Not a directory: the deny covers a shell rc the next command would read.
    assert (project / ".bashrc").is_file()


def test_existing_write_denies_are_left_alone(tmp_path, places):
    project = tmp_path / "ws"
    project.mkdir()
    (project / ".mcp.json").write_text('{"real": true}')
    assert (
        sandbox_linux.ensure_write_deny_targets([project / ".mcp.json"], places) == []
    )
    assert (project / ".mcp.json").read_text() == '{"real": true}'


def test_materialising_is_logged_because_it_touches_the_workspace(
    tmp_path, places, caplog
):
    project = tmp_path / "ws"
    project.mkdir()
    with caplog.at_level("INFO", logger="claude_on_the_fly.sandbox_linux"):
        sandbox_linux.ensure_write_deny_targets([project / ".mcp.json"], places)
    assert "placeholder" in "\n".join(r.getMessage() for r in caplog.records)


# --- mount ordering ---


def test_a_read_grant_on_a_parent_cannot_undo_an_opaque_child(tmp_path, places):
    """The hazard that forced depth ordering. `extra_paths: $HOME` re-exposes
    $HOME, as it does on macOS where the extras rule sits after the HOME deny --
    but it must NOT re-expose the deeper data dir holding the daemon's tokens.
    Verified against a live jail before it was encoded here."""
    home = Path("/home/me")
    data = home / ".claude-on-the-fly"
    out = sandbox_linux.jail_argv(
        ["true"],
        **_argv(opaque=[home, data], read_only=[home]),
        placeholders=places,
    )
    order = [i for i, a in enumerate(out) if a in ("--tmpfs", "--ro-bind-try")]
    kinds = [(out[i], out[i + 1]) for i in order]
    assert kinds.index(("--tmpfs", str(data))) > kinds.index(
        ("--ro-bind-try", str(home))
    )


def test_write_denies_land_after_the_write_grant_they_cover(tmp_path, places):
    project = tmp_path / "ws" / "proj"
    (project / ".git" / "hooks").mkdir(parents=True)
    out = sandbox_linux.jail_argv(
        ["true"],
        **_argv(read_write=[project], write_denied=[project / ".git" / "hooks"]),
        placeholders=places,
    )
    assert out.index(str(project / ".git" / "hooks")) > out.index(str(project))


def test_mount_order_does_not_depend_on_caller_list_order(tmp_path, places):
    home, data = Path("/home/me"), Path("/home/me/.data")
    forward = sandbox_linux.jail_argv(
        ["true"],
        **_argv(opaque=[home, data], read_only=[data / "shims"]),
        placeholders=places,
    )
    reversed_ = sandbox_linux.jail_argv(
        ["true"],
        **_argv(opaque=[data, home], read_only=[data / "shims"]),
        placeholders=places,
    )
    assert forward == reversed_


@pytest.mark.parametrize("scoped", ["", "true"])
def test_mount_order_is_depth_then_rank_for_every_pair(tmp_path, monkeypatch, scoped):
    """The ordering property that makes the whole mount table correct, asserted
    over the real policy through the real pure function rather than a hand-drawn
    table. Mount order is the policy -- later mounts win -- so a parent must
    always be mounted before its children, and at equal depth the rank
    (opaque < read-only < read-write < write-denied < masked) breaks the tie.
    This is the invariant that keeps `extra_paths: $HOME` from re-exposing the
    data dir, and the codex-grant and state-deny regressions on the macOS side
    were the same bug class in SBPL ordering."""
    if scoped:
        monkeypatch.setenv("COTF_SANDBOX_SCOPE_SESSIONS", scoped)
    workspace = tmp_path / "ws"
    grants = sandbox._linux_grants(workspace)
    placeholders = sandbox_linux.prepare_placeholders(tmp_path / "jail")
    argv = sandbox_linux.jail_argv(
        ["true"],
        opaque=grants["opaque"],
        read_only=grants["read_only"],
        read_write=grants["read_write"],
        write_denied=grants["write_denied"],
        masked=grants["masked"],
        sockets={},
        placeholders=placeholders,
        write_denied_dirs=grants["write_denied_dirs"],
    )
    # The full tuple, not just (depth, rank): jail_argv sorts on (depth, rank,
    # args), and the args list is what breaks a tie between two mounts of the
    # same path depth and the same kind.
    mounts = sorted(
        sandbox_linux._mounts(
            grants["opaque"],
            grants["read_only"],
            grants["read_write"],
            grants["write_denied"],
            grants["masked"],
            placeholders=placeholders,
            deny_dirs=frozenset(str(Path(p)) for p in grants["write_denied_dirs"]),
        )
    )
    # The property, over every pair: the deeper path lands later, and at equal
    # depth the rank breaks the tie.
    for (depth, rank, _), (next_depth, next_rank, _) in pairwise(mounts):
        assert (depth, rank) <= (next_depth, next_rank), (
            f"mount at depth {depth} rank {rank} sorts after depth {next_depth} "
            f"rank {next_rank}"
        )
    # The argv emits exactly this table, in this order: the fixed prefix, then
    # the sorted mounts, then the remount pass. A mismatch here means the jail
    # runs a table the property above did not check.
    prefix = argv[:11]
    assert prefix == [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/run",
        "--die-with-parent",
    ]
    emitted = argv[11 : 11 + sum(len(args) for _, _, args in mounts)]
    assert emitted == [arg for _, _, args in mounts for arg in args]
    assert argv[11 + len(emitted)] == "--remount-ro"


# --- the fixed prefix and the trailing remount ---


def test_opaque_paths_are_remounted_read_only_at_the_end(places):
    """A tmpfs is writable, so hiding $HOME behind one leaves everything under it
    writable -- ephemerally, but successfully, which contradicts what the agent is
    told. The remount must be last: doing it early makes bwrap unable to create
    the mount points beneath ("Can't mkdir parents ... Read-only file system")."""
    home = Path("/home/me")
    out = sandbox_linux.jail_argv(
        ["true"], **_argv(opaque=[home], read_write=[home / "ws"]), placeholders=places
    )
    assert out[-3:-1] == ["--remount-ro", str(home)] or "--remount-ro" in out
    assert out.index("--remount-ro") > out.index(str(home / "ws"))


def test_run_is_a_tmpfs_so_dbus_and_the_keychain_go_with_it(places):
    out = sandbox_linux.jail_argv(["true"], **_argv(), placeholders=places)
    assert out[out.index("--tmpfs") : out.index("--tmpfs") + 2] == ["--tmpfs", "/run"]


def test_network_namespace_is_always_unshared(places):
    out = sandbox_linux.jail_argv(["true"], **_argv(), placeholders=places)
    assert "--unshare-net" in out
    assert out[-1] == "true"


# --- relay sockets and the launcher ---


def test_sockets_are_bound_individually_under_a_plain_dir(tmp_path, places):
    sock = tmp_path / "8931.sock"
    sock.write_text("")
    out = sandbox_linux.jail_argv(
        ["codex"], **_argv(sockets={8931: sock}), placeholders=places, python="/py"
    )
    # A --dir, not another --tmpfs: nothing for the agent to write into.
    assert "--dir" in out and sandbox_linux.SOCKET_DIR in out
    assert ["--bind", str(sock), "/run/cotf/8931.sock"] == out[
        out.index("--bind") : out.index("--bind") + 3
    ]


def test_the_agent_runs_under_the_relay_launcher(tmp_path, places):
    """The namespace's loopback listeners must exist before the agent's first
    call, and nothing outside the namespace can create them."""
    sock = tmp_path / "8931.sock"
    sock.write_text("")
    out = sandbox_linux.jail_argv(
        ["codex", "exec"],
        **_argv(sockets={8931: sock}),
        placeholders=places,
        python="/py",
    )
    tail = out[out.index("--") + 1 :]
    assert tail[:3] == ["/py", "-m", "claude_on_the_fly.netns_relay"]
    assert "--map" in tail and "8931=/run/cotf/8931.sock" in tail
    assert tail[-2:] == ["codex", "exec"]


def test_no_sockets_means_no_launcher(places):
    out = sandbox_linux.jail_argv(["codex"], **_argv(), placeholders=places)
    assert "claude_on_the_fly.netns_relay" not in out
    assert out[out.index("--") + 1 :] == ["codex"]


def test_socket_path_is_the_shared_convention():
    assert sandbox_linux.socket_path(8931) == "/run/cotf/8931.sock"
