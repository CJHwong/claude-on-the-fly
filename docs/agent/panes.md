# Panes

A turn can run inside a tmux pane so something other than the turn itself can see what
the agent is doing. `src/claude_on_the_fly/tmux.py` owns the pane; the TUI's watch pane
renders it read-only.

Read this before changing how a turn is hosted, how the watch pane picks its source, or
how a hosted turn is reaped.

## Why a pane rather than the transcript

The watch pane already tails the session JSONL and renders it through
`tui/session_format.py`. That covers every backend, and it is still the fallback. What it
cannot show is a state the transcript never records. A turn parked on claude's
workspace-trust dialog writes no event while it waits, so the tail is empty and the turn
looks hung for no reason. The pane shows the dialog.

So the watch pane prefers the pane when the selected run has one, and falls back to the
tail when it does not.

Hosting is on wherever tmux is installed, and `agent.pane: false` switches it off — an
escape hatch rather than a feature flag, so an unrecognised value leaves it on. The
value is read per turn, so an edit takes effect on the next one: nothing here binds a
socket or constructs a service, which is why it is deliberately absent from
`settings.RESTART_REQUIRED`. `tmux.hosting_available()` is the single question both
producers ask.

## One private tmux server per run

Every run gets its own server. `TMUX_TMPDIR` places it; `-S` addresses it.

The two are not interchangeable. `TMUX_TMPDIR` is a hint tmux uses to *build* a socket
path, and when the directory it names does not exist tmux silently falls back to the
default socket — `TMUX_TMPDIR=/nonexistent tmux kill-server` ends the operator's server
and exits 0 (measured, tmux 3.7b). A `sweep` in a sibling daemon removes run directories,
so any control command can find its own directory gone, and the hint form turned a reap
of one finished turn into `kill-server` on whatever the operator was running. `_run`
therefore passes `-S <socket_path(tmpdir)>`, which names the socket outright: a missing
path is an error rather than a different server. Server *creation* and the agent's spawn
env still carry `TMUX_TMPDIR`, because `claude-pty` calls bare `tmux` and derives its own
socket from it.

- **The curated env reaches the pane with nothing in argv.** A pane on a server that was
  already running does not see the client's environment (measured, tmux 3.7c), so the
  alternative is `tmux new-session -e KEY=VALUE` per pair. That would put `COTF_CMD_TOKEN`
  — the bearer token for the broker that runs credentialed CLIs *outside* the jail — into
  a command line any local `ps` can read. A server this daemon starts inherits the
  daemon's spawn env, and its panes inherit that.
- **Teardown is total.** `kill-server` ends the session, its panes, and everything the
  agent started in them. A process-group kill never reached a pane child, because a pane
  is a child of the tmux server rather than of the daemon.
- **The operator's own tmux is untouched.** No cotf session appears in their `tmux ls`,
  and their `kill-server` cannot end a turn. This holds only because control commands are
  addressed with `-S`; under the `TMUX_TMPDIR` hint a reaped directory pointed cotf's
  `kill-server` straight at the default socket.

### The socket path is short on purpose

A run's directory is named for a 12-character digest of its session, not for the session.
The whole socket path has to fit a unix address: `sun_path` is 104 bytes on macOS. A
96-character directory yields a 113-byte socket and tmux fails the spawn outright with
"File name too long", which costs the turn and not just the mirror. `pane_for` warns with
the projected length when a redirected `COTF_DATA_DIR` is too deep.

Nothing reads the session name back out of the directory name as a result. `_sessions_on`
asks the server, which is the better source anyway.

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

## Lifecycle

1. The producer names the pane and creates its socket directory: `orchestrator.py` for a
   chat turn (via `permissions.tmux_session_name`, so approvals address the same pane),
   `jobs/agent_runner.py` for a job (via `tmux.job_session_name`).
2. The name and directory go into `sandbox.session_env`, which `sandbox.agent_env` layers
   over the allowlist unconditionally.
3. The backend hosts itself, or claude-pty does.
4. The producer calls `tmux.kill` in its `finally`. That ends the server, so a backend
   that left a process running past the deadline is reaped here too.
5. `tmux.sweep` runs at daemon startup for whatever a killed daemon left behind.

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
