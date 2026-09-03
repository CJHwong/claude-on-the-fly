"""Dashboard screen — one tab per daemon zone (chat / cron / jobs) + log tail.

Layout (top → bottom):
- Daemon tabs: CHAT, CRON and JOBS each get a tab; the tab title carries a
  health badge ([1]–[3] switch key + state glyph) so switching to one zone
  never blinds the operator to the others' state. The chat tab's badge
  aggregates its daemons (broken > running > stopped). The active tab owns
  the shared log/live row below.
- Log row: daemon log (left) + per-entry / per-job live view (right).

The jobs tab is a read-only observer of the worker's maildir: it renders queue
depth and the unfinished jobs, and never creates, moves, or writes anything
under `jobs/`. Its header's liveness half comes from the heartbeat, so a
stopped worker still shows its backlog. The worker publishes each running
job's session uuid in its heartbeat, so the highlighted job's live agent
conversation is watchable like a chat job's.

The chat tab is a live activity monitor, not a daemon roster: the header shows
every chat frontend's health (the ←/→-selected one reverse-video'd), and the
table lists that frontend's currently-running jobs (from the heartbeat) with
their uptime. Finished / failed requests aren't kept here — they live in the
History overlay (h) and ping a notification when they happen.

Selection model: the active tab decides which daemon the supervisor keys
(k/r) and the log/live row act on; tab state is stable across window blur,
unlike focus. On the chat tab ←/→ pick the frontend (header-owned, always
visible so a stopped one can still be started), and the highlighted row points
the live view at that job's session. Refresh runs at 1Hz, rebuilding tables
from a fresh state.snapshot() while preserving each table's cursor by row key
and its scroll offset (see _restore_cursor).
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, cast

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from claude_on_the_fly import checks, logs, permissions, tmux, upgrade
from claude_on_the_fly.agent import (
    DATA_DIR,
    get_backend,
    resolve_session_log,
)
from claude_on_the_fly.cron import request_run_now
from claude_on_the_fly.tui import (
    env_editor,
    render,
    session_format,
    state,
    supervisor,
)
from claude_on_the_fly.tui.screens.config_picker import ConfigPickerScreen
from claude_on_the_fly.tui.screens.env_diff import EnvDiffScreen
from claude_on_the_fly.tui.screens.help import HelpScreen
from claude_on_the_fly.tui.screens.logs import LogsScreen
from claude_on_the_fly.tui.screens.upgrade import UpgradeScreen

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from claude_on_the_fly.tui.tui_app import ClaudeTuiApp

LOG_DIR = DATA_DIR / "logs"
CRON_CONFIG = DATA_DIR / "cron.yaml"
# Both tables' flexible column: sized to the leftover width on every refresh
# (see _resize_flex_column), so the cron table's name column can auto-size to
# its content and the text still gets the rest of the row. The cron table
# labelled this "prompt / command" until the fifth column arrived: that header
# no longer fits the width the column is left at an 80-column terminal, and the
# `kind` column already says which of the two a row holds.
PROMPT_COLUMN = "prompt"
TAIL_LINES = 200
# When showing a run in the live view, tail this many raw JSONL events; each
# formats to 1–4 visible lines so the rendered pane stays manageable.
LIVE_EVENTS = 80
# Cap on RichLog growth so a 24/7 dashboard doesn't accumulate unbounded memory.
LOG_PANE_MAX_LINES = 10_000

# Reactive, user-driven daemons. Demoted to the compact strip so the
# autonomous cron engine owns the top of the dashboard.
# Order (and the default selection) comes from checks, which every other
# frontend-ordered surface reads too.
CHAT_FRONTENDS = checks.CHAT_FRONTENDS


def _short_job_id(job_id: str) -> str:
    """The tail of a `<time_ns>-<uuid8>` job id.

    The time half is already rendered as the row's `enqueued` age, so showing
    it again would cost 20 columns to say the same thing twice. The row key
    keeps the full id, so nothing downstream is truncated. An id in some other
    shape is shown whole.

    Split on the *first* hyphen, not the last: the time half never contains one,
    while the tail can (`rpartition` turned `...-ACE-1234` into `1234`).
    """
    _, sep, tail = job_id.partition("-")
    return tail if sep else job_id


def _prompt_preview(prompt: str | None, limit: int = 80) -> str:
    """One-line cell text for a job's prompt.

    Whitespace is collapsed because a DataTable cell cannot render a newline,
    and the result is clipped so a multi-KB prompt can't blow up the row. None
    means the file could not be read this tick (claimed mid-read, or hand-
    mangled) — said plainly rather than shown as an empty cell.
    """
    if prompt is None:
        return "(unreadable)"
    flat = " ".join(prompt.split())
    return flat[:limit]


def _running_by_entry(view: state.JobsQueueView | None) -> dict[str, int]:
    """How many of each cron entry's jobs the worker has in flight.

    Read off the queue rather than the cron daemon: the producer's job ends at
    the enqueue, so the entry that fired is idle while the work it produced
    runs. `origin` is what ties the two together (see `_job_source`).

    The row list is capped, so an entry can in principle be missed. `cur/` is
    listed before `new/`, so in-flight rows survive the cap first — a miss needs
    more concurrent jobs than the cap itself.
    """
    counts: dict[str, int] = {}
    for row in view.rows if view else []:
        if not row.in_flight or row.origin.get("kind") != "cron":
            continue
        entry = str(row.origin.get("entry") or "")
        if entry:
            counts[entry] = counts.get(entry, 0) + 1
    return counts


def _job_source(origin: dict) -> str:
    """Who produced this job, as one cell.

    A cron job is named by its entry, which is the string an operator matches
    against cron.yaml and against the `cron-<entry>-…` log; the bare word
    "cron" would only repeat what the tab already says. Every other producer
    is named by its kind. An origin with no kind predates the field, so it says
    so rather than claiming a producer it cannot know.
    """
    kind = str(origin.get("kind") or "")
    if kind == "cron":
        return str(origin.get("entry") or "cron")
    return kind or "-"


class DashboardScreen(Screen):
    DEFAULT_CSS = """
    #daemon-tabs {
        height: auto;
        max-height: 50%;
    }
    #chat-panel, #cron-panel, #jobs-panel {
        height: auto;
        max-height: 100%;
    }
    #cron-header, #chat-strip-header, #jobs-queue-header {
        height: auto;
        padding: 0 0 0 1;
    }
    #chat-strip, #jobs-queue {
        height: auto;
        max-height: 100%;
    }
    /* The cron table is the panel's flexible child: it takes the space the
       header and the prompt-detail block below it don't need, and scrolls
       inside that. `height: auto` let a long entry list grow past the panel
       and push the detail block off the bottom of the screen. */
    #cron-entries {
        height: 1fr;
        min-height: 3;
    }
    #cron-detail-header, #cron-detail {
        display: none;
    }
    #cron-detail-header {
        height: auto;
        padding: 0 0 0 1;
    }
    /* Half the panel at most: on a short terminal a fixed 8 rows would leave
       the flexible table nothing and spill out of the panel again. */
    #cron-detail {
        height: auto;
        max-height: 50%;
        border: solid grey;
    }
    #action-cue {
        height: auto;
        padding: 0 0 0 1;
    }
    """

    # The footer carries the keys reached for constantly: daemon lifecycle
    # (acting on the active tab / focused chat row — see the action cue), the
    # active tab's own actions, help, and quit. The tab-scoped ones are shown
    # but filtered by `check_action`, so the footer only offers what the tab in
    # front of you can actually do. Everything else — the views (logs /
    # history), config, and the utility tail (resume, doctor, copy-tail,
    # refresh, stop-all) — stays bound but `show=False`, surfaced on demand in
    # the `?` help modal. The tab-switch keys [1]–[3] ride on the tab titles
    # themselves, so they're hidden too. Keeping one BINDINGS list as the single
    # source means the modal can't drift.
    # `c` copies the highlighted log tail via OSC 52 (iTerm2/kitty/WezTerm/
    # Alacritty); hold Option (macOS) or Shift while click-dragging for the
    # terminal's own partial-selection copy.
    BINDINGS: ClassVar = [
        Binding("k", "stop", "Stop"),
        Binding("r", "restart", "Restart"),
        Binding("n", "run_now", "Run now"),
        Binding("S", "cycle_cron_sort", "Sort"),
        Binding("t", "copy_takeover", "Takeover"),
        Binding("a", "attach", "Attach"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "app.quit", "Quit"),
        Binding("l", "app.push_screen('logs')", "Logs", show=False),
        Binding("h", "app.push_screen('history')", "History", show=False),
        Binding("g", "open_config", "Config", show=False),
        Binding("u", "resume", "Resume", show=False),
        Binding("d", "app.push_screen('doctor')", "Doctor", show=False),
        Binding("c", "copy_log", "Copy tail", show=False),
        Binding("R", "refresh_now", "Refresh", show=False),
        Binding("K", "stop_all", "Stop all", show=False),
        Binding("U", "upgrade", "Upgrade", show=False),
        Binding("1", "show_tab('tab-chat')", "chat tab", show=False),
        Binding("2", "show_tab('tab-cron')", "cron tab", show=False),
        Binding("3", "show_tab('tab-jobs')", "jobs tab", show=False),
        # priority so the screen sees ←/→ before the focused DataTable swallows
        # them for its (row-mode no-op) horizontal cursor. The action no-ops off
        # the chat tab, so this doesn't strand arrow keys elsewhere.
        Binding("left", "strip_select(-1)", "Prev", show=False, priority=True),
        Binding("right", "strip_select(1)", "Next", show=False, priority=True),
    ]

    # Longer help text per action label (shown in the `?` modal; the footer
    # uses the short Binding.description above).
    _ACTION_HELP: ClassVar[dict[str, str]] = {
        "Stop": "stop (kill) the daemon for the active tab",
        "Restart": "restart the active daemon (also starts it if stopped)",
        "Help": "show this keymap",
        "Quit": "quit the TUI",
        "Logs": "browse every log file",
        "History": "the full job audit trail",
        "Config": "edit the cron / .env config",
        "Resume": "restart the daemons stopped by the last Stop-all",
        "Doctor": "run environment checks",
        "Copy tail": "copy the highlighted log's tail to the clipboard",
        "Refresh": "refresh now",
        "Stop all": "stop every running daemon",
        "Run now": "fire the highlighted cron entry now (cron tab)",
        "Takeover": "copy the highlighted run's resume command (chat / jobs tab)",
        "Attach": "copy the tmux attach command for the highlighted run's live turn (chat / jobs tab)",
        "Sort": "toggle the cron table between next-fire and name order (cron tab)",
        "chat tab": "switch to the chat tab",
        "cron tab": "switch to the cron tab",
        "jobs tab": "switch to the background-jobs tab",
        "Prev": "select the previous chat frontend (←)",
        "Next": "select the next chat frontend (→)",
    }

    def __init__(self) -> None:
        super().__init__()
        # Daemon log pane state.
        self._log_path: Path | None = None
        self._log_mtime: float | None = None
        # Live-tail state, keyed by daemon name. The pane shows only lines
        # written since you first opened that daemon's log (older history lives
        # in the [l] screen). `_log_offsets` is the byte position last read;
        # `_log_buffer` keeps the lines already shown so a tab switch can repaint
        # them instead of blanking the pane.
        self._log_offsets: dict[str, int] = {}
        self._log_buffer: dict[str, deque[str]] = {}
        # Chat frontends currently worth showing (running / broken / ever-run),
        # in display order. Drives the header, the request filter, and the
        # supervisor target. Seeded with all chat frontends so the pre-mount
        # target resolves to the first one (telegram).
        self._chat_frontend_names: list[str] = list(CHAT_FRONTENDS)
        # Which relevant frontend the chat tab is focused on. ←/→ move it; the
        # table shows only this frontend's requests and k/r act on it. Kept
        # stable by name across refreshes (see _refresh_chat_strip).
        self._chat_selected_idx: int = 0
        # Watch pane state, tracked separately so the two panes refresh
        # independently. _live_target encodes what's being watched, e.g.
        # "session:cron:ACE-1" or "cron:cleanup-entry", so we know to
        # force a reload when the user navigates to a different item.
        self._live_path: Path | None = None
        self._live_mtime: float | None = None
        self._live_target: str | None = None
        # ticket identifier → tracker source (jira | github), so the live view
        # resolves the per-tracker workspace dir.
        # "<frontend>:<identifier>" → session_uuid for the live view.
        # Chat rows key on the unique chat_id (workspace_name is not unique
        # across concurrent jobs), so the live view needs the workspace name
        # resolved separately: "<frontend>:<chat_id>" → workspace_name.
        # Jobs rows carry the full workspace path from the worker's heartbeat
        # (their workspaces are not under the chat convention), so that gets
        # its own map: "<frontend>:<job_id>" → workspace.
        self._job_sessions: dict[str, str] = {}
        self._chat_workspaces: dict[str, str] = {}
        self._job_workspaces: dict[str, Path] = {}
        # job name → raw detail text, rebuilt every tick from the snapshot.
        # The detail block reads from here, not from the table cell, which
        # holds the whitespace-collapsed one-liner.
        self._cron_details: dict[str, str] = {}
        # Cron table order: "next" (the snapshot's natural order) or "name".
        # `s` toggles; the header names the current mode.
        self._cron_sort: Literal["next", "name"] = "next"
        # (job name, detail) last shown in the cron detail block, so the 1Hz
        # refresh skips the rewrite when the selection hasn't changed.
        self._cron_detail_shown: tuple[str, str] | None = None
        self._busy_msg: str | None = None
        self._busy_ticks: int = 0
        # Which daemon the lifecycle keys + log row follow for the cron /
        # cron tab — derived from the active tab. Kept as a fallback for the
        # brief pre-mount window before TabbedContent.active is set (chat is the
        # default tab, so a chat daemon is the natural seed).
        self._last_active_daemon: str = CHAT_FRONTENDS[0]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="dashboard-body"):
            yield Static(id="stale-banner", markup=True)
            # One tab per daemon zone — chat first, then the two autonomous
            # engines. The tab title carries the at-a-glance health (badge set
            # in _refresh_tab_badges) so switching to one daemon never blinds
            # the operator to the others' state. The shared log/live row below
            # follows the active tab.
            with TabbedContent(id="daemon-tabs"):
                with (
                    TabPane("chat", id="tab-chat"),
                    Vertical(id="chat-panel"),
                ):
                    yield Static(id="chat-strip-header", markup=True)
                    yield DataTable(
                        id="chat-strip", cursor_type="row", zebra_stripes=True
                    )
                with (
                    TabPane("cron", id="tab-cron"),
                    Vertical(id="cron-panel"),
                ):
                    yield Static(id="cron-header", markup=True)
                    yield DataTable(
                        id="cron-entries", cursor_type="row", zebra_stripes=True
                    )
                    # The highlighted row's full prompt/command, wrapped —
                    # the table cell only shows a clipped one-liner.
                    yield Static(id="cron-detail-header", markup=True)
                    yield RichLog(
                        id="cron-detail",
                        wrap=True,
                        highlight=False,
                        markup=False,
                    )
                # `jobs-queue`, NOT `cron-entries` — the latter is already the
                # cron tab's cron table above.
                with (
                    TabPane("jobs", id="tab-jobs"),
                    Vertical(id="jobs-panel"),
                ):
                    yield Static(id="jobs-queue-header", markup=True)
                    yield DataTable(
                        id="jobs-queue", cursor_type="row", zebra_stripes=True
                    )
            with Horizontal(id="log-row"):
                with Vertical(id="log-daemon-col"):
                    yield Static(id="log-header", markup=True)
                    yield RichLog(
                        id="log-pane",
                        wrap=False,
                        highlight=False,
                        # Scroll is driven explicitly per refresh (render.apply_scroll)
                        # so a live update doesn't yank a reader who scrolled up.
                        auto_scroll=False,
                        max_lines=LOG_PANE_MAX_LINES,
                    )
                with Vertical(id="live-col"):
                    yield Static(id="live-header", markup=True)
                    yield RichLog(
                        id="live-view",
                        wrap=False,
                        highlight=False,
                        markup=True,
                        auto_scroll=False,  # scroll driven via render.apply_scroll
                        max_lines=LOG_PANE_MAX_LINES,
                    )
            yield Static(id="status-line", markup=True)
            yield Static(id="action-cue", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        jobs = self.query_one("#cron-entries", DataTable)
        # Auto-width: the name column sizes to its widest name, so names show
        # in full instead of clipping at a fixed width.
        jobs.add_column("name")
        jobs.add_column("cron", width=16)
        jobs.add_column("kind", width=8)
        jobs.add_column("next fire", width=24)
        # Flexible: resized to the leftover width on every refresh and on
        # resize (see _resize_flex_column). The mount-time width is the fit for
        # an 80-column terminal against a name column no wider than its own
        # header, because a hidden pane has no size to fit against until it is
        # first painted — every later fit corrects it from the real width.
        jobs.add_column(PROMPT_COLUMN, width=14)
        # The background-job queue: what the worker is running now and what is
        # waiting. The id cell shows only the random tail (the time half of the
        # id is already the `enqueued` column); the row key keeps the full id.
        # `job` is sized for the widest placeholder rather than the widest id:
        # "queue unavailable" is 17 cells against an id tail's 8, and a fixed
        # column clips rather than wraps. `source` names the producer — the
        # cron entry that fired this job, or the chat frontend that asked —
        # which is the only thing tying a queue row back to a schedule.
        # `prompt` is the flexible column: the other four take 48 cells and 8
        # of padding, so it gets whatever the terminal leaves over (see
        # _resize_flex_column) rather than a fifth fixed width that no longer
        # fits in the 76 the panel is given at an 80-column terminal. Its
        # mount-time width is that 80-column fit exactly, not a placeholder:
        # the first paint happens before any refresh has a size to fit against,
        # and a wider start would overflow the row until the next 1Hz tick.
        # Every later fit can only widen it.
        queue = self.query_one("#jobs-queue", DataTable)
        queue.add_column("job", width=17)
        queue.add_column("source", width=14)
        queue.add_column("state", width=8)
        queue.add_column(PROMPT_COLUMN, width=18)
        queue.add_column("enqueued", width=9)
        # The chat strip shows the selected frontend's currently-running jobs:
        # what's running and for how long. The header says which frontend.
        chat = self.query_one("#chat-strip", DataTable)
        chat.add_column("running request", width=34)
        chat.add_column("uptime", width=8)
        # The log panes shouldn't grab Tab focus — within a tab, Tab cycles only
        # that tab's table. Same for the cron detail block (mouse wheel still
        # scrolls it) and the Run-now button (its keyboard path is `n`).
        self.query_one("#log-pane", RichLog).can_focus = False
        self.query_one("#live-view", RichLog).can_focus = False
        self.query_one("#cron-detail", RichLog).can_focus = False
        self._refresh()
        self.set_interval(1.0, self._refresh)
        self.set_interval(1.0, self._refresh_log)
        self.set_interval(0.1, self._tick_busy)
        # Land focus on the chat strip (the chat tab is active first), so the
        # highlighted chat daemon is the default supervisor target.
        self.query_one("#chat-strip", DataTable).focus()
        self._update_action_cue()

    # Table id → (flexible column, the auto-sized column to measure). Both
    # tables give one column the width the others leave over.
    _FLEX_COLUMNS: ClassVar[dict[str, tuple[str, str | None]]] = {
        "cron-entries": (PROMPT_COLUMN, "name"),
        "jobs-queue": (PROMPT_COLUMN, None),
    }

    def _refit_flex_columns(self) -> None:
        """Re-fit every flexible column to its table's current width.

        Guarded per table: a table on an unopened tab still has no size, and
        that must not stop the other one being fitted.
        """
        for table_id, (label, auto_label) in self._FLEX_COLUMNS.items():
            with contextlib.suppress(Exception):
                self._resize_flex_column(
                    self.query_one(f"#{table_id}", DataTable),
                    label,
                    auto_label=auto_label,
                )

    def on_resize(self, event: events.Resize) -> None:
        """Re-fit both flexible prompt columns to the new width.

        The first _refresh runs before layout (table size 0), so without this
        the first paint would show the mount-time placeholder width; a
        terminal resize would otherwise leave the column stale until the next
        1Hz tick.
        """
        self._refit_flex_columns()

    # ------------------------------------------------------------------
    # Selection / focus
    # ------------------------------------------------------------------

    # Active tab id → the table to focus when that tab opens.
    _TAB_TABLES: dict[str, str] = {
        "tab-chat": "chat-strip",
        "tab-cron": "cron-entries",
        "tab-jobs": "jobs-queue",
    }

    def _active_daemon(self) -> str | None:
        """Which daemon the supervisor keys + log row act on — decided by the
        active tab (stable across window blur, unlike focus). On the chat tab the
        highlighted row picks the specific daemon (slack / telegram)."""
        try:
            active = self.query_one("#daemon-tabs", TabbedContent).active
        except Exception:
            return self._last_active_daemon  # pre-mount fallback
        if active == "tab-chat":
            return self._chat_supervisor_target()
        if active == "tab-cron":
            self._last_active_daemon = "cron"
        elif active == "tab-jobs":
            self._last_active_daemon = "jobs"
        return self._last_active_daemon

    def _chat_supervisor_target(self) -> str | None:
        """Which chat daemon k/r act on: the ←/→-selected frontend, shown
        reverse-video in the header. Works even with an empty request table, so
        an idle frontend is still stop/restartable."""
        names = self._chat_frontend_names
        if not names:
            return CHAT_FRONTENDS[0]
        idx = min(self._chat_selected_idx, len(names) - 1)
        return names[idx]

    def action_strip_select(self, delta: int) -> None:
        """←/→ move the selected item in the active tab's header strip: a chat
        frontend on the chat tab. No-op on other tabs or when there's nothing
        to switch to."""
        try:
            active = self.query_one("#daemon-tabs", TabbedContent).active
        except Exception:
            return
        if active == "tab-chat":
            count = len(self._chat_frontend_names)
            if count <= 1:
                return
            self._chat_selected_idx = (self._chat_selected_idx + delta) % count
        else:
            return
        self._refresh()
        self._refresh_log(force_reload=True)
        self._update_action_cue()

    def action_show_tab(self, tab_id: str) -> None:
        """Activate a daemon tab by id (bound to [1]/[2]/[3]). The TabActivated
        handler lands focus on the tab's table, so the lifecycle keys and the
        log row follow."""
        with contextlib.suppress(Exception):
            self.query_one("#daemon-tabs", TabbedContent).active = tab_id

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Switching tabs changes the active daemon: land focus on the new tab's
        table and repoint the shared log/live row + action cue."""
        active = event.tabbed_content.active
        table_id = self._TAB_TABLES.get(active, "chat-strip")
        with contextlib.suppress(Exception):
            self.query_one(f"#{table_id}", DataTable).focus()
        self._refresh_log(force_reload=True)
        self._update_action_cue()
        # The footer offers the active tab's actions, so it has to be rebuilt
        # whenever the tab changes (see check_action).
        self.refresh_bindings()

    # Action → the tabs that own it. An action absent from this map is global.
    # Takeover and Attach are the two verbs for a live run, and both tabs that
    # show a run carry one, so neither is pinned to a single tab.
    _TAB_ACTIONS: ClassVar[dict[str, frozenset[str]]] = {
        "run_now": frozenset({"tab-cron"}),
        "cycle_cron_sort": frozenset({"tab-cron"}),
        "copy_takeover": frozenset({"tab-chat", "tab-jobs"}),
        "attach": frozenset({"tab-chat", "tab-jobs"}),
    }

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide a tab-scoped key when its tab isn't in front.

        The footer reads this, so `n` / `S` only appear on the cron tab, and
        `t` / `a` only on the two tabs that show a run. Returning False (not
        None) hides the key rather than graying it out — a key you cannot use is
        noise, not information.

        Scoped by tab only. `attach` is deliberately NOT gated on tmux being
        installed: hiding the binding also swallows the keypress, so `a` would
        do nothing at all and say nothing about why. It stays offered, and
        `action_attach` explains the missing-tmux case. Liveness is not probed
        here either — this runs on every refresh_bindings, and a wedged tmux
        must cost a stale footer at worst.
        """
        owners = self._TAB_ACTIONS.get(action)
        if owners is None:
            return True
        try:
            return self.query_one("#daemon-tabs", TabbedContent).active in owners
        except Exception:
            return False  # pre-mount: no tab is in front yet

    def _selected_job(self) -> str | None:
        """Highlighted job name in the scheduled jobs DataTable, or None."""
        return self._datatable_cursor_key("#cron-entries")

    def _selected_run(self) -> tuple[str, str] | None:
        """The highlighted AI run as `(source, identifier)`, or None.

        Both tabs that show a run answer here, so the live view, Takeover and
        Attach all act on the row the operator is looking at. A chat run is
        keyed by its frontend and chat id; a job run by the literal source
        "jobs" and its job id. The cron tab has no run, so it answers None.
        """
        name = self._active_daemon()
        if name in CHAT_FRONTENDS:
            key = self._datatable_cursor_key("#chat-strip")
            if key and key != "__empty__" and ":" in key:
                source, _, identifier = key.partition(":")
                return source, identifier
            return None
        if name == "jobs":
            key = self._datatable_cursor_key("#jobs-queue")
            if key and key != "__empty__":
                return "jobs", key
        return None

    def _run_workspace(self, source: str, identifier: str) -> Path:
        """The workspace directory for a run.

        A job carries its own path in the worker's heartbeat. A chat run does
        not, so its workspace is rebuilt from the frontend/user name the chat
        strip resolved (`DATA_DIR/workspaces/<frontend>/<user>`).
        """
        key = f"{source}:{identifier}"
        label = self._chat_workspaces.get(key, identifier)
        return self._job_workspaces.get(key) or (DATA_DIR / "workspaces" / label)

    def _pane_prefixes(
        self, source: str, identifier: str, workspace: Path
    ) -> tuple[str, str]:
        """The two session-name shapes a run's pane can carry.

        The two producers name their panes differently and neither can be asked
        at this distance. A chat turn's pane carries the session discriminator
        the orchestrator minted, so only its `<chat id>` prefix is knowable
        here; a job's pane is named for its workspace directory, which the row
        already resolved, so there the prefix is the whole name.
        """
        return (
            f"{permissions.tmux_session_prefix(source)}{identifier}-",
            tmux.job_session_name(workspace.name),
        )

    def _datatable_cursor_key(self, selector: str) -> str | None:
        try:
            table = self.query_one(selector, DataTable)
        except Exception:
            return None
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return str(row_key.value) if row_key.value is not None else None

    @staticmethod
    def _restore_cursor(
        table: DataTable,
        keys: list[str],
        previously: str | None,
        scroll_y: int = 0,
    ) -> None:
        """Put the cursor back on the row it was on, and the viewport back where
        it was.

        `DataTable.clear()` drops both. Restoring the cursor alone scrolls the
        table back down to it, so a scrolled table snaps to the top and back
        inside every 1Hz tick — which the operator sees as a flashing table.
        `scroll_y` is the offset read before the rebuild; the scroll is clamped,
        so a rebuild that dropped rows lands at the new bottom, not past it.
        """
        index = keys.index(previously) if previously in keys else None
        # Already there? Don't move — move_cursor emits RowHighlighted even for
        # a no-op, and the 1Hz refresh would otherwise fire a spurious highlight
        # (and a log rewrite) every tick.
        if index is not None and table.cursor_row != index:
            with contextlib.suppress(Exception):
                table.move_cursor(row=index)
        if scroll_y:
            table.scroll_to(y=scroll_y, animate=False)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Repoint the log pane when the cursor moves. No force_reload: a real
        # selection change is caught downstream by the path/target-switch checks
        # (_refresh_daemon_log, _refresh_live_view), so a same-file highlight
        # hits the mtime guard and no-ops instead of rewriting 200+ lines.
        self._refresh_log()
        self._update_action_cue()
        if event.data_table.id == "cron-entries":
            self._refresh_cron_detail()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # Tabbing between zones changes which daemon the lifecycle keys target.
        self._update_action_cue()

    def _update_action_cue(self) -> None:
        target = self._active_daemon()
        try:
            cue = self.query_one("#action-cue", Static)
        except Exception:
            return  # focus event fired before the cue mounted
        if target:
            cue.update(Text.assemble(("acting on: ", "dim"), (target, "bold")))
        else:
            cue.update("[dim]acting on: (Tab to a panel)[/dim]")

    def action_refresh_now(self) -> None:
        self._refresh()
        self._refresh_log(force_reload=True)

    def action_help(self) -> None:
        """Show the full keymap — every binding, including the ones hidden from
        the slim footer. Built from BINDINGS so it never drifts."""
        rows = [
            (
                b.key_display or b.key,
                b.description,
                self._ACTION_HELP.get(b.description, ""),
            )
            for b in self.BINDINGS
            if isinstance(b, Binding) and b.description
        ]
        self.app.push_screen(HelpScreen(rows))

    # ------------------------------------------------------------------
    # Supervisor actions
    # ------------------------------------------------------------------

    async def _run_supervisor_action(
        self,
        verb: str,  # "stop" | "restart"
        op: Callable[[str], int],
    ) -> None:
        name = self._active_daemon()
        if self._busy_msg:
            return
        if name is None:
            self._notify("no daemon selected (Tab to a panel first)", "warning")
            return
        present = {"stop": "stopping", "restart": "restarting"}[verb]
        past = {"stop": "stopped", "restart": "restarted"}[verb]
        self._set_busy(f"{present} {name}")
        try:
            try:
                pid = await asyncio.to_thread(op, name)
            except supervisor.AlreadyRunning as exc:
                self._notify(f"{name}: already running (pid {exc.pid})", "warning")
                return
            except supervisor.NotRunning:
                self._notify(f"{name}: not running", "warning")
                return
            except supervisor.PreflightFailed as exc:
                bad = [r for r in exc.results if checks.is_blocking(r)]
                detail = bad[0].name if bad else "checks failed"
                self._notify(
                    f"{name}: preflight failed ({detail}) — opening doctor", "error"
                )
                self.app.push_screen("doctor")
                return
            except supervisor.SpawnTimeout as exc:
                # The exception already carries the cause, the log path and a
                # tail. A toast has no room for 25 lines, so it names the cause
                # and the logs screen shows the rest, the same handoff the
                # preflight branch above makes to the doctor screen.
                self._notify(f"{name}: {exc.cause} — opening logs", "error")
                self.app.push_screen(LogsScreen(preselect=exc.log_path))
                return
            except Exception as exc:
                self._notify(f"{name}: {verb} failed: {exc}", "error")
                return
            self._notify(f"{past} {name} (pid {pid})", "information")
        finally:
            self._clear_busy()
            self._refresh()

    async def action_stop(self) -> None:
        await self._run_supervisor_action("stop", supervisor.stop)

    async def action_restart(self) -> None:
        await self._run_supervisor_action("restart", supervisor.restart)

    async def action_stop_all(self) -> None:
        if self._busy_msg:
            return
        self._set_busy("stopping all daemons")
        try:
            stopped = await asyncio.to_thread(supervisor.stop_all)
            if not stopped:
                self._notify("nothing running", "warning")
                return
            names = ", ".join(name for name, _ in stopped)
            self._notify(
                f"stopped {len(stopped)}: {names} (press u to resume)",
                "information",
            )
        finally:
            self._clear_busy()
            self._refresh()

    async def action_resume(self) -> None:
        if self._busy_msg:
            return
        recorded = supervisor.read_last_running()
        if not recorded:
            self._notify("nothing to resume (no last-running record)", "warning")
            return
        self._set_busy(f"resuming {len(recorded)} daemon(s)")
        try:
            results = await asyncio.to_thread(supervisor.resume)
            successes = [name for name, _, exc in results if exc is None]
            failures = [(name, exc) for name, _, exc in results if exc is not None]
            if successes:
                self._notify(
                    f"started {len(successes)}: {', '.join(successes)}",
                    "information",
                )
            for name, exc in failures:
                self._notify(f"{name}: {exc}", "error")
        finally:
            self._clear_busy()
            self._refresh()

    async def action_upgrade(self) -> None:
        """Confirm, then stop every daemon, fetch new code, and come back on it."""
        if self._busy_msg:
            return
        try:
            plan = upgrade.resolve()
        except upgrade.UnknownInstall as exc:
            self._notify(str(exc), "error")
            return
        pending = supervisor.all_pending_work()
        self.app.push_screen(
            UpgradeScreen(plan, pending),
            lambda confirmed: self._on_upgrade_confirmed(plan, confirmed),
        )

    def _on_upgrade_confirmed(self, plan: upgrade.Plan, confirmed: bool | None) -> None:
        if confirmed:
            self.run_worker(self._upgrade(plan), exclusive=True)

    async def _upgrade(self, plan: upgrade.Plan) -> None:
        """Stop everything, run the command, start everything, hand over.

        The daemons come back whether or not the command succeeded — the failure
        case is old code running again, not a machine with nothing on it. Only a
        success relaunches the TUI, because only then is there new code to show.
        """
        self._set_busy("upgrading")
        try:
            stopped = await asyncio.to_thread(supervisor.stop_all)
            self._notify(f"stopped {len(stopped)} daemon(s), upgrading", "information")
            code, output = await asyncio.to_thread(upgrade.run_captured, plan)
            log_path = self._write_upgrade_log(plan, output)
            results = await asyncio.to_thread(supervisor.resume)
            for name, _, exc in results:
                if exc is not None:
                    self._notify(f"{name}: {exc}", "error")
        finally:
            self._clear_busy()
            self._refresh()
        if code != 0:
            self._notify(f"upgrade failed (exit {code}), see {log_path}", "error")
            return
        self._notify(f"upgraded, see {log_path} — relaunching", "information")
        cast("ClaudeTuiApp", self.app).relaunch_on_exit = True
        self.app.exit()

    def _write_upgrade_log(self, plan: upgrade.Plan, output: str) -> Path:
        """Park the command's output where the operator can read it.

        Captured rather than streamed, because git and uv writing to this
        terminal would land on top of the dashboard. Same `<role>-<host>-<date>`
        naming as every other log here, so retention prunes it like the rest.
        """
        path = logs.log_file("upgrade", directory=LOG_DIR)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"=== {plan.command} ({plan.source}) ===\n{output}\n")
        except OSError as exc:
            self._notify(f"could not write {path}: {exc}", "warning")
        return path

    async def action_run_now(self) -> None:
        """Fire the highlighted cron entry now, via the daemon's trigger file.

        The daemon polls the trigger file at 1s granularity, so the entry
        fires within a second — through the same `_fire` path as a scheduled
        fire, gates included. Refuses when the daemon is down: a trigger
        written to nobody would sit there and fire on the next start, which
        is a surprise, not a run-now.
        """
        if self._busy_msg:
            return
        job = self._selected_job()
        if job is None or job == "__empty__":
            self._notify("no cron entry selected", "warning")
            return
        if not supervisor.is_running("cron"):
            self._notify("cron daemon not running — press r to start", "warning")
            return
        try:
            request_run_now(job)
        except Exception as exc:
            self._notify(f"run-now failed: {exc}", "error")
            return
        self._notify(f"run-now requested for {job}", "information")

    def action_cycle_cron_sort(self) -> None:
        """Toggle the cron table between next-fire and name order."""
        self._cron_sort = "name" if self._cron_sort == "next" else "next"
        self._refresh()

    def action_copy_takeover(self) -> None:
        """Copy `cd <workspace> && <resume cmd>` for the highlighted run.

        Same contract as the History screen's `t`: the row's own
        (workspace, session_uuid) pair, so the command resumes that exact
        session rather than whatever the current env points at.

        Offered on the chat and the jobs tab alike. The worker publishes a
        job's session uuid in its heartbeat, so a job row can name its session
        exactly as a chat row can, and an operator taking over a background job
        needs the command more than a chat user does.
        """
        run = self._selected_run()
        if run is None:
            self._notify("no row selected", "warning")
            return
        source, identifier = run
        key = f"{source}:{identifier}"
        workspace_name = self._chat_workspaces.get(key, identifier)
        session_uuid = self._job_sessions.get(key)
        if not session_uuid:
            self._notify("no takeover for this row", "warning")
            return
        workspace = self._run_workspace(source, identifier)
        try:
            cmd = get_backend().takeover_command(workspace, session_uuid)
        except Exception as exc:
            self._notify(f"takeover failed: {exc}", "error")
            return
        if cmd is None:
            self._notify(
                "no session yet — agent hasn't run a turn",
                "warning",
            )
            return
        try:
            # Backend values ultimately include data read from a session store.
            # Re-tokenize and re-quote both pieces before placing them in a
            # shell command copied to the clipboard.
            safe_cmd = shlex.join(shlex.split(cmd))
            takeover = f"cd -- {shlex.quote(str(workspace))} && {safe_cmd}"
            self.app.copy_to_clipboard(takeover)
        except Exception as exc:
            self._notify(f"clipboard write failed: {exc}", "error")
            return
        self._notify(
            f"copied takeover cmd for {workspace_name}",
            "information",
        )

    async def action_attach(self) -> None:
        """Copy the `tmux attach` command for the highlighted run's live turn.

        Works on the chat and the jobs tab: both host their turns in a pane on
        cotf's own server, under the two name shapes `_pane_prefixes` knows.

        Probe liveness off the event loop. `pane_named` shells out to tmux with
        a 5s ceiling and Textual runs action handlers on the loop, so a sync
        call would freeze the whole TUI whenever the server is wedged — the
        stall `check_action` already refuses to risk.

        Copy the command rather than attaching in place: a tmux client seizes
        the terminal the TUI is drawing in, and detaching would drop the
        operator back mid-render.
        """
        run = self._selected_run()
        if run is None:
            self._notify("no row selected", "warning")
            return
        if not tmux.available():
            self._notify("attach unavailable: tmux is not installed", "warning")
            return
        source, identifier = run
        workspace = self._run_workspace(source, identifier)
        chat_prefix, job_name = self._pane_prefixes(source, identifier, workspace)
        pane = await asyncio.to_thread(tmux.pane_named, chat_prefix, job_name)
        if pane is None:
            self._notify(
                "no live pane for this run: the pane exists only while a turn "
                "is running, and only when pane hosting is on "
                f"({tmux.PANE_VAR})",
                "warning",
            )
            return
        try:
            self.app.copy_to_clipboard(tmux.attach_command(pane))
        except Exception as exc:
            self._notify(f"clipboard write failed: {exc}", "error")
            return
        self._notify(
            f"copied attach cmd for {pane.session} — paste it in a terminal "
            "(Ctrl-b d to detach)",
            "information",
        )

    def action_open_config(self) -> None:
        """Pick a config to edit via a modal. Decoupled from focus on purpose —
        config edits are rare and deliberate, so an explicit pick beats a key
        whose meaning shifts with whatever panel is focused."""
        self.app.push_screen(ConfigPickerScreen(), self._open_config_target)

    def _open_config_target(self, choice: str | None) -> None:
        if choice == "cron":
            self._edit_cron_config()
        elif choice == "env":
            self._edit_env()
        elif choice == "sandbox":
            self._edit_sandbox_config()

    def _edit_cron_config(self) -> None:
        from claude_on_the_fly.cron import EXAMPLE_YAML

        with self.app.suspend():
            env_editor.open_in_editor(CRON_CONFIG, seed=EXAMPLE_YAML)
        self._refresh()
        self._notify(f"edited {CRON_CONFIG.name}", "information")

    def _edit_sandbox_config(self) -> None:
        """Edit the operator's sandbox policy: egress hosts, brokered CLIs, approvals.

        Seeded from the bundled template when absent, the same way the daemon seeds
        it at startup, so the first thing opened is a commented file explaining every
        field rather than an empty buffer whose schema has to be guessed.

        No diff afterwards, unlike .env: nothing here is a secret to redact, and the
        loaders re-read the file per call, so an edit takes effect on the next session
        without a restart.
        """
        from claude_on_the_fly import settings

        target = settings.operator_settings()
        with self.app.suspend():
            env_editor.open_in_editor(
                target, seed=settings.BUNDLED_SETTINGS.read_text(encoding="utf-8")
            )
        self._refresh()
        self._notify(f"edited {target.name}", "information")

    def _edit_env(self) -> None:
        env_file = supervisor.DEFAULT_ENV_FILE
        with self.app.suspend():
            diff = env_editor.edit_and_diff(env_file)
        if diff.is_empty():
            self._notify("no env changes", "information")
            return
        self.app.push_screen(EnvDiffScreen(diff))
        self._refresh()

    # ------------------------------------------------------------------
    # Busy spinner
    # ------------------------------------------------------------------

    def _tick_busy(self) -> None:
        if self._busy_msg is None:
            return
        self._busy_ticks += 1
        self._render_busy_line()

    def _set_busy(self, msg: str) -> None:
        self._busy_msg = msg
        self._busy_ticks = 0
        self._render_busy_line()

    def _clear_busy(self) -> None:
        self._busy_msg = None

    def _render_busy_line(self) -> None:
        if self._busy_msg is None:
            return
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self._busy_ticks % 10]
        line = self.query_one("#status-line", Static)
        line.update(f"[yellow]{spinner} {self._busy_msg}…[/yellow]")

    # ------------------------------------------------------------------
    # Log + live views
    # ------------------------------------------------------------------

    def _current_log_path(self) -> Path | None:
        """Whichever log is more specific takes precedence: the live view
        (per-ticket session log) when something's highlighted there,
        otherwise the daemon log for the active daemon."""
        return self._live_path if self._live_path else self._log_path

    def action_copy_log(self) -> None:
        """Copy the tail of the currently-relevant log to the clipboard.

        For "share the whole error context with someone". For partial
        selections, hold Option (macOS) or Shift while click-dragging to
        bypass Textual's mouse capture and use the terminal's native
        selection + ⌘C.
        """
        path = self._current_log_path()
        if path is None:
            self._notify("no log selected to copy", "warning")
            return
        tail_n = 500
        try:
            if not path.is_file():
                raise OSError(f"log does not exist: {path}")
            tail_lines = render.tail_lines(path, tail_n)
        except Exception as exc:
            self._notify(f"copy failed: {exc}", "error")
            return
        tail = "".join(tail_lines).rstrip("\n")
        try:
            # Textual's clipboard uses OSC 52 — works in iTerm2, kitty,
            # WezTerm, Alacritty out of the box.
            self.app.copy_to_clipboard(tail)
        except Exception as exc:
            self._notify(f"clipboard write failed: {exc}", "error")
            return
        shown = len(tail_lines)
        self._notify(
            f"copied last {shown} line(s) of {path.name} to clipboard",
            "information",
        )

    def _refresh_log(self, *, force_reload: bool = False) -> None:
        """Refresh both panes: daemon log on the left, the live view on the right."""
        self._refresh_daemon_log(force_reload=force_reload)
        self._refresh_live_view(force_reload=force_reload)

    def _refresh_daemon_log(self, *, force_reload: bool) -> None:
        """Live-tail the active daemon's log into the dashboard pane.

        Append-only: the pane shows only lines written since you opened that
        daemon's log, so the 1Hz tick writes the few new bytes instead of
        re-rendering a 200-line backlog every second; the full history is one
        keypress away in the [l] Logs screen. The pane can only tail one file,
        so a tab switch clears it — but the per-daemon resume offset means
        switching back replays what was logged while you were away rather than
        starting blank.
        """
        name = self._active_daemon() or "cron"
        header = self.query_one("#log-header", Static)
        pane = self.query_one("#log-pane", RichLog)

        path = logs.find_log(name, directory=LOG_DIR)
        switched = path != self._log_path

        if path is None or not path.is_file():
            # Render the missing state on entry: a switch/force, or a
            # present→missing transition (we still hold a real mtime because the
            # file was there last tick). After rendering it once, mtime is None,
            # so an idle missing pane isn't re-cleared every tick.
            if switched or force_reload or self._log_mtime is not None:
                self._log_path = path
                self._log_mtime = None
                self._log_offsets.pop(name, None)
                self._log_buffer.pop(name, None)
                pane.clear()
                pane.write(Text(f"no log yet at {path}", style="dim"))
                header.update(f"[dim]log: {path.name} (missing)[/dim]")
            return

        try:
            mtime = path.stat().st_mtime
        except OSError:
            return

        if switched or self._log_mtime is None:
            # Repaint on a tab switch, and on a missing→present transition (the
            # missing branch left mtime None), so the "(missing)" header and
            # "no log yet" line are cleared once the log finally appears.
            # The pane can only show one file: repaint this daemon's last-shown
            # tail so a tab switch doesn't blank it. First view shows nothing but
            # the marker (no buffer yet, and the offset below seeks to EOF, so
            # the pre-open backlog stays hidden — full history is in [l]).
            self._log_path = path
            self._log_mtime = None
            render.begin_scroll_aware_rewrite(pane, stick_to_bottom=True)
            pane.write(Text("live tail · full log in [l]", style="dim"))
            for line in self._log_buffer.get(name, ()):
                pane.write(line)
            header.update(f"[bold]log: {path.name}[/bold] [dim]· live tail[/dim]")

        if not switched and not force_reload and mtime == self._log_mtime:
            return  # same daemon, nothing appended since last tick
        self._log_mtime = mtime

        # Append whatever is new since we last read (also catches lines written
        # while this daemon's tab was in the background). None → first view, so
        # read_new_lines seeks to EOF and the backlog stays hidden.
        offset = self._log_offsets.get(name)
        new_lines, self._log_offsets[name] = render.read_new_lines(path, offset)
        new_lines = new_lines[-TAIL_LINES:]  # bound a long background catch-up
        if not new_lines:
            return
        buffer = self._log_buffer.setdefault(name, deque(maxlen=TAIL_LINES))
        if switched:
            # auto_scroll is already on from the repaint above — stay pinned to
            # the bottom rather than reading a not-yet-settled scroll position.
            for line in new_lines:
                buffer.append(line)
                pane.write(line)
            return
        was_bottom, prev_y = render.capture_scroll(pane)
        pane.auto_scroll = was_bottom  # follow the bottom only if reader was there
        for line in new_lines:
            buffer.append(line)
            pane.write(line)
        if not was_bottom:
            render.restore_scroll(pane, prev_y=prev_y)

    def _refresh_live_view(self, *, force_reload: bool) -> None:
        """Show the live column when the active daemon has something to drill
        into: a highlighted cron entry, or a
        chat daemon with a running job. Otherwise the column is hidden so the
        daemon log gets full width.
        """
        col = self.query_one("#live-col", Vertical)
        name = self._active_daemon()

        # "session:<frontend>:<identifier>" for an AI job, "schedule:<name>"
        # for a cron job tail.
        target: str | None = None
        if name == "cron":
            job = self._selected_job()
            if job is not None and job != "__empty__":
                target = f"cron:{job}"
        else:
            # Follow the highlighted run so selecting any row tails its own
            # session, not just the first in-flight one for the daemon. The
            # jobs worker publishes its running job's session uuid in the same
            # heartbeat channel the chat daemons use, so both answer here.
            run = self._selected_run()
            if run is not None:
                target = f"session:{run[0]}:{run[1]}"

        if target is None:
            if col.display:
                col.display = False
                self._live_path = None
                self._live_mtime = None
                self._live_target = None
            return

        if not col.display:
            col.display = True

        if target != self._live_target:
            self._live_target = target
            self._live_path = None
            self._live_mtime = None
            force_reload = True

        header = self.query_one("#live-header", Static)
        pane = self.query_one("#live-view", RichLog)

        mode, _, rest = target.partition(":")
        if mode == "session":
            # rest = "<frontend>:<identifier>"
            source, _, identifier = rest.partition(":")
            self._refresh_live_session(source, identifier, header, pane, force_reload)
        else:
            self._refresh_live_cron(rest, header, pane, force_reload)

    def _refresh_live_session(
        self,
        source: str,
        identifier: str,
        header: Static,
        pane: RichLog,
        force_reload: bool,
    ) -> None:
        """Tail the live backend session JSONL for any AI job row.

        Chat workspaces live under `DATA_DIR/workspaces/<frontend>/<user>`.
        The backend itself is agnostic — it just needs (workspace, uuid).
        """
        key = f"{source}:{identifier}"
        # Rows key on chat_id; the workspace_name (e.g. "telegram/H") is
        # resolved from the side map populated by the chat strip. Jobs rows
        # carry their full workspace path from the worker's heartbeat.
        label = self._chat_workspaces.get(key, identifier)
        workspace = self._run_workspace(source, identifier)
        # The header and the pane render Rich markup, and on the trusted-bot path
        # this name carries a Slack `username` the poster chooses. Every log and
        # tail path in the TUI already goes through the same helper: a body
        # containing `[/path]` reads as a closing tag and raises MarkupError,
        # which takes the whole TUI down. `label` itself stays raw, because the
        # workspace path above is built from it.
        shown = session_format._safe(label)

        # Prefer the agent's own terminal when this run is hosted in a tmux
        # pane. The grid is what the agent is drawing, rather than a rendering
        # rebuilt from transcript events, and it shows the states the transcript
        # cannot: a turn parked on a prompt writes no event at all while it
        # waits. Falls through to the JSONL tail for every run with no pane.
        if self._refresh_live_grid(source, identifier, workspace, shown, header, pane):
            return

        session_uuid = self._job_sessions.get(key)
        if not session_uuid:
            if force_reload or self._live_path is not None:
                self._live_path = None
                self._live_mtime = None
                pane.clear()
                pane.write(f"[dim]no session uuid for {shown} yet[/dim]")
                header.update(f"[bold]live: {shown}[/bold] [dim](pending)[/dim]")
            return

        # Resolve across backends: the daemon may have run this job under a
        # different backend than the TUI's env points at (e.g. codex vs claude).
        path = resolve_session_log(workspace, session_uuid)

        if path is None:
            if force_reload or self._live_path is not None:
                self._live_path = None
                self._live_mtime = None
                pane.clear()
                pane.write(
                    f"[dim]no session log yet for {shown} — "
                    f"agent hasn't run a turn[/dim]"
                )
                header.update(f"[bold]live: {shown}[/bold] [dim](no session yet)[/dim]")
            return

        try:
            mtime = path.stat().st_mtime
        except OSError:
            return

        switched = path != self._live_path
        if not switched and not force_reload and mtime == self._live_mtime:
            return

        self._live_path = path
        self._live_mtime = mtime
        header.update(f"[bold]live: {shown}[/bold] [dim]{path.name}[/dim]")
        was_bottom, prev_y = render.capture_scroll(pane)
        stick = switched or force_reload or was_bottom
        render.begin_scroll_aware_rewrite(pane, stick_to_bottom=stick)
        import json

        raw_lines = render.tail_lines(path, LIVE_EVENTS)
        any_rendered = False
        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            formatted = session_format.format_event(event)
            if formatted is None:
                continue
            for line in formatted.split("\n"):
                pane.write(line)
            any_rendered = True
        if not any_rendered:
            pane.write(f"[dim](no displayable events yet in {path.name})[/dim]")
        if not stick:
            render.restore_scroll(pane, prev_y=prev_y)

    def _refresh_live_grid(
        self,
        source: str,
        identifier: str,
        workspace: Path,
        shown: str,
        header: Static,
        pane: RichLog,
    ) -> bool:
        """Render this run's live tmux pane. True when it did, False to fall back.

        The capture is synchronous on the 1Hz refresh. It costs single-digit
        milliseconds normally, and `CAPTURE_TIMEOUT_S` bounds the pathological
        case at a stutter rather than a hang.
        """
        if not tmux.hosting_available():
            return False
        # One scan, both shapes. Asking twice ran a `tmux list-sessions` per run
        # directory twice per frame, on the refresh thread.
        found = tmux.pane_named(*self._pane_prefixes(source, identifier, workspace))
        if found is None:
            return False
        grid = tmux.capture(found, cols=pane.size.width, rows=pane.size.height)
        # Blank counts as nothing to show, not as something to show. A pane that
        # exists but has not drawn yet trims to the empty string, and rendering
        # that would replace the transcript with an empty widget for the opening
        # stretch of every turn — which reads as the feature being broken.
        if not grid or not grid.strip():
            return False
        # The tail's bookkeeping has to be cleared, not just bypassed: it decides
        # whether a later switch back to a file counts as a switch, and a stale
        # path here would leave the tail refusing to reload the pane it lost.
        self._live_path = None
        self._live_mtime = None
        header.update(f"[bold]live: {shown}[/bold] [dim]tmux pane[/dim]")
        pane.clear()
        pane.write(Text.from_ansi(grid))
        return True

    def _refresh_live_cron(
        self, job: str, header: Static, pane: RichLog, force_reload: bool
    ) -> None:
        """Tail the per-job log for `job` as plain text.

        Markup is bypassed via rich.text.Text so literal log brackets like
        [INFO] survive intact even though the pane has markup=True (which the
        live view relies on for colors).
        """
        path = logs.find_log(f"cron-{job}", directory=LOG_DIR)
        if path is None or not path.is_file():
            if force_reload or self._live_path is not None:
                self._live_path = None
                self._live_mtime = None
                pane.clear()
                pane.write(
                    f"[dim]no log yet at {path} — job hasn't fired since startup[/dim]"
                )
                header.update(f"[bold]job: {job}[/bold] [dim](no log)[/dim]")
            return

        try:
            mtime = path.stat().st_mtime
        except OSError:
            return

        switched = path != self._live_path
        if not switched and not force_reload and mtime == self._live_mtime:
            return

        self._live_path = path
        self._live_mtime = mtime
        header.update(f"[bold]job: {job}[/bold] [dim]{path.name}[/dim]")
        was_bottom, prev_y = render.capture_scroll(pane)
        stick = switched or force_reload or was_bottom
        render.begin_scroll_aware_rewrite(pane, stick_to_bottom=stick)
        lines = render.tail_lines(path, TAIL_LINES)
        if not lines:
            pane.write(f"[dim]({path.name} is empty)[/dim]")
            return
        for line in lines:
            pane.write(Text(line.rstrip("\n")))
        if not stick:
            render.restore_scroll(pane, prev_y=prev_y)

    def _notify(
        self, msg: str, severity: Literal["information", "warning", "error"]
    ) -> None:
        # Disable markup so remote/user-controlled notification text stays literal.
        with contextlib.suppress(Exception):
            self.app.notify(msg, severity=severity, markup=False)
        line = self.query_one("#status-line", Static)
        line.update(Text(msg, style="dim"))

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        snap = state.snapshot()
        by_name = {f.name: f for f in snap.frontends}
        # Rebuilt every tick from the heartbeat; reset before repopulating.
        self._job_sessions = {}
        self._chat_workspaces = {}
        self._job_workspaces = {}

        self._refresh_cron(snap, by_name.get("cron"))
        self._refresh_jobs(snap, by_name.get("jobs"))
        self._refresh_chat_strip(by_name)
        self._refresh_tab_badges(by_name)
        self._refresh_stale_banner(snap)

    @staticmethod
    def _chat_aggregate_state(by_name: dict[str, state.FrontendStatus]) -> str:
        """One health glyph for the chat tab, which fronts three daemons. Worst
        state wins so the badge draws the eye when any chat daemon is broken;
        a single running daemon reads as running, else stopped."""
        states = [by_name[name].state for name in CHAT_FRONTENDS if name in by_name]
        if "broken" in states:
            return "broken"
        if "running" in states:
            return "running"
        return "stopped"

    def _refresh_tab_badges(self, by_name: dict[str, state.FrontendStatus]) -> None:
        """Stamp each tab title with its daemon's health glyph, so the tab bar
        carries the at-a-glance liveness the stacked-panel layout used to."""
        sched = by_name.get("cron")
        jobs = by_name.get("jobs")
        chat_state = self._chat_aggregate_state(by_name)
        sched_state = sched.state if sched else "stopped"
        jobs_state = jobs.state if jobs else "stopped"
        try:
            tabs = self.query_one("#daemon-tabs", TabbedContent)
            tabs.get_tab("tab-chat").label = render.tab_label(1, "chat", chat_state)
            tabs.get_tab("tab-cron").label = render.tab_label(2, "cron", sched_state)
            tabs.get_tab("tab-jobs").label = render.tab_label(3, "jobs", jobs_state)
        except Exception:
            pass

    def _refresh_cron(
        self, snap: state.Snapshot, sched: state.FrontendStatus | None
    ) -> None:
        sched_running = bool(sched and sched.state == "running")
        next_fire_str: str | None = None
        if snap.jobs and sched_running:
            nxt = min(snap.jobs, key=lambda j: j.next_fire)
            next_fire_str = render._fmt_next_fire(nxt.next_fire, snap.timestamp)
        self.query_one("#cron-header", Static).update(
            render.cron_header(
                state=sched.state if sched else "stopped",
                count=len(snap.jobs),
                next_fire_str=next_fire_str,
                sort="next fire" if self._cron_sort == "next" else "name",
                schedule_error=snap.schedule_error,
            )
        )

        # The snapshot arrives sorted by next fire; the name order is a
        # display-only re-sort on top of it.
        jobs = snap.jobs
        if self._cron_sort == "name":
            jobs = sorted(jobs, key=lambda j: j.name)

        table = self.query_one("#cron-entries", DataTable)
        previously = self._selected_job()
        scroll_y = table.scroll_offset.y
        self._cron_details = {job.name: job.detail for job in jobs}
        table.clear()
        keys: list[str] = []
        running = _running_by_entry(snap.jobs_queue)
        for job in jobs:
            # An entry whose work is in flight says so in place of its
            # countdown: that entry is the one an operator is watching, and the
            # next fire is the answer to a question they are not asking yet.
            # The count only appears above one, because a producer entry is the
            # only kind that can have several items running at once.
            in_flight = running.get(job.name, 0)
            if in_flight == 1:
                next_str = "running"
            elif in_flight > 1:
                next_str = f"running ({in_flight})"
            else:
                next_str = (
                    render._fmt_next_fire(job.next_fire, snap.timestamp)
                    if sched_running
                    else "-"
                )
            # Text(), not str: the detail is operator-written and can carry
            # markup-looking brackets ("[pytest]"), which a bare str cell would
            # feed to Rich's markup parser — same reason the jobs queue wraps
            # its prompt cell. Whitespace is collapsed because a DataTable cell
            # cannot render a newline; the full text lives in the detail block
            # below the table.
            table.add_row(
                job.name,
                job.cron,
                job.kind,
                next_str,
                Text(" ".join(job.detail.split())),
                key=job.name,
            )
            keys.append(job.name)
        if not keys:
            # Short enough to fit the auto-sized name column; `g` opens the
            # config.
            table.add_row(
                Text("no jobs (g)", style="dim"), "", "", "", "", key="__empty__"
            )
        else:
            self._restore_cursor(table, keys, previously, scroll_y)
        # The table is `height: 1fr`, so it fills the panel and scrolls once the
        # list outgrows it — that is what keeps the detail block below it on
        # screen. Capping it at the rows it actually has (+1 for the column
        # header) stops a two-entry list painting half a screen of empty zebra
        # stripes, the one thing `height: auto` was doing well.
        table.styles.max_height = table.row_count + 1
        self._resize_flex_column(table, PROMPT_COLUMN, auto_label="name")
        self._refresh_cron_detail()

    def _refresh_cron_detail(self) -> None:
        """Show the highlighted cron row's full prompt/command in the detail
        block below the table; hide the block when nothing is highlighted.

        The block shows the raw text (newlines intact), unlike the table cell
        which collapses whitespace to one line. Rewrites are skipped when the
        selection and text are unchanged, so the 1Hz refresh doesn't repaint
        the block every tick.
        """
        header = self.query_one("#cron-detail-header", Static)
        pane = self.query_one("#cron-detail", RichLog)
        job = self._selected_job()
        if job is None or job == "__empty__":
            self._cron_detail_shown = None
            if header.display:
                header.display = False
                pane.display = False
            return
        detail = self._cron_details.get(job, "")
        if not detail:
            self._cron_detail_shown = None
            if header.display:
                header.display = False
                pane.display = False
            return
        if (job, detail) == self._cron_detail_shown:
            return
        self._cron_detail_shown = (job, detail)
        header.update(f"[bold]prompt / command: {job}[/bold]")
        pane.clear()
        pane.write(Text(detail))
        if not header.display:
            header.display = True
            pane.display = True

    def _resize_flex_column(
        self, table: DataTable, label: str, *, auto_label: str | None = None
    ) -> None:
        """Size the `label` column to the table's leftover width.

        Every other column is fixed, so the flexible one takes whatever is left
        and DataTable clips the overflow. `auto_label` names a column that
        auto-sizes to its content instead (the cron table's name column, so
        names show in full); its width is measured from the rows rather than
        read off the column, because DataTable measures cells on idle — after
        this synchronous pass — so the column's own width is one tick stale.
        No-op before the first layout, when the table's size is still 0.
        """
        if table.size.width <= 0:
            return
        flex = next(
            (c for c in table.columns.values() if c.label.plain == label),
            None,
        )
        if flex is None:
            return
        auto_col = next(
            (c for c in table.columns.values() if c.label.plain == auto_label), None
        )
        others = 0
        if auto_col is not None:
            widest = max(
                (len(str(table.get_row(key)[0])) for key in table.rows),
                default=0,
            )
            others += max(len(auto_col.label.plain), widest) + 2 * table.cell_padding
        others += sum(
            c.get_render_width(table)
            for c in table.columns.values()
            if c is not flex and c is not auto_col
        )
        flex.width = max(10, table.size.width - others - 2 * table.cell_padding)
        flex.auto_width = False

    def _refresh_jobs(
        self, snap: state.Snapshot, jobs: state.FrontendStatus | None
    ) -> None:
        """Render the background-job worker: liveness in the header, the queue
        in the table.

        The two halves have different sources on purpose. Liveness comes from
        the heartbeat, so it is only as good as the worker; the queue is read
        off the maildir, so a backlog is visible with the worker stopped — the
        state the operator most needs to see. Read-only throughout: nothing
        here creates or moves a file under jobs/.

        The worker publishes each running job's session uuid in its heartbeat,
        which is what lets the live view tail the live agent conversation —
        the same channel the chat tab uses.
        """
        running = ((jobs.extra or {}).get("running_jobs") or []) if jobs else []
        for info in running:
            job_id = str(info.get("job_id", ""))
            if not job_id:
                continue
            key = f"jobs:{job_id}"
            session = info.get("session_uuid")
            if session:
                self._job_sessions[key] = str(session)
            workspace = info.get("workspace")
            if workspace:
                self._job_workspaces[key] = Path(str(workspace))
            self._chat_workspaces[key] = _short_job_id(job_id)

        view = snap.jobs_queue
        self.query_one("#jobs-queue-header", Static).update(
            render.jobs_header(
                state=jobs.state if jobs else "stopped",
                pid=jobs.pid if jobs else None,
                heartbeat_age_s=jobs.last_heartbeat_age_s if jobs else None,
                depth=view.depth if view else None,
            )
        )

        table = self.query_one("#jobs-queue", DataTable)
        previously = self._datatable_cursor_key("#jobs-queue")
        scroll_y = table.scroll_offset.y
        table.clear()
        keys: list[str] = []
        for row in view.rows if view else []:
            keys.append(row.id)
            age_s = (
                None
                if row.enqueued_at is None
                else (snap.timestamp - row.enqueued_at).total_seconds()
            )
            # Text(), not str: a bare str cell goes through Rich's markup
            # parser, and the prompt is the one cell on this dashboard carrying
            # third-party text (a Slack user's message). "[pytest]" would be
            # eaten as a tag and "[/]" raises MarkupError — which Textual turns
            # into an app exit, killing the whole dashboard. Same reason
            # _refresh_live_cron wraps log lines.
            table.add_row(
                Text(_short_job_id(row.id)),
                Text(_job_source(row.origin)),
                "running" if row.in_flight else "queued",
                Text(_prompt_preview(row.prompt)),
                render.fmt_age(age_s),
                key=row.id,
            )
        if not keys:
            # Keep one row so the table reads as "here, but empty" and stays
            # focusable for Tab / the lifecycle keys — same as the other tabs.
            msg = "queue unavailable" if view is None else "queue empty"
            table.add_row(Text(msg, style="dim"), "", "", "", "", key="__empty__")
        else:
            if view is not None and view.hidden:
                # The cap is a display limit, so say what it cut rather than
                # leaving the operator to subtract it out of the header's count.
                # Same shape as the placeholder above, and in `keys` like a real
                # row: a key _restore_cursor cannot find leaves the cursor where
                # clear() dropped it, which would yank an operator parked on the
                # last row back to the top on the next tick.
                table.add_row(
                    Text(f"… {view.hidden} more", style="dim"),
                    "",
                    "",
                    "",
                    "",
                    key="__more__",
                )
                keys.append("__more__")
            self._restore_cursor(table, keys, previously, scroll_y)
        self._resize_flex_column(table, PROMPT_COLUMN)

    @staticmethod
    def _chat_frontends(
        by_name: dict[str, state.FrontendStatus],
    ) -> list[state.FrontendStatus]:
        """Every chat frontend, in display order — always shown, regardless of
        state, so any of them can be ←/→-selected and started even when another
        is already running. The table scopes to the selected one, so showing the
        stopped ones in the header costs nothing."""
        return [by_name[name] for name in CHAT_FRONTENDS if name in by_name]

    def _refresh_chat_strip(self, by_name: dict[str, state.FrontendStatus]) -> None:
        frontends = self._chat_frontends(by_name)
        names = [f.name for f in frontends]
        # Keep the selection pinned to the same frontend across refreshes even
        # if the set shifts; clamp into range otherwise.
        prev = self._chat_frontend_names
        if prev and 0 <= self._chat_selected_idx < len(prev):
            pinned = prev[self._chat_selected_idx]
            if pinned in names:
                self._chat_selected_idx = names.index(pinned)
        self._chat_selected_idx = min(self._chat_selected_idx, max(0, len(names) - 1))
        self._chat_frontend_names = names
        selected = names[self._chat_selected_idx] if names else None
        selected_state = (
            by_name[selected].state if selected and selected in by_name else "stopped"
        )

        # The chat tab is a live activity monitor: rows are the selected
        # frontend's currently-running jobs, straight from the heartbeat (the
        # source of truth for "now"). Done / failed requests live in the
        # History overlay (h) and surface as notifications when they happen —
        # the dashboard doesn't hoard them.
        running: list = []
        if selected and selected in by_name:
            running = (by_name[selected].extra or {}).get("running_jobs") or []

        self.query_one("#chat-strip-header", Static).update(
            render.chat_header(frontends, self._chat_selected_idx, len(running))
        )

        table = self.query_one("#chat-strip", DataTable)
        previously = self._datatable_cursor_key("#chat-strip")
        scroll_y = table.scroll_offset.y
        table.clear()
        keys: list[str] = []
        for job in running:
            identifier = str(job.get("identifier", "?"))
            session = job.get("session_uuid")
            # chat_id is the unique discriminator; workspace_name (identifier)
            # can repeat when one sender has concurrent jobs across threads.
            key = f"{selected}:{job.get('chat_id', identifier)}"
            self._chat_workspaces[key] = identifier
            if session:
                self._job_sessions[key] = str(session)
            table.add_row(
                Text(identifier), render.fmt_age(job.get("uptime_s")), key=key
            )
            keys.append(key)

        if not keys:
            # A running daemon with no jobs is idle; a stopped one tells the
            # user how to bring it up (k/r act on the selected frontend).
            msg = (
                "idle — nothing running"
                if selected_state == "running"
                else f"{selected_state} — press r to start"
            )
            table.add_row(Text(msg, style="dim"), "", key="__empty__")
        else:
            self._restore_cursor(table, keys, previously, scroll_y)

    def _refresh_stale_banner(self, snap: state.Snapshot) -> None:
        stale = [f.name for f in snap.frontends if f.stale]
        banner = self.query_one("#stale-banner", Static)
        if stale:
            names = ", ".join(stale)
            banner.update(
                f"[bold yellow]⚠ {len(stale)} daemon(s) out of date "
                f"({names}) — press Shift+K then u to upgrade.[/bold yellow]"
            )
        else:
            banner.update("")
