"""Hosting one agent turn in a private tmux server, and mirroring its pane.

A hosted turn runs inside a tmux pane so something other than the turn itself can
see what the agent is doing: `capture` snapshots the pane's visible grid, and the
TUI renders it read-only. That is the whole feature. Nothing here types into a
pane; approvals already own that door (`permissions.PermissionService`).

**Every run gets its own tmux server**, addressed by `TMUX_TMPDIR` rather than by
`-L`. Three things fall out of that, and all three are why it is worth a directory
per run:

- **The pane inherits the curated env with nothing in argv.** A pane on a server
  that was already running does not see the client's environment at all (measured
  on tmux 3.7c), so the alternative is `tmux new-session -e KEY=VALUE` per pair —
  which would put `COTF_CMD_TOKEN`, the bearer token for the broker that runs
  credentialed CLIs *outside* the jail, into a command line any local `ps` can
  read. A server this daemon starts inherits the daemon's spawn env, and its panes
  inherit that. So `sandbox.agent_env()` reaches the agent unchanged and unexposed.
- **Teardown is total.** `kill-server` on the run's socket directory ends the
  session, its panes and the server, so a reap cannot miss a pane the agent
  spawned. Killing by session name leaves the server, and a process-group kill
  never reached a pane child in the first place (a pane is a child of the tmux
  server, not of us).
- **The operator's own tmux is untouched.** No cotf session appears in their
  `tmux ls`, and their `kill-server` cannot end a turn.

`claude-pty` needs no change to take part: it calls bare `tmux`, so exporting
`TMUX_TMPDIR` into its spawn env puts its session on the private server too.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rich.style import Style
from rich.text import Text

logger = logging.getLogger(__name__)

# Where a run's socket directory lives. Under DATA_DIR because the jail already
# grants that subtree for writing (`sandbox.py`), so a jailed agent can create its
# own server there with no profile change, and because a socket under TMPDIR would
# be swept by the OS while a long turn is still running.
PANES_DIRNAME = "panes"

# Session name for a background job's pane. Chat turns are named by
# `permissions.tmux_session_name`, which the approval path already owns; a job
# has no approval service, so its name lives here. The run id is the workspace's
# own directory name, which is what lets the TUI reconstruct the name from a row
# it is already showing.
JOB_SESSION_PREFIX = "cotf-job-"


def job_session_name(run_id: str) -> str:
    """The pane name for one background job run."""
    return f"{JOB_SESSION_PREFIX}{run_id}"


# Floor on the rows a capture reflows the window to. tmux's own default for a
# detached session is 24, so this asks the agent for no less than the screenful it
# would have drawn had nobody mirrored it. The window is shared with every other
# viewer, so a short viewport must not pin the agent's own view at its height: a
# viewport shorter than this becomes a window onto the grid instead.
MIRROR_MIN_ROWS = 24

# A capture is on the path of a UI repaint, so it gets its own short deadline
# rather than tmux's internal 5s one: a wedged server must cost a stale frame, not
# a frozen pane. Control operations are off that path and can afford to wait.
CAPTURE_TIMEOUT_S = 1.5
CONTROL_TIMEOUT_S = 5.0

# A run's directory is named for a digest of its session, not for the session
# itself, because the whole socket path has to fit in a unix address: `sun_path`
# is 104 bytes on macOS and 108 on Linux, and tmux appends `tmux-<uid>/default`
# to whatever `TMUX_TMPDIR` says. Measured — a 96-character directory yields a
# 113-byte socket and tmux fails the spawn with "File name too long", which
# costs the whole turn, not just the mirror. Twelve hex characters need ~16
# million panes for a collision and leave a redirected COTF_DATA_DIR roughly 38
# characters of headroom under the default one.
#
# Nothing reads the human session name back out of the directory name as a
# result. `_sessions_on` asks the server instead, which is the better source
# anyway: it reports the name tmux actually gave the session.
_DIR_CHARS = 12
_SUN_PATH_MAX = 104

# How long a run directory is left alone before a sweep may reap it. Long enough
# to cover the gap between the daemon creating it and the agent's tmux starting
# the server in it, which is the window a concurrent sweep would otherwise take
# a live turn's socket directory in.
_SWEEP_GRACE_S = 120.0


# OSC (window-title and friends). `Text.from_ansi` drops the introducer but leaves
# the *payload* as visible text, so `ESC]0;title BEL` would print "0;title" — which
# also makes a row holding nothing else look like a row the agent drew.
#
# The class excludes `\n` as well as the two terminators, so an *unterminated*
# `ESC]` is bounded to its own row. Without that the match runs to the next escape
# anywhere in the capture and takes whole rows with it. Rows are what the viewer
# counts and rests on, so a stray byte must never be able to delete one.
_OSC = re.compile(r"\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)?")


@dataclass(frozen=True)
class Pane:
    """One run's private tmux server: where its socket lives, and the session on it.

    Constructed by `pane_for` at spawn time and carried for the life of the turn.
    The socket directory is the identity — the session name is only what the
    server calls its one session, and `claude-pty` picks that name itself from the
    environment we hand it.
    """

    tmpdir: Path
    session: str

    @property
    def env(self) -> dict[str, str]:
        """What a spawned agent needs so its tmux lands on this server.

        Both keys, always. `CLAUDE_PTY_TMUX_SESSION` without `TMUX_TMPDIR` would
        put claude-pty's session on the operator's default server, which is the
        pollution this module exists to avoid; `TMUX_TMPDIR` without the session
        name would leave claude-pty naming its own session from its pid, which
        nothing outside the pane can predict.
        """
        return {
            "TMUX_TMPDIR": str(self.tmpdir),
            "CLAUDE_PTY_TMUX_SESSION": self.session,
        }


# Values that turn hosting off. Anything else, including an absent setting,
# leaves it on: this is an escape hatch rather than a feature flag, so the
# failure mode of a typo has to be "still mirrored" rather than "silently not".
_FALSY = frozenset({"0", "false", "no", "off"})

# The setting that switches hosting off. Read per turn rather than bound to a
# constant, so an operator's edit takes effect on the next turn: nothing here
# binds a socket or constructs a service, so it needs no restart and is
# deliberately absent from `settings.RESTART_REQUIRED`.
PANE_VAR = "COTF_AGENT_PANE"


def hosting_enabled() -> bool:
    """Whether turns should be hosted in a pane at all. On unless switched off."""
    from claude_on_the_fly import settings

    return settings.get(PANE_VAR).strip().lower() not in _FALSY


def available() -> bool:
    """Whether tmux is on PATH. A turn is hosted only when it is."""
    return shutil.which("tmux") is not None


def hosting_available() -> bool:
    """Whether this turn gets a pane: the operator allows it and tmux exists.

    One question with one answer, so the two producers cannot come to different
    conclusions about the same turn.
    """
    return hosting_enabled() and available()


def panes_root() -> Path:
    """The directory holding every run's socket directory."""
    from claude_on_the_fly.agent import DATA_DIR

    return Path(DATA_DIR) / PANES_DIRNAME


def socket_path(tmpdir: Path) -> Path:
    """Where tmux will put the server's socket under `tmpdir`.

    Spelled out so the length check below measures the real address rather than
    the directory, and so a maintainer can see that the `tmux-<uid>` segment is
    tmux's own naming, not ours.
    """
    return tmpdir / f"tmux-{os.getuid()}" / "default"


def pane_dir(session: str) -> Path:
    """Where a run named `session` keeps its socket, without creating anything.

    Pure so a caller that must not create a pane can still address one. The
    approval path needs exactly that: it has to reach the server hosting a turn,
    and whether the directory exists is also how it tells a hosted turn from one
    on the operator's default server.
    """
    return (
        panes_root()
        / hashlib.blake2b(session.encode(), digest_size=8).hexdigest()[:_DIR_CHARS]
    )


def session_env(session: str) -> dict[str, str]:
    """`TMUX_TMPDIR` for a hosted session, or nothing when it is not hosted.

    Empty is the meaningful answer, not a failure: with hosting off, or before
    this build existed, claude-pty puts its session on the default server, and a
    caller that forced TMUX_TMPDIR would look for it on a server that has it not.
    """
    directory = pane_dir(session)
    return {"TMUX_TMPDIR": str(directory)} if directory.is_dir() else {}


def pane_for(session: str) -> Pane | None:
    """The pane a run named `session` gets, creating its socket directory.

    The directory is 0700 because tmux refuses a socket directory group or world
    can write, and because the socket in it is a full command channel to the
    server hosting the agent.
    """
    tmpdir = pane_dir(session)
    projected = len(str(socket_path(tmpdir)))
    if projected > _SUN_PATH_MAX:
        # None, not a pane. Returning one anyway published TMUX_TMPDIR and hosted
        # the turn regardless, so the agent's own tmux failed with "File name too
        # long" -- costing the turn, which is the opposite of what this warning
        # promises. Unhosted is the degrade; the turn still runs.
        logger.warning(
            "tmux: %s needs a %d-byte socket path and the limit is %d, so this turn "
            "runs unmirrored; point COTF_DATA_DIR somewhere shallower",
            tmpdir,
            projected,
            _SUN_PATH_MAX,
        )
        return None
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmpdir.chmod(0o700)
    return Pane(tmpdir=tmpdir, session=session)


def _run(
    pane: Pane, args: list[str], timeout: float
) -> subprocess.CompletedProcess | None:
    """One tmux command against `pane`'s server, or None when it could not run.

    Never raises. Every caller here is either painting a UI or reaping a finished
    turn, and neither has anything useful to do with a tmux failure except carry
    on: a wedged server must not take down the turn that outlived it.
    """
    if not available():
        return None
    try:
        return subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "TMUX_TMPDIR": str(pane.tmpdir)},
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("tmux: %s failed on %s: %s", args[0], pane.session, exc)
        return None


def alive(pane: Pane) -> bool:
    """Whether a turn is still hosted in this pane right now."""
    result = _run(pane, ["has-session", "-t", pane.session], CONTROL_TIMEOUT_S)
    return result is not None and result.returncode == 0


def capture(
    pane: Pane, *, cols: int | None = None, rows: int | None = None
) -> str | None:
    """The pane's visible grid as ANSI text, or None when there is nothing to show.

    None rather than an exception for every failure the viewer cannot act on: tmux
    absent, the turn finished so the session is gone, or the server not answering
    inside the deadline. A viewer showing the previous frame is the right response
    to all three.

    `cols`/`rows` reflow the window before capturing, so a viewer wider than 80
    columns gets the agent's output laid out for its own width rather than
    line-wrapped at tmux's detached default. The reflow is a real side effect on a
    window every other viewer shares, which is why `rows` has a floor: see
    `MIRROR_MIN_ROWS`.
    """
    if cols and rows:
        _run(
            pane,
            [
                "resize-window",
                "-t",
                pane.session,
                "-x",
                str(cols),
                "-y",
                str(max(rows, MIRROR_MIN_ROWS)),
            ],
            CONTROL_TIMEOUT_S,
        )
    result = _run(
        pane, ["capture-pane", "-e", "-p", "-t", pane.session], CAPTURE_TIMEOUT_S
    )
    if result is None or result.returncode != 0:
        return None
    return trim_trailing_blank_rows(result.stdout)


def kill_session(pane: Pane) -> None:
    """End the session, leaving the server and the run directory in place.

    What a backend calls when its turn is over but the run is not: a nudge retry
    opens a new session with the same name, and `tmux new-session` refuses a
    duplicate. The server exits by itself once its last session goes.
    """
    _run(pane, ["kill-session", "-t", pane.session], CONTROL_TIMEOUT_S)


def kill(pane: Pane) -> None:
    """End the run's server and remove its socket directory. Best-effort.

    `kill-server` rather than `kill-session`: the session is the only one on this
    server, so ending the server ends the pane, every process the pane spawned,
    and the server itself in one call. Leaving the server behind would leak a
    process per turn.
    """
    _run(pane, ["kill-server"], CONTROL_TIMEOUT_S)
    with contextlib.suppress(OSError):
        shutil.rmtree(pane.tmpdir)


def _sessions_on(tmpdir: Path) -> list[str]:
    """The session names a server in `tmpdir` reports, empty when it answers nothing.

    Empty covers every "no pane here" case at once: a directory whose server died
    with the daemon that started it, a directory tmux never got as far as using,
    and a server too wedged to reply. All three mean the same thing to both
    callers — nothing to mirror, safe to reap.
    """
    result = _run(
        Pane(tmpdir=tmpdir, session=""),
        ["list-sessions", "-F", "#{session_name}"],
        # The capture deadline, not the control one: this runs on the TUI's 1Hz
        # refresh, once per run directory, so a wedged server has to cost a stale
        # frame rather than five seconds of frozen dashboard.
        CAPTURE_TIMEOUT_S,
    )
    if result is None or result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _run_dirs() -> list[Path]:
    """Every run directory under the panes root, in a stable order."""
    root = panes_root()
    if not root.is_dir():
        return []
    return [entry for entry in sorted(root.iterdir()) if entry.is_dir()]


def live_panes() -> list[Pane]:
    """Every pane with a server still answering.

    Discovery is a directory listing plus one `list-sessions` per directory,
    rather than a state file, so a viewer in another process (the TUI) needs no
    agreement with the daemon beyond `panes_root()`. The session name comes from
    the server rather than from the directory name, which is a digest.
    """
    return [
        Pane(tmpdir=tmpdir, session=name)
        for tmpdir in _run_dirs()
        for name in _sessions_on(tmpdir)
    ]


def pane_named(*prefixes: str) -> Pane | None:
    """The live pane whose session starts with `prefix`, or None.

    A prefix rather than an exact name because a viewer knows less than the
    daemon did. A chat pane is `cotf-pty-<chat>-<session digest>`, and the TUI
    has the chat id but not the discriminator the daemon minted; matching on
    `cotf-pty-<chat>-` finds it without the TUI having to track sessions the
    orchestrator owns. A job pane is named for its run, which the TUI does know
    in full, so the prefix is the whole name there.
    """
    if not prefixes:
        return None
    # One scan for all of them. Each call costs a `tmux list-sessions` per run
    # directory and this sits on the TUI's 1Hz refresh, so asking twice doubled
    # the subprocesses per frame for no more information.
    for pane in live_panes():
        if any(pane.session.startswith(prefix) for prefix in prefixes):
            return pane
    return None


def sweep() -> int:
    """Reap run directories whose server is gone, returning how many.

    Called at daemon startup. A turn killed with its daemon leaves both a server
    and a directory: the server dies with the pane's parent, but the directory
    stays and would otherwise accumulate one per turn forever.
    """
    reaped = 0
    cutoff = time.time() - _SWEEP_GRACE_S
    for tmpdir in _run_dirs():
        if _sessions_on(tmpdir):
            continue
        # A directory is created at the top of a turn and the server appears a
        # moment later, when the agent's own tmux runs. Both daemons sweep this
        # shared root, so a worker restarting inside that window would otherwise
        # reap a chat turn that is just starting and leave its TMUX_TMPDIR gone.
        try:
            if tmpdir.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        kill(Pane(tmpdir=tmpdir, session=""))
        reaped += 1
    if reaped:
        logger.info("tmux: swept %d dead run director(ies)", reaped)
    return reaped


def _paints(style: Style | str | None) -> bool:
    """True when a run with no visible characters still puts ink on the screen."""
    if style is None:
        return False
    if isinstance(style, str):
        style = Style.parse(style)
    return bool(
        style.reverse or (style.bgcolor is not None and not style.bgcolor.is_default)
    )


def _is_blank(row: str) -> bool:
    """True when a row renders as nothing at all.

    Blankness is by *rendered* result, not by string comparison — tmux commonly
    pads with styled spaces, which are not empty as a string but are empty on
    screen. Spaces carrying a background or reverse video are the exception: they
    have no characters but do paint a solid bar, and dropping one would remove a
    row the operator can see.
    """
    text = Text.from_ansi(row)
    if text.plain.strip():
        return False
    return not any(_paints(span.style) for span in text.spans)


def trim_trailing_blank_rows(text: str) -> str:
    """Normalize a capture to the rows the agent actually drew.

    A pane showing six lines of output still yields its full grid, so the tail is
    rows of nothing, and a viewer resting on the end would rest on emptiness
    instead of on the newest output.

    OSC payloads are stripped first and the result carries that stripping, so
    every viewer parses the same string: `Text.from_ansi` leaves an OSC payload as
    visible text, so a trailing row holding only `ESC]0;title BEL` would answer
    "the agent drew this" for one viewer and "it did not" for another.
    """
    rows = _OSC.sub("", text).split("\n")
    while rows and _is_blank(rows[-1]):
        rows.pop()
    return "\n".join(rows)


def pane_from_env(env: Mapping[str, str] | None) -> Pane | None:
    """The pane a spawn's environment points at, or None when it is not hosted.

    How a backend learns where to host itself. The daemon publishes the pane
    through `sandbox.session_env`, so the same values that tell `claude-pty`
    which session to create tell a backend that builds its own `tmux
    new-session` where to build it. Passing a `Pane` down through `agent.run`
    instead would put a tmux argument in the signature of every backend,
    including the ones that never host anything.

    The session key keeps claude-pty's name because claude-pty reads it and is
    vendored; it is the generic "this run's pane" value despite the spelling.
    """
    if not env:
        return None
    tmpdir = env.get("TMUX_TMPDIR")
    session = env.get("CLAUDE_PTY_TMUX_SESSION")
    if not tmpdir or not session:
        return None
    return Pane(tmpdir=Path(tmpdir), session=session)
