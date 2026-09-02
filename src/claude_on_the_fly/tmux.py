"""Hosting agent turns in cotf's own tmux server, and mirroring their panes.

A hosted turn runs inside a tmux pane so something other than the turn itself can
see what the agent is doing: `capture` snapshots the pane's visible grid, and the
TUI renders it read-only. That is the whole feature. Nothing here types into a
pane; approvals already own that door (`permissions.PermissionService`).

**One server for every turn**, at a fixed socket under `panes_root()`, addressed
with `-S` and never with `TMUX_TMPDIR`. Two properties follow.

- **The operator's own tmux is untouched.** No cotf session appears in their
  `tmux ls`, and their `kill-server` cannot end a turn. This is why cotf keeps a
  socket at all rather than using the default server the way rhapsody does.
- **One address, so a writer and a reader cannot diverge.** `-S` beats an
  inherited `TMUX`; `TMUX_TMPDIR` does not. A daemon started from inside the
  operator's tmux used to create its panes on the operator's server while this
  module looked for them on a private socket, so a live turn read as a dead pane
  and leaked its agent. `argv_prefix` exists so `backends.codex`, which builds
  its own async argv, uses the identical address.

There used to be a server *per run*, and its third stated benefit was that a pane
inherits the daemon's curated env with nothing in argv. That does not survive
sharing: a pane on a server that was already running does not see the client's
environment at all (measured on tmux 3.7c). The replacement is not `-e KEY=VALUE`,
which would put `COTF_CMD_TOKEN` -- the bearer token for the broker that runs
credentialed CLIs *outside* the jail -- into a command line any local `ps` can
read. `backends.codex` sources a 0600 env file inside the pane instead, which is
what `claude-pty` has always done.

Teardown is `kill-session`, not `kill-server`: one server now holds every turn,
so ending it would take the others down too. tmux ends the pane's process group
with the session, so a reap still cannot miss a pane child (a pane is a child of
the tmux server, not of us).

`claude-pty` needs no change to take part: it calls bare `tmux`, so exporting
`TMUX_TMPDIR` into its spawn env puts its session on cotf's server too. It is the
one participant that cannot use `-S`, which is why a daemon must not pass an
inherited `TMUX` down to it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
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

# The socket address has to fit a unix address: `sun_path` is 104 bytes on macOS
# and 108 on Linux, and tmux appends `tmux-<uid>/default` to the root. Measured --
# a 96-character root yields a 113-byte socket and tmux fails the spawn with
# "File name too long", which costs the whole turn, not just the mirror.
_SUN_PATH_MAX = 104

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
    """One hosted turn: the session name it goes by on cotf's shared server.

    The name is the whole identity. Panes used to get a server each, addressed by
    a private socket directory, and the directory was the identity; one server
    for all of them means a name is enough, and `tmux list-sessions` becomes the
    register that a directory walk used to approximate.
    """

    session: str

    @property
    def env(self) -> dict[str, str]:
        """What a spawned agent needs so its tmux lands on cotf's server.

        Both keys, always. `CLAUDE_PTY_TMUX_SESSION` without `TMUX_TMPDIR` would
        put claude-pty's session on the operator's default server, which is the
        pollution this module exists to avoid; `TMUX_TMPDIR` without the session
        name would leave claude-pty naming its own session from its pid, which
        nothing outside the pane can predict.

        `TMUX_TMPDIR` rather than `-S` only because claude-pty takes no socket
        argument. Everything cotf runs itself uses `argv_prefix`, which is
        immune to an inherited `TMUX`; claude-pty is not, so a daemon started
        inside the operator's tmux must not pass `TMUX` down (`backends.codex`
        strips it).
        """
        return {
            "TMUX_TMPDIR": str(panes_root()),
            "CLAUDE_PTY_TMUX_SESSION": self.session,
        }


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
    """The directory holding cotf's tmux socket."""
    from claude_on_the_fly.agent import DATA_DIR

    return Path(DATA_DIR) / PANES_DIRNAME


def socket_path() -> Path:
    """cotf's tmux socket. One server for every pane, at a fixed address.

    Spelled `tmux-<uid>/default` under the root rather than a name of our own,
    because that is the path tmux itself builds from `TMUX_TMPDIR`. claude-pty
    creates its own session and can only be pointed at a server through that
    variable, so the socket has to sit where tmux would have put it. Everything
    in cotf that speaks to the server uses `-S` on this exact path, and
    claude-pty reaches the same server through `TMUX_TMPDIR=panes_root()`.
    """
    return panes_root() / f"tmux-{os.getuid()}" / "default"


def turn_file(session: str, suffix: str) -> Path:
    """A per-turn scratch path under the panes root, named for the pane.

    Keyed on the session name because that is the only identifier guaranteed
    unique per turn. These used to live in the run's own socket directory, which
    was per-run by construction; with one shared server there is no such
    directory, and the obvious replacement -- the workspace's `CODEX_HOME` --
    collapses onto the operator's shared `~/.codex` whenever
    `sandbox.scoped_sessions()` is off. Measured: a live job wrote
    `~/.codex/pane-env` and `~/.codex/pane-output`, so two concurrent turns would
    have clobbered one another. For `pane-env` that is a turn sourcing another
    turn's environment, `COTF_CMD_TOKEN` included.

    Under the panes root because the jail already grants that subtree, so a
    jailed pane can read the file with no profile change.
    """
    return panes_root() / f"{session}.{suffix}"


def argv_prefix() -> list[str]:
    """The `tmux` argv that addresses cotf's server, for callers outside this module.

    `backends.codex` builds its own `new-session`, `pipe-pane` and `wait-for`
    argv rather than going through `_run`, because those are async and carry a
    pane command. Exporting the prefix is what keeps the writer and the reader on
    one address: the split where the writer relied on `TMUX_TMPDIR` and the
    reader used `-S` is what put a live agent on the operator's server and then
    reported it dead.
    """
    return ["tmux", "-S", str(socket_path())]


def ensure_root() -> bool:
    """Create the socket directory, returning whether the address is usable.

    0700 because tmux refuses a socket directory group or world can write, and
    because the socket in it is a full command channel to every hosted agent.

    False when the address would not fit a unix socket. `sun_path` is 104 bytes
    on macOS and 108 on Linux, and returning True anyway hosted the turn
    regardless, so the agent's own tmux failed with "File name too long" --
    costing the turn, which is the opposite of what the warning promises.
    Unhosted is the degrade; the turn still runs.
    """
    projected = len(str(socket_path()))
    if projected > _SUN_PATH_MAX:
        logger.warning(
            "tmux: %s needs a %d-byte socket path and the limit is %d, so turns "
            "run unmirrored; point COTF_DATA_DIR somewhere shallower",
            socket_path(),
            projected,
            _SUN_PATH_MAX,
        )
        return False
    try:
        # The socket's own parent, not just the root: `-S` binds the exact path
        # given and creates nothing on the way, so the `tmux-<uid>` segment has to
        # be there first. tmux would have made it itself from `TMUX_TMPDIR`, which
        # is the hint form this module refuses to use. Measured as
        # "error creating .../tmux-501/default (No such file or directory)".
        directory = socket_path().parent
        directory.mkdir(parents=True, exist_ok=True)
        # 0700 on both: the root is what claude-pty is pointed at through
        # `TMUX_TMPDIR`, and the socket in the child is a full command channel to
        # every hosted agent. tmux refuses a socket directory group or world can
        # write, so this is its precondition as well as ours.
        panes_root().chmod(0o700)
        directory.chmod(0o700)
    except OSError as exc:
        logger.warning(
            "tmux: could not create %s (%s); turns run unmirrored",
            socket_path().parent,
            exc,
        )
        return False
    return True


def session_env(session: str) -> dict[str, str]:
    """What a spawned agent needs so its own tmux lands on cotf's server.

    Empty when hosting is unavailable, which is the meaningful answer rather than
    a failure: claude-pty then puts its session on the default server, and a
    caller that forced `TMUX_TMPDIR` at an address with no server would look for
    it somewhere it is not.
    """
    if not hosting_available() or not ensure_root():
        return {}
    return Pane(session=session).env


def pane_for(session: str) -> Pane | None:
    """The pane a run named `session` gets, creating the socket directory.

    None when the address is unusable, which every caller reads as an unhosted
    turn rather than a failed one.
    """
    if not ensure_root():
        return None
    return Pane(session=session)


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """One tmux command against cotf's server, or None when it could not run.

    Addressed with `-S`, never with `TMUX_TMPDIR`. tmux treats that variable as a
    hint for building a socket path and **silently falls back to the default
    socket when the directory it names does not exist** -- measured on 3.7b:
    `TMUX_TMPDIR=/nonexistent tmux kill-server` ends the operator's server and
    exits 0. `-S` names the socket outright: a missing path is an error, not a
    different server.

    `-S` also beats an inherited `TMUX`, which is the failure this addressing
    exists to prevent. A daemon started from inside the operator's tmux carries
    `TMUX` into every child, and a bare `tmux new-session` there lands on the
    operator's server no matter what `TMUX_TMPDIR` says. Measured: with `TMUX`
    set and `TMUX_TMPDIR` pointing at a fresh directory, `new-session` created
    nothing under that directory and the session appeared on the default server.
    Every caller in this module and in `backends.codex` goes through the same
    address for exactly that reason -- when the writer and the reader disagreed,
    a live turn read as a dead pane and leaked its agent.

    Never raises. Every caller here is either painting a UI or reaping a finished
    turn, and neither has anything useful to do with a tmux failure except carry
    on: a wedged server must not take down the turn that outlived it.
    """
    if not available():
        return None
    try:
        return subprocess.run(
            [*argv_prefix(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("tmux: %s failed: %s", args[0], exc)
        return None


def alive(pane: Pane) -> bool:
    """Whether a turn is still hosted in this pane right now."""
    result = _run(["has-session", "-t", pane.session], CONTROL_TIMEOUT_S)
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
    result = _run(["capture-pane", "-e", "-p", "-t", pane.session], CAPTURE_TIMEOUT_S)
    if result is None or result.returncode != 0:
        return None
    return trim_trailing_blank_rows(result.stdout)


def kill(pane: Pane) -> None:
    """End this pane's session. Best-effort.

    `kill-session`, never `kill-server`: cotf's panes share one server, so ending
    the server would take every other turn down with this one. tmux ends the
    pane's process group with the session, which is the reap this needs -- the
    agent and everything it spawned go together.

    Also what a backend calls when its turn is over but the run is not: a nudge
    retry opens a session with this same name, and `tmux new-session` refuses a
    duplicate, so leaving it alive turned every retry into "tmux refused to host
    the codex turn".
    """
    _run(["kill-session", "-t", pane.session], CONTROL_TIMEOUT_S)


def live_panes() -> list[Pane]:
    """Every session on cotf's server, which is every pane it is hosting.

    One `list-sessions`, not a directory walk: the server is the register. A
    viewer in another process (the TUI) needs no agreement with the daemon beyond
    `socket_path()`, and the names come from the server rather than from anything
    we cached, so a session tmux renamed still reports truthfully.

    Every session here is cotf's by construction -- the operator's own sessions
    live on the default socket, which this never addresses.
    """
    result = _run(["list-sessions", "-F", "#{session_name}"], CONTROL_TIMEOUT_S)
    if result is None or result.returncode != 0:
        return []
    return [
        Pane(session=name)
        for name in (row.strip() for row in result.stdout.splitlines())
        if name
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
    # One scan for all of them. This sits on the TUI's 1Hz refresh, so asking
    # twice doubled the subprocesses per frame for no more information.
    for pane in live_panes():
        if any(pane.session.startswith(prefix) for prefix in prefixes):
            return pane
    return None


def sweep(prefix: str) -> int:
    """Reap this daemon's leftover sessions, returning how many. Best-effort.

    Called at daemon startup. A pane is a child of the shared server rather than
    of the daemon, so it survives a daemon that dies mid-turn and would otherwise
    sit there holding an agent process forever.

    `prefix` is what keeps one daemon from reaping another's live turn. Every
    session name is owned by exactly one daemon -- `cotf-job-` by the jobs worker,
    `cotf-pty-` by the orchestrator -- so a daemon that only reaps its own prefix
    needs no liveness handshake with its siblings. That segmentation replaced the
    `owner.pid` file the per-run directories used to carry: with one shared
    server there is no directory to write it in, and a name we already control
    answers the same question.
    """
    reaped = 0
    for pane in live_panes():
        if not pane.session.startswith(prefix):
            continue
        kill(pane)
        reaped += 1
    if reaped:
        logger.info("tmux: swept %d leftover %s session(s)", reaped, prefix)
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

    `TMUX_TMPDIR` is still what marks a spawn as hosted, even though every pane
    now shares one server: claude-pty builds its own session and has no other way
    to be pointed at cotf's socket rather than the operator's.
    """
    if not env:
        return None
    if not env.get("TMUX_TMPDIR"):
        return None
    session = env.get("CLAUDE_PTY_TMUX_SESSION")
    if not session:
        return None
    return Pane(session=session)
