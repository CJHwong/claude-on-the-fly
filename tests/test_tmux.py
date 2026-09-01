"""Private-server tmux hosting and pane capture."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_on_the_fly import tmux

# Long enough for a loaded CI runner to start a shell and flush its first line,
# short enough that a genuine regression fails the test rather than hanging it.
_PROBE_DEADLINE_S = 10.0

HAS_TMUX = shutil.which("tmux") is not None
needs_tmux = pytest.mark.skipif(not HAS_TMUX, reason="tmux is not installed")


@pytest.fixture
def panes_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "panes"
    monkeypatch.setattr(tmux, "panes_root", lambda: root)
    return root


@pytest.fixture
def short_panes_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A panes root shallow enough for a real socket.

    pytest's own `tmp_path` is ~100 characters on macOS, which spends the whole
    104-byte `sun_path` budget before tmux appends anything.
    """
    root = Path(tempfile.mkdtemp(prefix="cotf-t-"))
    monkeypatch.setattr(tmux, "panes_root", lambda: root)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _done(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["tmux"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_pane_env_carries_both_keys_so_claude_pty_lands_on_the_private_server():
    pane = tmux.Pane(tmpdir=Path("/tmp/sock"), session="cotf-pty-7-abcd")

    assert pane.env == {
        "TMUX_TMPDIR": "/tmp/sock",
        "CLAUDE_PTY_TMUX_SESSION": "cotf-pty-7-abcd",
    }


def test_pane_for_creates_a_private_directory(short_panes_root: Path):
    pane = tmux.pane_for("cotf-pty-7-abcd")

    assert pane.tmpdir.is_dir()
    assert pane.tmpdir.stat().st_mode & 0o777 == 0o700
    assert pane.session == "cotf-pty-7-abcd"


def test_pane_for_keeps_the_directory_short_enough_for_a_unix_socket(
    short_panes_root: Path,
):
    """A session name in the path costs a whole turn, not just the mirror: tmux fails
    the spawn with "File name too long" once the socket exceeds sun_path."""
    pane = tmux.pane_for("job/jira/ACE-1234-a-very-long-key-" + "x" * 120)

    assert pane is not None
    assert pane.tmpdir.parent == short_panes_root
    assert len(pane.tmpdir.name) == 12


def test_pane_for_warns_when_the_data_dir_is_too_deep_for_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    deep = tmp_path / ("d" * 90)
    monkeypatch.setattr(tmux, "panes_root", lambda: deep)

    with caplog.at_level("WARNING"):
        pane = tmux.pane_for("cotf-chat-1")

    assert "COTF_DATA_DIR" in caplog.text
    # None, not a pane. Returning one published TMUX_TMPDIR and hosted the turn
    # anyway, so the agent's own tmux failed with "File name too long" -- the
    # turn, not just its mirror.
    assert pane is None
    assert not deep.exists()


def test_pane_for_says_nothing_when_the_path_fits(
    short_panes_root: Path, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level("WARNING"):
        assert tmux.pane_for("cotf-chat-1") is not None

    assert caplog.text == ""


def test_pane_for_is_idempotent_so_a_retry_reuses_the_directory(short_panes_root: Path):
    first = tmux.pane_for("cotf-job-1")
    second = tmux.pane_for("cotf-job-1")

    assert first == second


def test_run_declines_when_tmux_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: None)
    pane = tmux.Pane(tmpdir=tmp_path, session="s")

    assert tmux._run(pane, ["has-session"], 1.0) is None


def test_run_swallows_a_wedged_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")

    def explode(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=1.5)

    monkeypatch.setattr(tmux.subprocess, "run", explode)
    pane = tmux.Pane(tmpdir=tmp_path, session="s")

    assert tmux._run(pane, ["capture-pane"], 1.5) is None


def test_run_points_tmux_at_the_panes_own_socket_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")
    seen: dict = {}

    def record(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return _done()

    monkeypatch.setattr(tmux.subprocess, "run", record)
    pane = tmux.Pane(tmpdir=tmp_path, session="s")

    tmux._run(pane, ["has-session", "-t", "s"], 1.0)

    socket = str(tmux.socket_path(tmp_path))
    assert seen["argv"] == ["tmux", "-S", socket, "has-session", "-t", "s"]


def test_run_names_the_socket_so_a_reaped_directory_cannot_hit_the_default_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The bug this addressing exists for.

    tmux reads TMUX_TMPDIR as a hint and falls back to the default socket when the
    directory it names is gone -- so a `kill-server` meant for one finished turn
    ended the operator's own server instead. A concurrent `sweep` removes that
    directory, so the race is reachable, not theoretical.
    """
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")
    seen: dict = {}

    def record(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return _done()

    monkeypatch.setattr(tmux.subprocess, "run", record)
    gone = tmp_path / "already-reaped"
    pane = tmux.Pane(tmpdir=gone, session="s")

    tmux._run(pane, ["kill-server"], 1.0)

    assert seen["argv"][:3] == ["tmux", "-S", str(tmux.socket_path(gone))]
    # No hint form anywhere: an inherited TMUX_TMPDIR must not decide the target.
    assert seen["env"] is None or "TMUX_TMPDIR" not in seen["env"]


def test_run_keeps_the_env_out_of_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The whole reason for a private server: no `-e KEY=VALUE` pairs to read from `ps`."""
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setenv("COTF_CMD_TOKEN", "super-secret")
    seen: dict = {}

    def record(argv, **kwargs):
        seen["argv"] = argv
        return _done()

    monkeypatch.setattr(tmux.subprocess, "run", record)

    tmux._run(tmux.Pane(tmpdir=tmp_path, session="s"), ["new-session", "-d"], 1.0)

    assert not any("super-secret" in part for part in seen["argv"])


def test_alive_reads_the_probes_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    pane = tmux.Pane(tmpdir=tmp_path, session="s")
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: _done(returncode=0))
    assert tmux.alive(pane) is True

    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: _done(returncode=1))
    assert tmux.alive(pane) is False

    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)
    assert tmux.alive(pane) is False


def test_capture_returns_none_when_the_turn_has_finished(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: _done(returncode=1))

    assert tmux.capture(tmux.Pane(tmpdir=tmp_path, session="gone")) is None


def test_capture_returns_none_when_tmux_cannot_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)

    assert tmux.capture(tmux.Pane(tmpdir=tmp_path, session="s")) is None


def test_capture_trims_the_rows_the_agent_never_drew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(
        tmux, "_run", lambda *_a, **_k: _done(stdout="hello\n   \n   \n")
    )

    assert tmux.capture(tmux.Pane(tmpdir=tmp_path, session="s")) == "hello"


def test_capture_reflows_to_the_viewer_but_never_below_the_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls: list[list[str]] = []

    def record(_pane, args, _timeout):
        calls.append(args)
        return _done(stdout="out")

    monkeypatch.setattr(tmux, "_run", record)

    tmux.capture(tmux.Pane(tmpdir=tmp_path, session="s"), cols=200, rows=4)

    assert calls[0] == [
        "resize-window",
        "-t",
        "s",
        "-x",
        "200",
        "-y",
        str(tmux.MIRROR_MIN_ROWS),
    ]
    assert calls[1][0] == "capture-pane"


def test_capture_does_not_resize_when_the_viewer_gives_no_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls: list[list[str]] = []

    def record(_pane, args, _timeout):
        calls.append(args)
        return _done(stdout="out")

    monkeypatch.setattr(tmux, "_run", record)

    tmux.capture(tmux.Pane(tmpdir=tmp_path, session="s"))

    assert [args[0] for args in calls] == ["capture-pane"]


def test_kill_ends_the_server_and_removes_the_directory(
    monkeypatch: pytest.MonkeyPatch, short_panes_root: Path
):
    calls: list[list[str]] = []
    monkeypatch.setattr(tmux, "_run", lambda _p, args, _t: calls.append(args))
    pane = tmux.pane_for("cotf-job-1")

    tmux.kill(pane)

    assert calls == [["kill-server"]]
    assert not pane.tmpdir.exists()


def test_kill_survives_a_directory_that_is_already_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)

    tmux.kill(tmux.Pane(tmpdir=tmp_path / "never-made", session="s"))


def test_live_panes_is_empty_before_any_turn_runs(panes_root: Path):
    assert tmux.live_panes() == []


def test_live_panes_names_each_session_from_its_own_server(
    monkeypatch: pytest.MonkeyPatch, short_panes_root: Path
):
    """The directory is a digest, so the human name can only come from tmux."""
    live = tmux.pane_for("cotf-chat-1")
    tmux.pane_for("cotf-chat-2")
    (short_panes_root / "stray-file").write_text("not a socket directory")
    monkeypatch.setattr(
        tmux, "_sessions_on", lambda d: ["cotf-chat-1"] if d == live.tmpdir else []
    )

    assert tmux.live_panes() == [live]


def test_sweep_reaps_the_leftovers_of_a_killed_daemon(
    monkeypatch: pytest.MonkeyPatch, short_panes_root: Path
):
    dead = tmux.pane_for("cotf-chat-dead")
    live = tmux.pane_for("cotf-chat-live")
    assert dead is not None and live is not None
    # Older than the grace: a directory younger than that belongs to a turn that
    # may not have started its server yet.
    aged = time.time() - tmux._SWEEP_GRACE_S - 1
    os.utime(dead.tmpdir, (aged, aged))
    # A leftover is one whose daemon died; `pane_for` stamped both with this
    # live process, so the dead one has to name a pid that is really gone.
    (dead.tmpdir / tmux._OWNER_FILE).write_text("0\n", encoding="utf-8")
    (short_panes_root / "stray-file").write_text("not a socket directory")
    monkeypatch.setattr(
        tmux, "_sessions_on", lambda d: ["cotf-chat-live"] if d == live.tmpdir else []
    )
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)

    assert tmux.sweep() == 1
    assert not dead.tmpdir.exists()
    assert live.tmpdir.exists()


def test_sweep_spares_a_live_turn_whose_backend_never_starts_a_server(
    monkeypatch: pytest.MonkeyPatch, short_panes_root: Path
):
    """`agent.codex.mode: native` runs the agent outside the pane, so its directory
    never gets a server and looks identical to a leftover for the whole turn. Only
    the owner tells them apart, and the mtime grace runs out after two minutes."""
    live = tmux.pane_for("cotf-chat-native")
    assert live is not None
    aged = time.time() - tmux._SWEEP_GRACE_S - 1
    os.utime(live.tmpdir, (aged, aged))
    monkeypatch.setattr(tmux, "_sessions_on", lambda _d: [])
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)

    assert tmux.sweep() == 0
    assert live.tmpdir.exists()


def test_sweep_reaps_a_directory_whose_owner_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, short_panes_root: Path
):
    """Directories written before the owner stamp existed keep the old behavior
    rather than being stranded forever."""
    stale = short_panes_root / "0123456789ab"
    stale.mkdir()
    aged = time.time() - tmux._SWEEP_GRACE_S - 1
    os.utime(stale, (aged, aged))
    monkeypatch.setattr(tmux, "_sessions_on", lambda _d: [])
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)

    assert tmux.sweep() == 1
    assert not stale.exists()


def test_pane_for_stamps_the_owner_so_a_sibling_sweep_can_tell_it_is_live(
    short_panes_root: Path,
):
    pane = tmux.pane_for("cotf-chat-owned")
    assert pane is not None
    assert (pane.tmpdir / tmux._OWNER_FILE).read_text().strip() == str(os.getpid())


def test_sweep_is_a_no_op_before_any_turn_runs(panes_root: Path):
    assert tmux.sweep() == 0


def test_trim_keeps_a_row_that_paints_a_bar_with_no_characters():
    """A styled blank row is visible, so trimming it would delete something real."""
    bar = "\x1b[42m      \x1b[0m"

    assert tmux.trim_trailing_blank_rows(f"text\n{bar}\n   \n") == f"text\n{bar}"


def test_trim_drops_tmuxs_padding_of_unstyled_spaces():
    assert tmux.trim_trailing_blank_rows("text\n     \n\n") == "text"


def test_trim_strips_an_osc_payload_instead_of_showing_it_as_a_row():
    assert tmux.trim_trailing_blank_rows("text\n\x1b]0;a title\x07\n") == "text"


def test_trim_bounds_an_unterminated_osc_to_its_own_row():
    """Without the newline exclusion the match eats every row up to the next escape."""
    grid = "row1\x1b]bare\nrow2\nrow3\x1b[0m tail"

    assert tmux.trim_trailing_blank_rows(grid) == "row1\nrow2\nrow3\x1b[0m tail"


def test_trim_leaves_a_grid_the_agent_filled_completely():
    assert tmux.trim_trailing_blank_rows("a\nb\nc") == "a\nb\nc"


@needs_tmux
def test_a_real_pane_inherits_the_curated_env_and_can_be_captured(
    short_panes_root: Path,
):
    """The measured basis for the whole design, kept as a test so a tmux upgrade cannot
    quietly take it away: a server this process starts hands its env to the pane, with
    nothing on any command line."""
    pane = tmux.pane_for("cotf-live-probe")
    try:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                pane.session,
                "printenv COTF_PROBE_TOKEN; sleep 30",
            ],
            env={**os.environ, **pane.env, "COTF_PROBE_TOKEN": "inherited"},
            check=True,
            capture_output=True,
        )
        assert tmux.alive(pane) is True

        # A live session does not mean the shell has drawn yet: `alive` asks
        # whether the server has the session, and on a loaded runner `printenv`
        # can still be ahead of its first write. Capturing once read an empty
        # grid there and failed a claim about the environment. Poll instead.
        deadline = time.monotonic() + _PROBE_DEADLINE_S
        grid = ""
        while time.monotonic() < deadline:
            grid = tmux.capture(pane)
            assert grid is not None
            if "inherited" in grid:
                break
            time.sleep(0.05)

        assert "inherited" in grid
    finally:
        tmux.kill(pane)

    assert tmux.alive(pane) is False
    assert not pane.tmpdir.exists()


@needs_tmux
def test_a_real_pane_is_invisible_to_the_operators_own_tmux(short_panes_root: Path):
    pane = tmux.pane_for("cotf-isolation-probe")
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", pane.session, "sleep 30"],
            env={**os.environ, **pane.env},
            check=True,
            capture_output=True,
        )
        default = subprocess.run(
            ["tmux", "list-sessions"], capture_output=True, text=True, check=False
        )

        assert pane.session not in default.stdout
    finally:
        tmux.kill(pane)


def test_available_follows_the_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: None)
    assert tmux.available() is False

    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")
    assert tmux.available() is True


def test_panes_root_hangs_off_the_data_dir():
    from claude_on_the_fly.agent import DATA_DIR

    assert tmux.panes_root() == Path(DATA_DIR) / tmux.PANES_DIRNAME


def test_sessions_on_reads_the_names_the_server_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: _done(stdout="cotf-chat-1\n\n"))

    assert tmux._sessions_on(tmp_path) == ["cotf-chat-1"]


def test_sessions_on_reads_a_dead_or_wedged_server_as_nothing_to_mirror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: _done(returncode=1))
    assert tmux._sessions_on(tmp_path) == []

    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)
    assert tmux._sessions_on(tmp_path) == []


def test_paints_accepts_a_style_however_rich_hands_it_over():
    """`Text.from_ansi` spans carry a Style or its name, and an unstyled run carries None."""
    assert tmux._paints(None) is False
    assert tmux._paints("on green") is True
    assert tmux._paints("bold") is False


def test_pane_named_finds_a_run_from_the_prefix_a_viewer_knows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The TUI has the chat id but not the session discriminator the daemon minted."""
    chat = tmux.Pane(tmpdir=tmp_path / "a", session="cotf-pty-777-9f3c1a2b")
    job = tmux.Pane(tmpdir=tmp_path / "b", session="cotf-job-run-1")
    monkeypatch.setattr(tmux, "live_panes", lambda: [chat, job])

    assert tmux.pane_named("cotf-pty-777-") == chat
    assert tmux.pane_named(tmux.job_session_name("run-1")) == job
    assert tmux.pane_named("cotf-pty-778-") is None


def test_pane_from_env_reads_where_the_daemon_published_the_pane():
    pane = tmux.pane_from_env(
        {"TMUX_TMPDIR": "/tmp/sock", "CLAUDE_PTY_TMUX_SESSION": "cotf-job-1"}
    )

    assert pane == tmux.Pane(tmpdir=Path("/tmp/sock"), session="cotf-job-1")


def test_pane_from_env_reads_an_unhosted_spawn_as_having_no_pane():
    """Half the pair is not a pane: claude-pty would name its own session from
    its pid, which nothing outside the pane can predict."""
    assert tmux.pane_from_env(None) is None
    assert tmux.pane_from_env({}) is None
    assert tmux.pane_from_env({"TMUX_TMPDIR": "/tmp/sock"}) is None
    assert tmux.pane_from_env({"CLAUDE_PTY_TMUX_SESSION": "cotf-job-1"}) is None


class TestHostingCanBeSwitchedOff:
    """An escape hatch, not a feature flag: it is on unless somebody says otherwise."""

    def test_hosting_is_on_when_nothing_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv(tmux.PANE_VAR, raising=False)
        assert tmux.hosting_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " False "])
    def test_the_recognised_ways_of_saying_no(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        monkeypatch.setenv(tmux.PANE_VAR, value)
        assert tmux.hosting_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "", "maybe"])
    def test_anything_else_leaves_hosting_on(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        """A typo must cost a mirrored turn, never a silently unmirrored one."""
        monkeypatch.setenv(tmux.PANE_VAR, value)
        assert tmux.hosting_enabled() is True

    def test_hosting_needs_both_the_operators_yes_and_tmux(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")
        monkeypatch.setenv(tmux.PANE_VAR, "false")
        assert tmux.hosting_available() is False

        monkeypatch.setenv(tmux.PANE_VAR, "true")
        assert tmux.hosting_available() is True

        monkeypatch.setattr(tmux.shutil, "which", lambda _name: None)
        assert tmux.hosting_available() is False


def test_sweep_leaves_a_turn_that_is_still_starting_alone(
    monkeypatch: pytest.MonkeyPatch, short_panes_root: Path
):
    """The directory is created at the top of a turn and the server appears a
    moment later. Both daemons sweep this root, so a worker restarting inside that
    window would otherwise take a live chat turn's socket directory with it."""
    starting = tmux.pane_for("cotf-chat-starting")
    assert starting is not None
    monkeypatch.setattr(tmux, "_sessions_on", lambda _d: [])

    assert tmux.sweep() == 0
    assert starting.tmpdir.exists()


def test_pane_dir_answers_without_creating_anything(short_panes_root: Path):
    """The approval path has to address a pane without bringing one into being."""
    directory = tmux.pane_dir("cotf-pty-1-abcd")

    assert directory.parent == short_panes_root
    assert not directory.exists()


def test_session_env_points_at_a_hosted_pane_and_nothing_otherwise(
    short_panes_root: Path,
):
    """Empty is the meaningful answer: with hosting off, claude-pty puts its
    session on the default server, and forcing TMUX_TMPDIR would look for it on a
    server that does not have it."""
    assert tmux.session_env("cotf-pty-1-abcd") == {}

    pane = tmux.pane_for("cotf-pty-1-abcd")
    assert pane is not None
    assert tmux.session_env("cotf-pty-1-abcd") == {"TMUX_TMPDIR": str(pane.tmpdir)}


def test_kill_session_leaves_the_directory_for_a_retry(
    monkeypatch: pytest.MonkeyPatch, short_panes_root: Path
):
    """A nudge retry opens a session with the same name, and tmux refuses a
    duplicate, so the session has to go while the run directory stays."""
    calls: list[list[str]] = []
    monkeypatch.setattr(tmux, "_run", lambda _p, args, _t: calls.append(args))
    pane = tmux.pane_for("cotf-job-retry")
    assert pane is not None

    tmux.kill_session(pane)

    assert calls == [["kill-session", "-t", "cotf-job-retry"]]
    assert pane.tmpdir.exists()


def test_pane_named_scans_once_for_every_shape_a_viewer_tries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Discovery sits on the TUI's 1Hz refresh and costs a subprocess per run
    directory, so asking twice doubled the cost per frame."""
    job = tmux.Pane(tmpdir=tmp_path / "b", session="cotf-job-run-1")
    scans = []

    def counted():
        scans.append(1)
        return [job]

    monkeypatch.setattr(tmux, "live_panes", counted)

    assert tmux.pane_named("cotf-pty-777-", "cotf-job-run-1") == job
    assert len(scans) == 1
    assert tmux.pane_named() is None


def test_sweep_skips_a_directory_that_vanishes_under_it(
    monkeypatch: pytest.MonkeyPatch, short_panes_root: Path
):
    """Two daemons sweep this root, so the other one may remove a directory
    between the listing and the stat."""
    monkeypatch.setattr(tmux, "_run_dirs", lambda: [short_panes_root / "already-gone"])
    monkeypatch.setattr(tmux, "_sessions_on", lambda _d: [])

    assert tmux.sweep() == 0
