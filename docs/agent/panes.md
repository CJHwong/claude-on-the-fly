# Panes

A turn can run inside a tmux pane so something other than the turn itself can see what
the agent is doing. `src/claude_on_the_fly/tmux.py` owns the pane; the TUI's live view
renders it read-only.

Read this before changing how a turn is hosted, how the live view picks its source, or
how a hosted turn is reaped.

## Why a pane rather than the transcript

The live view already tails the session JSONL and renders it through
`tui/session_format.py`. That covers every backend, and it is still the fallback. What it
cannot show is a state the transcript never records. A turn parked on claude's
workspace-trust dialog writes no event while it waits, so the tail is empty and the turn
looks hung for no reason. The pane shows the dialog.

So the live view prefers the pane when the selected run has one, and falls back to the
tail when it does not.

Hosting is on wherever tmux is installed, and `agent.pane: false` switches it off — an
escape hatch rather than a feature flag, so an unrecognised value leaves it on. The
value is read per turn, so an edit takes effect on the next one: nothing here binds a
socket or constructs a service, which is why it is deliberately absent from
`settings.RESTART_REQUIRED`. `tmux.hosting_available()` is the single question both
producers ask.

## One tmux server for all of cotf

Every hosted turn is a session on **one** server, at a fixed socket under
`DATA_DIR/panes/tmux-<uid>/default`. `argv_prefix()` addresses it with `-S`, and every
call in `tmux.py` and in `backends/codex.py` goes through it.

There used to be a server per run. That is what produced the outage this section exists
to prevent: the reader addressed `-S <per-run socket>` while the writer built its session
from `TMUX_TMPDIR`, and the two disagree the moment `TMUX` is set.

### `-S` beats `TMUX`; `TMUX_TMPDIR` does not

A tmux client obeys `TMUX` over every other hint. A daemon started from inside the
operator's tmux carries `TMUX` into every child, so a bare `tmux new-session` there lands
on the operator's server no matter what `TMUX_TMPDIR` says. Measured: with `TMUX` set and
`TMUX_TMPDIR` naming a fresh directory, `new-session` returned 0, created nothing under
that directory, and the session appeared on the default server.

What that cost: every hosted job reported `RuntimeError: Exit code -1` about a second
after launch, because `tmux.alive` asked the private socket and found no server, while
the agent kept running on the operator's server — unsupervised, sandbox bypassed, with
nobody collecting its work. Six accumulated in twenty minutes before anyone looked.

Three things follow, and none of them are optional:

- **One address for the writer and the reader.** `argv_prefix()` is exported for exactly
  this: `backends/codex.py` builds its own async `new-session`, `pipe-pane` and
  `wait-for` argv rather than going through `_run`, and it must not build its own
  address.
- **The spawn env drops `TMUX` and `TMUX_PANE`.** `claude-pty` calls bare `tmux` and
  takes no socket argument, so `TMUX_TMPDIR` is the only way to aim it — and an inherited
  `TMUX` would beat that. It is the one participant that cannot use `-S`.
- **`new-session` returning 0 is not proof.** tmux reports success for a session it
  created somewhere else, so the backend asks `has-session` on the address it is about to
  poll. A mismatch degrades to an unhosted `codex exec` turn, which works, instead of
  being read as a dead pane forty lines later.

### What sharing a server costs

- **The curated env no longer reaches the pane by inheritance.** A pane on a server that
  was already running does not see the client's environment (measured, tmux 3.7c). The
  fix is *not* `tmux new-session -e KEY=VALUE`: that would put `COTF_CMD_TOKEN` — the
  bearer token for the broker that runs credentialed CLIs *outside* the jail — into a
  command line any local `ps` can read. The pane sources a 0600 file written beside the
  workspace's `CODEX_HOME` instead, which is what `claude-pty` has always done.
- **Teardown is `kill-session`, never `kill-server`.** One server holds every turn, so
  ending it would take the others down. tmux ends the pane's process group with the
  session, so the reap still cannot miss a pane child — a pane is a child of the tmux
  server rather than of the daemon.
- **The sweep segments by name.** Each prefix has exactly one owning daemon —
  `cotf-job-` the jobs worker, `cotf-pty-<frontend>-` one orchestrator — so a restarting
  daemon reaps only its own leftovers. That replaced the `owner.pid` file the per-run
  directories carried; with no directory there is nowhere to write it, and a name we
  already control answers the same question. rhapsody segments the same way and for the
  same reason (`rhapsody-<owner>-`).

  The frontend segment is load-bearing, not decoration. `claude-slack` and
  `claude-telegram` are separate entry points running separate orchestrators against one
  server, so a bare `cotf-pty-` sweep on a telegram restart would kill slack's live chat
  panes mid-turn. `permissions.tmux_session_prefix` builds the sweep argument so it
  cannot drift from `tmux_session_name`.

### What it keeps

**The operator's own tmux is untouched.** No cotf session appears in their `tmux ls`, and
their `kill-server` cannot end a turn. This is the whole reason cotf keeps a socket at all
rather than using the default server the way rhapsody does — rhapsody wants
`rhapsody attach <KEY>` to work from any terminal, and cotf has the TUI mirror instead.

### The socket path is short on purpose

The whole socket path has to fit a unix address: `sun_path` is 104 bytes on macOS. A
96-character root yields a 113-byte socket and tmux fails the spawn outright with
"File name too long", which costs the turn and not just the mirror. `ensure_root` warns
with the projected length and returns False when a redirected `COTF_DATA_DIR` is too
deep; every caller reads that as an unhosted turn rather than a failed one.

`-S` binds the exact path it is given and creates nothing on the way, so `ensure_root`
makes the `tmux-<uid>` segment itself. tmux would have made it from `TMUX_TMPDIR`, which
is the hint form this module refuses to use.

## Who hosts what

| Run | Hosted | What the pane shows |
|---|---|---|
| claude, `mode: pty` | Yes | claude's interactive TUI |
| claude, `mode: native` | No | nothing — the tail covers it |
| codex, `mode: pty` | Yes | codex's interactive TUI |
| codex, `mode: native` | No | nothing — the tail covers it |

`claude-pty` needs no change to take part: it calls bare `tmux`, so exporting
`TMUX_TMPDIR` into its spawn env puts its session on the private server. codex is hosted
by the backend itself (`backends/codex.py:_run_codex_in_pane`), because codex has no
wrapper that would do it.

Native `claude -p` is deliberately unhosted. Its pane would show stream JSON.

### `codex.mode: pty` runs the interactive binary

`codex exec` is the *non-interactive* mode: it prints plain lines and never draws a UI,
so mirroring it gives a pane that reads as dead next to claude-pty's. `codex.mode: pty`
therefore runs `codex <prompt>` (or `codex resume <thread id> <prompt>`) — the same
binary a person would use — and the mirror shows what they would see, status bar
included.

Three consequences:

- **It never exits.** The TUI returns to its prompt and waits for the next turn, so the
  end of a turn comes from the rollout's `task_complete` (the follower already watches
  for it) and the session is killed once it lands. There is no exit code to read: a
  completed turn is a success, because the reply is already in the rollout.
- **The workspace has to be trusted first.** The TUI asks "do you trust the contents of
  this directory?" and nobody is there to answer, so `_ensure_workspace_trusted` writes
  the stanza codex would have written. The key is the **resolved** path — on macOS a
  workspace under `/tmp` is recorded as `/private/tmp/...`, and a stanza under the
  unresolved name matches nothing (measured: the dialog still appeared).
- **Autonomy is `--dangerously-bypass-approvals-and-sandbox`**, not `--yolo`. That is the
  spelling both the interactive entry point and `resume` document; `--yolo` is
  undocumented on `resume`.

`native` mode and any pty-mode turn that has no pane still run `codex exec`, and every
arm reads the turn from the rollout through one parser, so they cannot report it
differently.

The mode is separate from `agent.pane` on purpose. `pane` is global, so using it to
retreat from a break in codex's interactive path would take claude-pty's mirror away
at the same time; `codex.mode: native` gives up only what broke.

## The bottom viewport and its modes

The dashboard's bottom row is one viewport, not two panes side by side. It shows the
daemon **log**, the highlighted row's **live** output, or a **preview** of what that row
runs. `v` cycles; `p` jumps to the preview and back to where you were. Only the active
mode is displayed, and `_refresh_bottom` refreshes only that one: a hidden log is not
read at all.

Half a terminal each is what this replaced. Both panes tailed a file every second
whether or not anyone was reading them, and neither was wide enough to read.

Three things follow:

- **The mode is per tab, and remembered.** A tab that shows runs opens on `live`; the
  cron tab opens on `preview`, which is what an operator reads a schedule for. The
  operator's own choice wins from then on, for that tab, for the session.
- **A mode owns the viewport, so it never hides itself.** The live view used to hide its
  column when nothing was highlighted. It now says nothing is highlighted, and the mode
  strip dims `live` — a dimmed mode is still reachable, it just has nothing behind it.
- **The header is painted from one place.** Each mode sets a label
  (`_set_bottom_label`); `_paint_bottom_header` draws the strip around it once per
  refresh. A mode that returns early — nothing appended since the last tick — still gets
  its label redrawn when the availability of another mode changes.

`_preview_subject` and `_preview_text` are deliberately split. The mode strip asks the
first one every tick to decide whether to dim `preview`, so it resolves the highlighted
row to a name and reads nothing; only the refresh asks for the text. Keep it that way
when you add a preview source — an availability check that opens a file is a file opened
once a second to grey out one word.

Writes to a mode that is not displayed are safe but pointless: `RichLog` defers every
write until it knows its size and flushes on the first layout. That is why `_set_mode`
forces a reload rather than trusting what the widget was left holding.

## Attaching to a live pane

The live view is read-only. To type at the agent, attach to its session: the dashboard's
`a` key copies the command for the highlighted run, on the chat tab and the jobs tab
alike.

The command carries `-S`, from `tmux.attach_command()`. A bare `tmux attach -t <name>`
reads the *default* server and reports nothing, because cotf's sessions are not on it.
That is the same split that once put a running agent on the operator's server and then
reported it dead, so the command an operator pastes is built from `argv_prefix()` like
every other caller.

Two things the key deliberately does not do:

- **It does not attach in place.** A tmux client seizes the terminal the TUI is drawing
  in, and detaching drops the operator back mid-render. It copies instead.
- **It does not hide itself when tmux is missing.** Hiding a binding also swallows the
  keypress, so `a` would do nothing and explain nothing. It stays offered and answers.

Liveness is probed on the press, off the event loop, and never in `check_action`.
`check_action` runs on every `refresh_bindings`, so a wedged server there would freeze
the footer.

`t` is the other half. It copies the backend's resume command (`claude --resume`,
`codex resume`) for a run whose session already exists, so an operator can take the
conversation over in their own terminal rather than watch it. Both tabs offer both keys.

## Lifecycle

1. The producer names the pane and ensures the socket directory: `orchestrator.py` for a
   chat turn (via `permissions.tmux_session_name`, so approvals address the same pane),
   `jobs/agent_runner.py` for a job (via `tmux.job_session_name`).
2. The name and the root go into `sandbox.session_env`, which `sandbox.agent_env` layers
   over the allowlist unconditionally.
3. The backend hosts itself, or claude-pty does. The backend confirms the session landed
   on cotf's server before it starts polling.
4. The producer calls `tmux.kill` in its `finally`. That ends the session and its process
   group, so a backend that left a process running past the deadline is reaped here too.
5. `tmux.sweep(<own prefix>)` runs at daemon startup for whatever a killed daemon left
   behind. The prefix is what stops it reaping a sibling daemon's live turn.

## Gotchas

- **A read command does not start a server.** Measured: `capture-pane` and
  `resize-window` against a missing socket both fail with "error connecting" rather than
  starting one. So the TUI polling for a pane that does not exist yet cannot create a
  server with the wrong environment.
- **`capture` reflows the window.** `cols`/`rows` resize a window every other viewer
  shares, which is why `rows` has a floor (`MIRROR_MIN_ROWS`). A viewport shorter than
  the floor becomes a window onto the grid instead of shrinking the agent's own view.
- **The capture is synchronous on the TUI's 1Hz refresh.** It costs single-digit
  milliseconds normally; `CAPTURE_TIMEOUT_S` bounds the pathological case at a stutter.
