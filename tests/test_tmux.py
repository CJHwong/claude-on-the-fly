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


def test_run_declines_when_tmux_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: None)

    assert tmux._run(["has-session"], 1.0) is None


def test_run_swallows_a_wedged_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")

    def explode(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="tmux", timeout=1.5)

    monkeypatch.setattr(tmux.subprocess, "run", explode)

    assert tmux._run(["capture-pane"], 1.5) is None


def test_run_keeps_the_env_out_of_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The whole reason for a private server: no `-e KEY=VALUE` pairs to read from `ps`."""
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setenv("COTF_CMD_TOKEN", "super-secret")
    seen: dict = {}

    def record(argv, **kwargs):
        seen["argv"] = argv
        return _done()

    monkeypatch.setattr(tmux.subprocess, "run", record)

    tmux._run(["new-session", "-d"], 1.0)

    assert not any("super-secret" in part for part in seen["argv"])


def test_alive_reads_the_probes_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    pane = tmux.Pane(session="s")
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

    assert tmux.capture(tmux.Pane(session="gone")) is None


def test_capture_returns_none_when_tmux_cannot_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)

    assert tmux.capture(tmux.Pane(session="s")) is None


def test_capture_trims_the_rows_the_agent_never_drew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(
        tmux, "_run", lambda *_a, **_k: _done(stdout="hello\n   \n   \n")
    )

    assert tmux.capture(tmux.Pane(session="s")) == "hello"


def test_capture_reflows_to_the_viewer_but_never_below_the_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls: list[list[str]] = []

    def record(args, _timeout):
        calls.append(args)
        return _done(stdout="out")

    monkeypatch.setattr(tmux, "_run", record)

    tmux.capture(tmux.Pane(session="s"), cols=200, rows=4)

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

    def record(args, _timeout):
        calls.append(args)
        return _done(stdout="out")

    monkeypatch.setattr(tmux, "_run", record)

    tmux.capture(tmux.Pane(session="s"))

    assert [args[0] for args in calls] == ["capture-pane"]


def test_live_panes_is_empty_before_any_turn_runs(panes_root: Path):
    assert tmux.live_panes() == []


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


def test_available_follows_the_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: None)
    assert tmux.available() is False

    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")
    assert tmux.available() is True


def test_panes_root_hangs_off_the_data_dir():
    from claude_on_the_fly.agent import DATA_DIR

    assert tmux.panes_root() == Path(DATA_DIR) / tmux.PANES_DIRNAME


def test_paints_accepts_a_style_however_rich_hands_it_over():
    """`Text.from_ansi` spans carry a Style or its name, and an unstyled run carries None."""
    assert tmux._paints(None) is False
    assert tmux._paints("on green") is True
    assert tmux._paints("bold") is False


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


# --- One server, one address -------------------------------------------------


def test_pane_env_points_claude_pty_at_cotfs_server_not_the_operators(
    panes_root: Path,
):
    """claude-pty takes no `-S`, so `TMUX_TMPDIR` is the only way to aim it."""
    pane = tmux.Pane(session="cotf-pty-7-abcd")

    assert pane.env == {
        "TMUX_TMPDIR": str(panes_root),
        "CLAUDE_PTY_TMUX_SESSION": "cotf-pty-7-abcd",
    }


def test_every_pane_shares_one_socket_so_a_writer_and_a_reader_cannot_diverge(
    panes_root: Path,
):
    """The outage this design exists to prevent.

    The writer used to build its session from `TMUX_TMPDIR` while the reader
    addressed `-S`, so a daemon holding an inherited `TMUX` created the pane on
    the operator's server and then read it as dead. One address for both halves
    is what makes that unrepresentable.
    """
    first = tmux.socket_path()
    second = tmux.socket_path()

    assert first == second
    assert first.parent.parent == panes_root
    assert tmux.argv_prefix() == ["tmux", "-S", str(first)]


def test_run_addresses_the_socket_by_name(
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")
    seen: dict = {}

    def record(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return _done()

    monkeypatch.setattr(tmux.subprocess, "run", record)

    tmux._run(["has-session", "-t", "s"], 1.0)

    assert seen["argv"] == [
        "tmux",
        "-S",
        str(tmux.socket_path()),
        "has-session",
        "-t",
        "s",
    ]
    # No hint form anywhere: an inherited TMUX_TMPDIR must not decide the target.
    assert seen["env"] is None or "TMUX_TMPDIR" not in seen["env"]


def test_kill_ends_the_session_and_never_the_shared_server(
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    """`kill-server` would take every other live turn down with this one."""
    seen: list = []
    monkeypatch.setattr(tmux, "_run", lambda args, _t: seen.append(args) or _done())

    tmux.kill(tmux.Pane(session="cotf-job-abc"))

    assert seen == [["kill-session", "-t", "cotf-job-abc"]]
    assert not any("kill-server" in part for args in seen for part in args)


def test_ensure_root_creates_a_private_directory(short_panes_root: Path):
    assert tmux.ensure_root() is True
    assert short_panes_root.is_dir()
    assert short_panes_root.stat().st_mode & 0o777 == 0o700


def test_ensure_root_refuses_an_address_too_long_for_a_unix_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    deep = tmp_path / ("d" * 90) / ("e" * 90)
    monkeypatch.setattr(tmux, "panes_root", lambda: deep)

    with caplog.at_level("WARNING"):
        assert tmux.ensure_root() is False

    assert "unmirrored" in caplog.text
    assert not deep.exists()


def test_pane_for_declines_when_the_address_is_unusable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(tmux, "ensure_root", lambda: False)
    assert tmux.pane_for("cotf-job-abc") is None


def test_live_panes_asks_the_server_rather_than_walking_a_directory(
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    monkeypatch.setattr(
        tmux, "_run", lambda *_a, **_k: _done(stdout="cotf-pty-1-ab\ncotf-job-xyz\n")
    )

    assert [pane.session for pane in tmux.live_panes()] == [
        "cotf-pty-1-ab",
        "cotf-job-xyz",
    ]


def test_live_panes_reads_a_dead_or_wedged_server_as_nothing_to_mirror(
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: _done(returncode=1))
    assert tmux.live_panes() == []

    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)
    assert tmux.live_panes() == []


def test_sweep_reaps_only_the_calling_daemons_own_prefix(
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    """Segmentation by name replaced the per-run `owner.pid` file.

    One server holds every daemon's panes now, so a worker restarting must not
    reap a sibling's live turn. Each prefix has exactly one owner.
    """
    monkeypatch.setattr(
        tmux,
        "live_panes",
        lambda: [
            tmux.Pane(session="cotf-job-one"),
            tmux.Pane(session="cotf-pty-9-live"),
            tmux.Pane(session="cotf-job-two"),
        ],
    )
    killed: list[str] = []
    monkeypatch.setattr(tmux, "kill", lambda pane: killed.append(pane.session))

    assert tmux.sweep(tmux.JOB_SESSION_PREFIX) == 2
    assert killed == ["cotf-job-one", "cotf-job-two"]


def test_sweep_is_a_no_op_before_any_turn_runs(
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    monkeypatch.setattr(tmux, "live_panes", list)
    assert tmux.sweep(tmux.JOB_SESSION_PREFIX) == 0


def test_pane_named_finds_a_run_from_the_prefix_a_viewer_knows(
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    monkeypatch.setattr(
        tmux,
        "live_panes",
        lambda: [tmux.Pane(session="cotf-pty-42-9f3c1d")],
    )

    found = tmux.pane_named("cotf-job-nope", "cotf-pty-42-")

    assert found is not None
    assert found.session == "cotf-pty-42-9f3c1d"
    assert tmux.pane_named("cotf-pty-7-") is None
    assert tmux.pane_named() is None


def test_pane_named_scans_once_for_every_shape_a_viewer_tries(
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    """This sits on the TUI's 1Hz refresh, so one scan per frame, not one per prefix."""
    scans = 0

    def count():
        nonlocal scans
        scans += 1
        return [tmux.Pane(session="cotf-job-run")]

    monkeypatch.setattr(tmux, "live_panes", count)

    tmux.pane_named("cotf-pty-1-", "cotf-job-run")

    assert scans == 1


def test_session_env_points_at_a_hosted_pane_and_nothing_otherwise(
    monkeypatch: pytest.MonkeyPatch, short_panes_root: Path
):
    monkeypatch.setattr(tmux, "hosting_available", lambda: True)
    assert tmux.session_env("cotf-pty-1-abcd") == {
        "TMUX_TMPDIR": str(short_panes_root),
        "CLAUDE_PTY_TMUX_SESSION": "cotf-pty-1-abcd",
    }

    monkeypatch.setattr(tmux, "hosting_available", lambda: False)
    assert tmux.session_env("cotf-pty-1-abcd") == {}


def test_pane_from_env_reads_where_the_daemon_published_the_pane():
    pane = tmux.pane_from_env(
        {"TMUX_TMPDIR": "/tmp/cotf-panes", "CLAUDE_PTY_TMUX_SESSION": "cotf-job-7"}
    )

    assert pane is not None
    assert pane.session == "cotf-job-7"


@needs_tmux
def test_a_real_pane_sources_its_env_and_can_be_captured(short_panes_root: Path):
    """End to end on a real server, which is the only thing that proves the wiring."""
    assert tmux.ensure_root()
    pane = tmux.Pane(session="cotf-test-real")
    prefix = tmux.argv_prefix()
    env_file = short_panes_root / "env"
    env_file.write_text("export COTF_PROBE=landed\n", encoding="utf-8")
    try:
        subprocess.run(
            [
                *prefix,
                "new-session",
                "-d",
                "-s",
                pane.session,
                "bash",
                "-c",
                f". {env_file}; echo probe=$COTF_PROBE; sleep {_PROBE_DEADLINE_S}",
            ],
            check=True,
            timeout=_PROBE_DEADLINE_S,
        )
        deadline = time.monotonic() + _PROBE_DEADLINE_S
        grid = ""
        while time.monotonic() < deadline:
            grid = tmux.capture(pane) or ""
            if "probe=" in grid:
                break
            time.sleep(0.05)

        assert "probe=landed" in grid
        assert tmux.alive(pane) is True
    finally:
        subprocess.run(
            [*prefix, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


@needs_tmux
def test_a_real_pane_is_invisible_to_the_operators_own_tmux(short_panes_root: Path):
    """Why cotf keeps a socket at all instead of using the default server."""
    assert tmux.ensure_root()
    pane = tmux.Pane(session="cotf-test-hidden")
    prefix = tmux.argv_prefix()
    try:
        subprocess.run(
            [*prefix, "new-session", "-d", "-s", pane.session, "sleep", "30"],
            check=True,
            timeout=_PROBE_DEADLINE_S,
        )
        listed = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=_PROBE_DEADLINE_S,
            check=False,
            env={k: v for k, v in os.environ.items() if k != "TMUX_TMPDIR"},
        )

        assert pane.session not in listed.stdout
        assert tmux.alive(pane) is True
    finally:
        subprocess.run(
            [*prefix, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


@needs_tmux
def test_an_inherited_tmux_cannot_steal_a_pane_from_cotfs_server(
    short_panes_root: Path,
):
    """The regression, reproduced.

    A daemon started inside the operator's tmux carries `TMUX`, and a tmux client
    obeys it over `TMUX_TMPDIR`. Addressing with `-S` is what makes the pane land
    where the reader will look for it.
    """
    assert tmux.ensure_root()
    pane = tmux.Pane(session="cotf-test-hijack")
    prefix = tmux.argv_prefix()
    hostile = {**os.environ, "TMUX": "/tmp/some-other-server,999,0", **pane.env}
    try:
        subprocess.run(
            [*prefix, "new-session", "-d", "-s", pane.session, "sleep", "30"],
            check=True,
            timeout=_PROBE_DEADLINE_S,
            env=hostile,
        )

        assert tmux.alive(pane) is True
    finally:
        subprocess.run(
            [*prefix, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def test_turn_files_are_named_per_turn_so_two_turns_cannot_clobber(panes_root: Path):
    """A shared `pane-env` would let one turn source another's `COTF_CMD_TOKEN`.

    Measured on a live daemon: with `sandbox.scoped_sessions()` off, the workspace
    `CODEX_HOME` collapses onto the operator's `~/.codex`, so a fixed name there is
    shared by every concurrent turn.
    """
    one = tmux.turn_file("cotf-job-aaa", "env")
    two = tmux.turn_file("cotf-job-bbb", "env")

    assert one != two
    assert one.parent == panes_root
    assert one != tmux.turn_file("cotf-job-aaa", "out")
