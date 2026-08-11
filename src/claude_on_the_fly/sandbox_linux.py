"""Linux half of `sandbox.mode: jail`, built on bubblewrap.

The macOS jail is a seatbelt profile: rules layered over the real filesystem, so
a denied path still exists and the denial arrives as EPERM. bubblewrap works the
other way round. It builds a *mount namespace*, so a denied path is simply not
there, and the failure arrives as ENOENT for a read or EROFS for a write. Three
consequences run through this module and the code that consumes it:

  * The contract is expressed as mounts, not rules. `--ro-bind / /` makes the
    whole filesystem readable and nothing writable; every write grant is a mount
    that overrides it, and every write deny is a read-only mount laid back over
    an already-writable area. Order is significant, which SBPL's last-match-wins
    also is, so the two profiles read similarly even though the mechanism differs.
  * There is no regex or glob. Seatbelt can deny `~/.codex/*.sqlite(-wal|-shm)?`
    in one rule; a mount namespace cannot express a pattern at all. Where a
    pattern is unavoidable the caller passes concrete paths it resolved itself.
  * A deny needs something to mount. `(deny file-write* (literal ~/.mcp.json))`
    on macOS also stops the file being *created*; a read-only bind cannot be made
    over a path that does not exist. `placeholders` closes that gap, and without
    it an absent-then-created file would be a silent parity hole.

Policy (which paths) lives in sandbox.py beside the seatbelt profile selection.
This module is mechanism only: it turns path lists into argv and knows nothing
about credentials, brokers, or settings. That split keeps it a pure function,
which is what lets both platforms' branches be tested on either OS.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Spawned inside the namespace to bring up the loopback listeners before the
# agent starts. Named rather than imported: this module must stay importable on
# macOS, where the relay is never used.
_RELAY_MODULE = "claude_on_the_fly.netns_relay"

# Where the relay's unix sockets are mounted inside the namespace. A tmpfs is
# mounted here first: /run is read-only under `--ro-bind / /`, and bwrap cannot
# create a mount point inside a read-only parent.
SOCKET_DIR = "/run/cotf"


class Placeholders(NamedTuple):
    """Inert stand-ins mounted over write-denied paths that do not exist.

    Three of them because the stand-in still has to parse as whatever the reader
    expects. Measured the hard way: a single `{}` placeholder used for every file
    made codex exit with "Error loading config.toml: invalid key-value pair" the
    moment it was mounted over an absent config.toml. An empty file is valid TOML,
    valid Markdown and valid YAML; only JSON needs the empty object.
    """

    empty: Path
    json: Path
    directory: Path
    unreadable: Path

    def for_path(self, target: Path, *, directory: bool = False) -> Path:
        """The stand-in for `target`. Whether a missing one should be a directory
        comes from the caller, because the name cannot say: `.vscode` is a
        directory and `.bashrc` is a file, and neither has a suffix. Deciding it
        from the suffix made every extension-less deny a directory, so a jailed
        turn left a directory called `.bashrc` in the operator's workspace and a
        directory at `.git/config`, which makes `git init` there fail outright.
        """
        if directory:
            return self.directory
        return self.json if target.suffix == ".json" else self.empty


def prepare_placeholders(root: Path) -> Placeholders:
    """Create the stand-ins. Idempotent.

    Deliberately outside the sandbox's write set: they are mounted read-only, but
    a writable source would still be a way to hand the agent a shared file under a
    name it does not control.
    """
    root.mkdir(parents=True, exist_ok=True)
    empty, empty_json, directory = root / "empty", root / "empty.json", root / "empty.d"
    unreadable = root / "unreadable"
    if not empty.exists():
        empty.write_text("")
    if not empty_json.exists():
        empty_json.write_text("{}\n")
    if not unreadable.exists():
        # Mode 000, so a read of anything masked with it fails with EACCES rather
        # than succeeding and returning nothing. "You may read it, it is simply
        # blank" is not the promise; the macOS side refuses outright and this has
        # to as well. It also makes a masked socket un-connectable, since it is no
        # longer a socket.
        unreadable.write_text("")
        unreadable.chmod(0o000)
    directory.mkdir(exist_ok=True)
    return Placeholders(empty, empty_json, directory, unreadable)


# Mount kinds, in the order they must apply to a *single* path. Only used to
# break ties between two grants at the same depth; depth itself does the real
# ordering work below.
_OPAQUE, _READ_ONLY, _READ_WRITE, _WRITE_DENIED, _MASKED = range(5)


def _mounts(
    opaque: Iterable[Path | str],
    read_only: Iterable[Path | str],
    read_write: Iterable[Path | str],
    write_denied: Iterable[Path | str],
    masked: Iterable[Path | str],
    *,
    placeholders: Placeholders,
    deny_dirs: frozenset[str],
) -> list[tuple[int, int, list[str]]]:
    """Every mount tagged with its path depth, for ordering by `sorted`.

    Mount order decides the policy, and getting it from the caller's list order
    is a trap: a read grant on a parent silently re-exposes an opaque child.
    Without ordering, granting `extra_paths: /home/me` would also have re-exposed
    the deeper data dir holding this daemon's tokens, with nothing in the argv
    looking wrong.

    Sorting by depth settles it. A parent is always mounted before its children,
    so a deeper rule always wins over a shallower one whichever way it points: an
    opaque data dir still hides a `.env` beneath a granted home, and a granted
    `memory/` still surfaces beneath an opaque data dir. The caller can pass its
    lists in any order and get the same jail. What depth does NOT do is override a
    grant aimed at an opaque path itself -- `extra_paths: $HOME` does re-expose
    `$HOME`, exactly as it does on macOS, where the extras rule sits after the
    home deny and last-match-wins.
    """
    out: list[tuple[int, int, list[str]]] = []
    groups = (
        (_OPAQUE, opaque),
        (_READ_ONLY, read_only),
        (_READ_WRITE, read_write),
        (_WRITE_DENIED, write_denied),
        (_MASKED, masked),
    )
    for rank, paths in groups:
        for path in paths:
            target = Path(path)
            out.append(
                (
                    len(target.parts),
                    rank,
                    _mount_args(rank, target, placeholders, deny_dirs),
                )
            )
    return out


def _mount_args(
    rank: int, target: Path, placeholders: Placeholders, deny_dirs: frozenset[str]
) -> list[str]:
    """The bwrap flags for one mount.

    `-try` on the grants because a grant for a path that is not there is a
    harmless no-op, and requiring every optional tool's directory to exist would
    make the jail fail on a machine that simply has not run that tool yet. Never
    `-try` on a deny: an absent path there gets a placeholder instead, so the
    deny holds whether or not the file exists yet.
    """
    if rank == _OPAQUE:
        return ["--tmpfs", str(target)]
    if rank == _READ_ONLY:
        return ["--ro-bind-try", str(target), str(target)]
    if rank == _READ_WRITE:
        return ["--bind-try", str(target), str(target)]
    # An existing path is bound over itself, which keeps its contents readable
    # while refusing writes. An absent one gets the placeholder, so `.mcp.json`
    # cannot be *created* either: the macOS rule denies the write to a literal
    # path whether or not anything is there yet.
    if rank == _MASKED:
        # Source and target kinds have to match: bwrap refuses to mount a
        # directory over a file with "Can't mkdir ...: Not a directory", which is
        # how the ssh-agent socket first broke every jail in this suite. So a
        # directory target gets the empty directory, and everything else -- plain
        # file or socket -- gets the mode-000 file, which denies the read and
        # stops the socket being a socket.
        source = placeholders.directory if target.is_dir() else placeholders.unreadable
        return ["--ro-bind", str(source), str(target)]
    source = (
        target
        if target.exists()
        else placeholders.for_path(target, directory=str(target) in deny_dirs)
    )
    return ["--ro-bind", str(source), str(target)]


def ensure_write_deny_targets(
    write_denied: Iterable[Path | str],
    placeholders: Placeholders,
    directories: Iterable[Path | str] = (),
) -> list[Path]:
    """Materialise any write-denied path that does not exist yet. Returns what it made.

    A read-only bind needs something to mount over, and bwrap creates a missing
    mount point *in the parent it was given*. For a workspace that is a
    read-write bind of a real directory, that means the file lands on the host
    and stays there after the namespace goes away. Doing it here instead makes
    that deliberate and loggable rather than a surprise artifact of a mount.

    Not doing it at all is the one option that is actually unsafe. Without a
    target, an absent `.mcp.json` is simply creatable, and MCP config decides
    which tool servers later runs load: the same "instructions that outlive the
    session" problem the codex write grants exist to close. A visible inert `{}`
    in the workspace is the smaller cost, and it is one an operator can see.
    """
    deny_dirs = {str(Path(p)) for p in directories}
    created: list[Path] = []
    for path in write_denied:
        target = Path(path)
        if target.exists():
            continue
        source = placeholders.for_path(target, directory=str(target) in deny_dirs)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text())
        created.append(target)
    if created:
        logger.info(
            "sandbox: created %d placeholder(s) so the write deny has something to "
            "mount over: %s",
            len(created),
            [str(p) for p in created],
        )
    return created


def jail_argv(
    argv: list[str],
    *,
    opaque: Iterable[Path | str],
    read_only: Iterable[Path | str],
    read_write: Iterable[Path | str],
    write_denied: Iterable[Path | str],
    masked: Iterable[Path | str],
    sockets: Mapping[int, Path | str],
    placeholders: Placeholders,
    write_denied_dirs: Iterable[Path | str] = (),
    bwrap: str = "bwrap",
    python: str | None = None,
) -> list[str]:
    """Wrap `argv` in a bubblewrap jail. Pure: no environment or settings reads.

    The mount order *is* the policy, because later mounts win. The fixed prefix
    goes first:

      1. `--ro-bind / /` — everything readable, nothing writable. Matches
         fs-deny-most.sb's "reads outside _HOME stay allowed" so the toolchain
         still starts.
      2. `--proc` / `--dev` — a namespace needs its own; binding the host's
         /proc in is both wrong and a leak.

    Then every path rule, ordered by depth rather than by argument order (see
    `_mounts` for why that matters):

      * `opaque` — tmpfs, making the path vanish. `$HOME` and the data dir go
        here; it is the mount-namespace spelling of
        `(deny file-read* (subpath _HOME))`.
      * `read_only` — re-granted readable, still not writable.
      * `read_write` — the write allowlist.
      * `write_denied` — read-only laid back over a writable area. The only way
        to express `(deny file-write* ...)` inside an allowed one, e.g.
        `.git/hooks` within a writable workspace. `write_denied_dirs` names the
        subset that are directories, which the path cannot say for itself.
      * `masked` — unreadable outright, for a path that a coarser grant would
        otherwise expose. `--ro-bind / /` reaches every unix socket on the
        machine, and a mount namespace has no pattern matching, so both the
        forwarded ssh-agent and a `.env` sitting under a granted subtree need
        naming individually.

    Last come the relay sockets and `--unshare-net`. Those sit on `/run`, which
    no path rule touches, so they need no part in the depth ordering.
    """
    python = python or sys.executable
    args: list[str] = [
        bwrap,
        "--ro-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        # /run is the Linux answer to jail.sb's keychain deny. libsecret,
        # gnome-keyring and kwallet are all reached over the D-Bus session bus,
        # whose socket lives in $XDG_RUNTIME_DIR under /run/user/<uid>; a tmpfs
        # here takes the bus away and the credential stores with it.
        #
        # It is also the only writable parent available for the relay's socket
        # directory. Under `--ro-bind / /` everything is read-only, and bwrap
        # cannot create a mount point inside a read-only parent ("Can't mkdir
        # /run/cotf: Read-only file system"), so the socket dir has to sit on a
        # filesystem this profile made itself.
        "--tmpfs",
        "/run",
        # Without this a killed daemon leaves the agent running with no parent
        # to reap it, holding the workspace and the relay sockets open.
        "--die-with-parent",
    ]
    opaque = list(opaque)
    for _depth, _rank, mount in sorted(
        _mounts(
            opaque,
            read_only,
            read_write,
            write_denied,
            masked,
            placeholders=placeholders,
            deny_dirs=frozenset(str(Path(p)) for p in write_denied_dirs),
        )
    ):
        args += mount
    # A tmpfs is writable, so hiding $HOME behind one would leave every path
    # under it writable -- ephemerally, into a throwaway filesystem the host
    # never sees, but *successfully*. That breaks the contract in the direction
    # that matters least for data and most for the agent: it is told the write
    # worked, having been told writes outside its workspace fail.
    #
    # This has to be a trailing pass. Remounting read-only straight after the
    # tmpfs makes the path a read-only parent, and bwrap then cannot create the
    # mount points for the workspace and memory grants beneath it ("Can't mkdir
    # parents ... Read-only file system"). Verified both orderings on the target
    # kernel: only last works, and the deeper grants stay writable through it
    # because each is its own mount rather than part of the tmpfs.
    # Sorted for the same reason the mounts are: the caller's list order must not
    # reach the argv, or the same contract emits two different jails and the
    # difference is invisible in review.
    for path in sorted(opaque, key=lambda p: Path(p).parts):
        args += ["--remount-ro", str(path)]
    inner = list(argv)
    if sockets:
        # A plain dir on the /run tmpfs made above, not another tmpfs: the
        # sockets are bound in individually, so there is no directory for the
        # agent to write into.
        args += ["--dir", SOCKET_DIR]
        mapping: list[str] = []
        for port, host_socket in sorted(sockets.items()):
            args += ["--bind", str(host_socket), socket_path(port)]
            mapping += ["--map", f"{port}={socket_path(port)}"]
        # The agent runs *under* the relay launcher rather than beside it: the
        # namespace's loopback listeners have to exist before the agent's first
        # call, and nothing outside the namespace can create them.
        inner = [python, "-m", _RELAY_MODULE, *mapping, "--", *inner]
    # Egress: a fresh network stack with no route anywhere. Verified on the
    # target kernel -- lo comes up on its own (so the agent's own dev servers and
    # tests still work), the host's loopback services are refused, and the
    # internet is unreachable. The relay sockets bound above are the only way
    # out, which makes the egress proxy unbypassable rather than merely default.
    args += ["--unshare-net"]
    args += ["--", *inner]
    return args


def socket_path(port: int) -> str:
    """Where the relay socket for `port` appears inside the namespace."""
    return f"{SOCKET_DIR}/{port}.sock"
