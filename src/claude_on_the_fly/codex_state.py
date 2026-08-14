"""Daemon-owned Codex session mappings.

The workspace is writable by the agent, so it is not a trustable place to store
the mapping from a COTF session to a Codex thread. This module keeps that mapping
under DATA_DIR, authenticates the workspace/session pair in the record, and uses
an atomic 0600 write.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import shutil
import stat
from contextlib import suppress
from pathlib import Path

from claude_on_the_fly.agent import DATA_DIR

logger = logging.getLogger(__name__)

MAPPINGS_DIR = DATA_DIR / "codex-sessions"
HOMES_DIR = DATA_DIR / "codex-homes"
_MAX_THREAD_ID = 512

# A thread id is spliced into a glob pattern and into a filename, so the charset
# is part of both. Measured against 14 real mappings under a deployed data dir:
# every one is 36 characters of lowercase hex and hyphen, which is a UUID. This
# pattern is a deliberate superset of that -- underscore and dot and mixed case
# cost nothing and survive codex changing the format -- while still excluding
# every glob metacharacter (`*`, `?`, `[`), the path separators, and whitespace.
#
# Nothing reachable today produces a bad one: the id arrives only in codex's own
# `--json` control event `thread.started`, never from model freeform text. This
# is defence in depth, not a live exploit.
_THREAD_ID_SAFE = re.compile(r"\A[A-Za-z0-9._-]+\Z")

# Everything under a shared ~/.codex that the per-workspace home has to expose so
# codex starts and behaves the way the operator configured it. Linked rather than
# copied, so an operator's edit to the real file takes effect on the next turn and
# a jailed turn writing through the link lands on a path the profile already
# governs: the instruction and execution entries stay write-denied there, and
# auth.json stays writable so token refresh still works.
#
# Measured against codex-cli 0.147.0: a home holding only an auth.json link and an
# empty config.toml completed a real `codex exec` turn, so the rest of this list is
# about honouring the operator's configuration rather than about starting at all.
_SHARED_ENTRIES = (
    "config.toml",
    "AGENTS.md",
    "hooks.json",
    "rules",
    "plugins",
    "agents",
    "prompts",
    "auth.json",
)

# Shared directories the agent must see the *contents* of, but which codex also
# writes into. Linking the directory itself would do one of those at the cost of
# the other: measured on a real jailed turn, codex touched `skills/` 242 times and
# created a `skills/.system/` tree of its own, so a link to a write-denied shared
# directory would stop it. Linking each entry instead leaves a real directory codex
# owns, holding read-only links to the operator's own skills.
#
# The operator's skills were reachable before per-thread homes existed, because the
# turn ran against the shared ~/.codex directly. Losing them was a regression this
# restores.
_SHARED_MERGED = ("skills",)


def _valid_thread_id(clean: str) -> bool:
    """Whether a stripped thread id is one this daemon will act on.

    Both the read side and the write side ask, so a mapping written by an older
    build cannot get past the reader either. The length cap and the charset are
    one question: a value that fails the charset is not a thread id, whatever it
    would do downstream.
    """
    return (
        bool(clean)
        and len(clean) <= _MAX_THREAD_ID
        and bool(_THREAD_ID_SAFE.match(clean))
    )


def rollout_glob(thread_id: str) -> str:
    """The filename pattern that finds one thread's rollout.

    One helper because both callers splice the same id into the same shape, and
    both prefix their own search root: this module's is `sessions/**/`, and
    `transcript._iter_rollouts` already starts inside each `sessions/`.

    `glob.escape` because the id is interpolated into a pattern, not compared:
    an id of `*` matches every rollout in the tree, and both callers act on what
    the pattern returns. `adopt_rollout` would copy another thread's rollout into
    this workspace's own CODEX_HOME, and `transcript._find_codex_rollout`
    searches every thread's home plus the shared tree and picks by mtime, so an
    unrelated conversation could be prepended to the next prompt as handoff
    context. Reproduced in a scratch tree before the escape went in, and the
    regression tests replay both shapes through the real callers.
    """
    return f"rollout-*-{glob.escape(thread_id)}.jsonl"


def _home_key(workspace: Path) -> str:
    return hashlib.sha256(
        str(workspace.resolve(strict=False)).encode("utf-8")
    ).hexdigest()


def home_dir(workspace: Path) -> Path:
    """This workspace's own `CODEX_HOME`.

    codex names its rollouts by date and thread id in one flat tree, and it
    chooses the name at startup, so there is no per-workspace path to grant the
    way claude's `projects/<hash>` can be granted. Giving each workspace its own
    home is what makes the rollout location predictable before the run, and one
    workspace is one chat thread, so it is also the isolation boundary: a jailed
    turn is granted this directory and cannot see any other thread's rollouts.

    Daemon-owned under DATA_DIR, beside the mappings and outside the
    agent-writable workspace, for the reason this module exists.

    Without `sandbox.scoped_sessions` this is the operator's shared home, which is
    where codex kept every rollout before per-thread homes existed. One place
    decides it, so the profile parameter and the `CODEX_HOME` the backend sets on
    the child cannot disagree about where the rollouts are.
    """
    from claude_on_the_fly import envfile, sandbox

    if not sandbox.scoped_sessions():
        return envfile.codex_home()
    return HOMES_DIR / _home_key(workspace)


def ensure_home(workspace: Path, shared: Path | None = None) -> Path:
    """Create this workspace's `CODEX_HOME` and link the shared entries into it.

    Idempotent: it runs before every codex spawn, and an operator who adds a
    prompts/ directory later gets it linked on the next turn without a restart.
    Absent shared entries are skipped rather than stubbed, because codex treats a
    missing config as "use the defaults" and a dangling link as an error.
    """
    from claude_on_the_fly import envfile, sandbox

    home = home_dir(workspace)
    # sessions/ is created here rather than left to codex because the jail makes
    # $HOME opaque, and a recursive mkdir that cannot stat an ancestor walks up and
    # tries to create it instead: measured as `mkdir: /Users/hoss: Operation not
    # permitted` on a path whose leaf was granted. Creating the chain now leaves
    # codex writing files inside a directory that already exists. True of the shared
    # home too: the boundary decides which home a turn gets, not whether the
    # directory codex is about to write into is there.
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    if not sandbox.scoped_sessions():
        # `home` is the operator's own shared tree here, so there is nothing to link:
        # every entry is already in place, and linking one onto itself would replace
        # the operator's config with a link to itself. `shared` is only ever a link
        # source, so it does not change that.
        return home
    # Resolved through envfile, not `Path.home() / ".codex"`. A deployment that sets
    # CODEX_HOME keeps its config and credential there, and the hardcoded default
    # pointed at a directory that may hold neither -- every link then skipped as an
    # absent target, leaving a home with no config and no credential at all. The
    # shared tree the jail denies resolves the same way, so the two cannot disagree.
    source_root = envfile.codex_home() if shared is None else shared
    for name in _SHARED_ENTRIES:
        link, target = home / name, source_root / name
        if not target.exists():
            continue
        if link.is_symlink():
            if link.readlink() == target:
                continue
            if not _clear_link_site(link):
                continue
        elif link.exists():
            # A real file or directory where the link belongs is removed, not left
            # alone. This home is writable by the jailed turn, so a real AGENTS.md
            # or config.toml here would be an execution-control file the agent can
            # write -- standing orders it leaves itself for the next run, which is
            # the thing the shared ~/.codex deny list exists to prevent. Replacing
            # it with the link puts the name back on a path the profile denies
            # writes to.
            if not _clear_link_site(link):
                continue
        try:
            link.symlink_to(target)
        except OSError as exc:
            # Logged rather than suppressed. This is the operator's config, their
            # AGENTS.md and their hooks.json arriving in the thread, and a turn
            # that runs without them looks normal from the outside.
            logger.warning("codex: cannot link %s -> %s: %s", link, target, exc)
    for name in _SHARED_MERGED:
        _merge_shared_dir(home / name, source_root / name)
    return home


def _clear_link_site(link: Path) -> bool:
    """Empty the place a shared entry has to be linked into. False if it stays.

    `Path.unlink()` raises `IsADirectoryError` on a directory, and the old
    `with suppress(OSError)` around the unlink and the symlink_to that followed
    swallowed it with nothing logged. Confirmed by a run: a turn that deleted a
    shared-entry link inside its own writable CODEX_HOME and made a directory in
    its place kept that directory through every later `ensure_home()`, so the
    operator's `config.toml`, `AGENTS.md` and `hooks.json` stopped applying to
    that thread for ever. A thread that can permanently drop its own operator
    guardrails is worth one destructive step to take back.

    Two bounds on that step, since it is the only recursive removal in this
    module's hot path. The site must sit under HOMES_DIR, which the daemon owns:
    the operator's shared `~/.codex` is only ever a link *target* here, and with
    the session boundary off `ensure_home` returns before any of this. And a
    symlink is unlinked, never descended -- `rmtree` refuses one anyway -- so a
    link the turn planted cannot carry the removal out of the home.
    """
    if not link.is_relative_to(HOMES_DIR):
        logger.warning("codex: refusing to clear %s, outside %s", link, HOMES_DIR)
        return False
    try:
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)
        else:
            link.unlink()
    except OSError as exc:
        logger.warning("codex: cannot clear %s for the shared link: %s", link, exc)
        return False
    return True


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def shared_link_targets(shared: Path | None = None) -> list[Path]:
    """Resolved paths the per-thread home links to, for the Linux jail to mount.

    Seatbelt needs none of this: it matches the resolved path, and everything here
    already sits under a granted subtree. A mount namespace has no such luck -- a
    link into a directory nobody mounted dangles inside the jail, and codex reports
    a missing file rather than a hidden one. `~/.codex/skills -> ~/.agents/skills`
    is the shape that found this: outside every mount the profile lists.
    """
    from claude_on_the_fly import envfile

    root = envfile.codex_home() if shared is None else shared
    resolved: dict[str, Path] = {}
    for name in (*_SHARED_ENTRIES, *_SHARED_MERGED):
        entry = root / name
        if not entry.exists():
            continue
        real = Path(os.path.realpath(entry))
        resolved.setdefault(str(real), real)
        # Each child too: a merged directory links its entries individually, and
        # those can resolve somewhere else again.
        for child in _safe_iterdir(entry):
            child_real = Path(os.path.realpath(child))
            resolved.setdefault(str(child_real), child_real)
    return list(resolved.values())


def _merge_shared_dir(local: Path, shared: Path) -> None:
    """Make `local` a real directory holding links to each entry in `shared`.

    A real directory so codex can still create its own state inside it, and links
    so the operator's entries stay on the shared, write-denied paths. Entries codex
    created itself are left alone: the operator's set and codex's own set live side
    by side, and only a name collision has to be decided, which the shared entry
    wins for the same reason it wins in `ensure_home`.
    """
    local.mkdir(parents=True, exist_ok=True)
    for entry in _safe_iterdir(shared):
        # Dot-entries are codex's own state, not operator skills: it keeps a
        # `skills/.system/` tree of built-in skills and writes into it. Linking that
        # at the shared, write-denied path left codex unable to create its own
        # system skills -- measured as "cannot create .../skills/.system/marker"
        # inside a real jail, while an ordinary skill linked and read fine. Each
        # thread gets its own, which is also the right blast radius for something
        # the agent can write.
        if entry.name.startswith("."):
            continue
        link = local / entry.name
        if link.is_symlink():
            if link.readlink() == entry:
                continue
            if not _clear_link_site(link):
                continue
        elif link.exists():
            # Unlike `ensure_home`, a real entry here is codex's own and stays:
            # it may hold state the agent created on purpose, and `skills/` is a
            # directory codex writes into rather than an execution-control file
            # the operator owns.
            continue
        try:
            link.symlink_to(entry)
        except OSError as exc:
            logger.warning("codex: cannot link %s -> %s: %s", link, entry, exc)


def _mapping_key(workspace: Path, session_uuid: str) -> str:
    identity = f"{workspace.resolve(strict=False)}\0{session_uuid}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def mapping_path(workspace: Path, session_uuid: str) -> Path:
    """Stable daemon-owned path for one canonical workspace/session pair."""
    return MAPPINGS_DIR / f"{_mapping_key(workspace, session_uuid)}.json"


def _read_record(path: Path) -> dict | None:
    """Read one mapping without following a replacement symlink."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            raw = os.read(fd, 64 * 1024)
        finally:
            os.close(fd)
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def adopt_rollout(workspace: Path, thread_id: str, shared: Path | None = None) -> bool:
    """Whether `codex resume <thread_id>` can find its rollout in this home.

    A mapping only records a thread id. Whether the thread is still resumable is a
    question about a file, and `CODEX_HOME` decides which directory codex looks in,
    so turning the session boundary on moves the answer. Asking before the spawn is
    what turns "the turn fails with `no rollout found for thread id`" into "this
    thread starts fresh", which is the difference between a broken chat and a
    forgetful one.

    The rollout is copied out of the shared tree rather than linked to it. Seatbelt
    matches the resolved path and the shared tree is read-denied under the boundary,
    so a link would resolve onto a denied path and codex would report the rollout
    missing anyway. Its relative date directory is preserved, since that is the
    layout codex writes and reads. The original stays where it is: the daemon reads
    it for the token and model lookups, and an operator who turns the boundary back
    off has to find it there.
    """
    from claude_on_the_fly import envfile

    if not thread_id:
        return False
    pattern = f"sessions/**/{rollout_glob(thread_id)}"
    home = home_dir(workspace)
    if any(home.glob(pattern)):
        return True
    source_root = envfile.codex_home() if shared is None else shared
    found = sorted(source_root.glob(pattern))
    if not found:
        logger.warning("codex: no rollout anywhere for thread=%s", thread_id)
        return False
    origin = found[-1]
    destination = home / origin.relative_to(source_root)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, temporary)
        os.replace(temporary, destination)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink()
        logger.warning("codex: cannot adopt rollout %s: %s", origin, exc)
        return False
    logger.info("codex: adopted rollout %s into %s", origin.name, destination.parent)
    return True


def clear_thread_id(workspace: Path, session_uuid: str) -> None:
    """Forget one mapping, so the next turn starts a thread codex can resume."""
    with suppress(OSError):
        mapping_path(workspace, session_uuid).unlink()


def read_thread_id(workspace: Path, session_uuid: str) -> str | None:
    """Read and validate the mapping for exactly this workspace/session."""
    path = mapping_path(workspace, session_uuid)
    record = _read_record(path)
    if record is None:
        return None
    if record.get("backend") != "codex":
        return None
    if record.get("session_uuid") != session_uuid:
        return None
    try:
        recorded_workspace = Path(str(record["workspace"])).resolve(strict=False)
    except (KeyError, OSError, ValueError):
        return None
    if recorded_workspace != workspace.resolve(strict=False):
        return None
    thread_id = record.get("thread_id")
    if not isinstance(thread_id, str) or not _valid_thread_id(thread_id.strip()):
        return None
    return thread_id.strip()


def write_thread_id(workspace: Path, session_uuid: str, thread_id: object) -> Path:
    """Atomically persist a validated Codex thread mapping."""
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Codex returned an empty thread id")
    clean = thread_id.strip()
    if not _valid_thread_id(clean):
        raise ValueError("Codex returned an invalid thread id")
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    MAPPINGS_DIR.chmod(stat.S_IRWXU)
    path = mapping_path(workspace, session_uuid)
    payload = json.dumps(
        {
            "backend": "codex",
            "workspace": str(workspace.resolve(strict=False)),
            "session_uuid": session_uuid,
            "thread_id": clean,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short write while storing Codex session mapping")
            offset += written
        os.fsync(fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp)
        raise
    finally:
        os.close(fd)
    os.replace(temp, path)
    os.chmod(path, 0o600)
    return path


def mappings_for_workspace(workspace: Path) -> list[tuple[Path, str, float]]:
    """Return validated mappings for a workspace, newest first."""
    found: list[tuple[Path, str, float]] = []
    canonical_workspace = workspace.resolve(strict=False)
    try:
        paths = list(MAPPINGS_DIR.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        record = _read_record(path)
        if record is None or record.get("backend") != "codex":
            continue
        session_uuid = record.get("session_uuid")
        if not isinstance(session_uuid, str):
            continue
        if path != mapping_path(workspace, session_uuid):
            continue
        try:
            recorded_workspace = Path(str(record["workspace"])).resolve(strict=False)
        except (KeyError, OSError, ValueError):
            continue
        if recorded_workspace != canonical_workspace:
            continue
        thread_id = read_thread_id(workspace, session_uuid)
        if thread_id is None:
            continue
        try:
            mtime = path.lstat().st_mtime
        except OSError:
            continue
        found.append((path, session_uuid, mtime))
    return sorted(found, key=lambda item: item[2], reverse=True)


def remove_workspace(workspace: Path) -> None:
    """Best-effort removal of all daemon-owned mappings for ``workspace``.

    The workspace's codex home goes too. It holds that thread's rollouts, and its
    name is derived from a path that will never exist again, so nothing else could
    ever reclaim it -- the same argument `transcript.remove_workspace_sessions`
    makes for claude's `projects/` directory.
    """
    canonical_workspace = workspace.resolve(strict=False)
    # Links first, so removing the tree cannot follow one into the shared ~/.codex.
    #
    # Addressed under HOMES_DIR rather than through `home_dir`, which answers with
    # the shared home when the session boundary is off. Retiring one workspace would
    # then delete the operator's whole ~/.codex. This name is derived from the
    # workspace either way, so a home left behind by an earlier scoped run is still
    # the one that goes.
    home = HOMES_DIR / _home_key(workspace)
    for link in [
        *(home / name for name in _SHARED_ENTRIES),
        *(entry for name in _SHARED_MERGED for entry in _safe_iterdir(home / name)),
    ]:
        if link.is_symlink():
            with suppress(OSError):
                link.unlink()
    shutil.rmtree(home, ignore_errors=True)
    try:
        paths = list(MAPPINGS_DIR.glob("*.json"))
    except OSError:
        return
    for path in paths:
        record = _read_record(path)
        if record is None or record.get("backend") != "codex":
            continue
        session_uuid = record.get("session_uuid")
        if not isinstance(session_uuid, str) or path != mapping_path(
            workspace, session_uuid
        ):
            continue
        try:
            recorded_workspace = Path(str(record["workspace"])).resolve(strict=False)
        except (KeyError, OSError, ValueError):
            continue
        if recorded_workspace != canonical_workspace:
            continue
        with suppress(OSError):
            path.unlink()
