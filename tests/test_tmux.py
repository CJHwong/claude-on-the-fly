"""Private-server tmux hosting and pane capture."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_on_the_fly import tmux

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


def test_pane_for_creates_a_private_directory(panes_root: Path):
    pane = tmux.pane_for("cotf-pty-7-abcd")

    assert pane.tmpdir.is_dir()
    assert pane.tmpdir.stat().st_mode & 0o777 == 0o700
    assert pane.session == "cotf-pty-7-abcd"


def test_pane_for_keeps_the_directory_short_enough_for_a_unix_socket(panes_root: Path):
    """A session name in the path costs a whole turn, not just the mirror: tmux fails
    the spawn with "File name too long" once the socket exceeds sun_path."""
    pane = tmux.pane_for("job/jira/ACE-1234-a-very-long-key-" + "x" * 120)

    assert pane.tmpdir.parent == panes_root
    assert len(pane.tmpdir.name) == 12


def test_pane_for_warns_when_the_data_dir_is_too_deep_for_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    deep = tmp_path / ("d" * 90)
    monkeypatch.setattr(tmux, "panes_root", lambda: deep)

    with caplog.at_level("WARNING"):
        tmux.pane_for("cotf-chat-1")

    assert "COTF_DATA_DIR" in caplog.text


def test_pane_for_says_nothing_when_the_path_fits(
    short_panes_root: Path, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level("WARNING"):
        tmux.pane_for("cotf-chat-1")

    assert caplog.text == ""


def test_pane_for_is_idempotent_so_a_retry_reuses_the_directory(panes_root: Path):
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
        seen["env"] = kwargs["env"]
        return _done()

    monkeypatch.setattr(tmux.subprocess, "run", record)
    pane = tmux.Pane(tmpdir=tmp_path, session="s")

    tmux._run(pane, ["has-session", "-t", "s"], 1.0)

    assert seen["argv"] == ["tmux", "has-session", "-t", "s"]
    assert seen["env"]["TMUX_TMPDIR"] == str(tmp_path)


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
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
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
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    """The directory is a digest, so the human name can only come from tmux."""
    live = tmux.pane_for("cotf-chat-1")
    tmux.pane_for("cotf-chat-2")
    (panes_root / "stray-file").write_text("not a socket directory")
    monkeypatch.setattr(
        tmux, "_sessions_on", lambda d: ["cotf-chat-1"] if d == live.tmpdir else []
    )

    assert tmux.live_panes() == [live]


def test_sweep_reaps_the_leftovers_of_a_killed_daemon(
    monkeypatch: pytest.MonkeyPatch, panes_root: Path
):
    dead = tmux.pane_for("cotf-chat-dead")
    live = tmux.pane_for("cotf-chat-live")
    (panes_root / "stray-file").write_text("not a socket directory")
    monkeypatch.setattr(
        tmux, "_sessions_on", lambda d: ["cotf-chat-live"] if d == live.tmpdir else []
    )
    monkeypatch.setattr(tmux, "_run", lambda *_a, **_k: None)

    assert tmux.sweep() == 1
    assert not dead.tmpdir.exists()
    assert live.tmpdir.exists()


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

        grid = tmux.capture(pane)

        assert grid is not None
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
