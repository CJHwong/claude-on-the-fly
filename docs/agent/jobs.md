# Background jobs

A single daemon that drains a durable queue: claim a job, run it through an agent, reply into wherever it came from. Where symphony polls a tracker for work, jobs waits for work someone handed it — a Slack user asking for something long-running, or `claude-jobs enqueue`.

Entry point: `src/claude_on_the_fly/jobs/cli.py`. Use case: `jobs/worker.py`. Ports and data types: `jobs/core.py`.

## Layers

`jobs/core.py` is the clean core — stdlib only, no chat/DB/network/LLM/filesystem client. It holds `Job`, `Result`, and the three ports the worker depends on:

| Port | Adapter shipped | Where |
|---|---|---|
| `JobQueue` | `FileInboxQueue` (maildir) | `jobs/file_queue.py` |
| `AgentRunner` | `OrchestratorAgentRunner` | `jobs/agent_runner.py` |
| `Notifier` | `SlackThreadNotifier` | `jobs/slack_notifier.py` |

`jobs/worker.py` imports nothing but those ports plus `asyncio`/`logging`, so an adapter can be swapped with no change to the loop. `jobs/cli.py` is the composition root: it names `OrchestratorAgentRunner` and `SlackThreadNotifier` directly, and takes the queue from `registry.make_queue()` — `jobs/registry.py` is where `FileInboxQueue` is named.

A `Job` carries an opaque `origin` dict. The core, the queue, and the worker pass it through untouched; only the notifier reads it. That is what keeps vendor vocabulary (`channel`, `thread_ts`) out of the core.

## Adding a new queue adapter

`SUPPORTED_QUEUES` in `jobs/registry.py` maps a kind name to a factory over a root directory; `make_queue()` picks one via `JOBS_QUEUE_KIND` (default `file`). Implement the `JobQueue` Protocol, register the kind, set the env var — no worker changes. This mirrors symphony's `SUPPORTED_TRACKERS`. The file also marks the attach point for a Python entry-points group, not built yet.

## The maildir contract

`FileInboxQueue` lays out five subdirs under one root (default `~/.claude-on-the-fly/jobs/`):

```
tmp/     staging for a partial write before it is atomically published
new/     unclaimed, FIFO by time-sortable id
cur/     in-flight (claimed, not completed)
done/    completed: <id>.json plus <id>.result.json
failed/  poison (unparseable / id-mismatch), quarantined, never re-looped
```

Two properties everything else rests on:

- **The claim is a `rename(2)` from `new/` to `cur/`.** Atomic within one filesystem, so two workers racing resolve to exactly one winner with no lock files. `root` and its subdirs must therefore live on one filesystem.
- **Ids are `f"{time.time_ns()}-{uuid4().hex[:8]}"`.** Time-sortable, so `sorted(new/)` is FIFO — and the enqueue time is readable straight out of the name, with no `stat()` and no reliance on an mtime a copy or a `touch` would move.

**Execution is at-least-once.** A crashed worker leaves its job in `cur/`; `recover_stale` moves it back to `new/` on the next start, and shutdown deliberately *cancels* an in-flight job rather than finishing it (so the process tree is reaped inside the supervisor's grace). A job must therefore be safe to re-run.

**`complete()` writes two files into `done/`** — `<id>.result.json` first (so a crash between the two still leaves a durable result), then the job file moves in. Anything counting finished jobs must count `*.result.json`; counting `*.json` double-counts every one of them.

## Reading the queue from outside

`read_queue_depth(root)` and `read_queue_rows(root, limit)` at the bottom of `file_queue.py` are the observer half, used by the TUI's jobs tab.

They are module-level functions over a `root`, **not** `FileInboxQueue` methods, and that is load-bearing: every queue operation (`enqueue` / `claim` / `complete` / `recover_stale`) opens with `_ensure_tree()`, which creates five directories. A reader added as a sibling method would inherit that line by symmetry and start writing into a directory a live worker owns. As free functions there is nothing to inherit — a missing tree reads as an empty queue rather than being built on the spot.

Rules for any future observer:

- **Never create, move, or write.** A missing directory is zeros and an empty list, never an error and never a `mkdir`.
- **Never raise — and never make the caller raise.** A half-written or hand-mangled file degrades one field (`prompt=None`, `enqueued_at=None`), never the whole read. Ids are deduplicated across `cur/` and `new/` for the same reason: the two are listed one after the other, and `recover_stale` moving a file `cur → new` in between would otherwise hand back the same id twice, which is fatal to a caller keying rows by id.
- **Stay bounded.** `read_queue_rows` is hard-capped (default 20), which bounds the file reads — the expensive part. Listing and sorting the two directories is still O(depth); the cap cannot come before the sort without losing oldest-first.
- **The expensive reads are memoized on the directory's own mtime** — the `done/` and `failed/` counts, and the `cur/`+`new/` row list. A directory's mtime changes on any add, remove, or rename within it, so the memo is exact *for this queue's access pattern* — the queue only ever moves whole files. It would **not** be exact for in-place edits of existing files, which never happen here — and it is only ever exact to the resolution the filesystem stamps mtimes at: on a mount that stamps whole seconds rather than nanoseconds, a second change inside the same second reads stale until the next one. An idle tick therefore no longer walks `done/` or opens and parses every unfinished job file — which the 1Hz dashboard refresh would otherwise do whether or not the jobs tab is visible. The `new/` and `cur/` *counts* are not memoized: `read_queue_depth` lists those two directories on every tick.

## The TUI tab

`[4]` on the dashboard (`tui/screens/dashboard.py`). Header liveness comes from the `jobs` heartbeat; the queue counts and rows come from the directory, so a backlog is visible with the worker stopped — the state an operator most needs to see. `k`/`r` stop/restart the worker when that tab is active, resolved through `_active_daemon()`; `jobs` is already in `supervisor._FRONTEND_MODULE` and `checks.SUPERVISABLE_FRONTENDS`, so `K`/`u` cover it too.

A queue deeper than the row cap gets a trailing `… N more` row, so the cap never truncates in silence. `N` is counted only when the page came back full — a shorter page means a job finished between the depth read and the row read, not a hidden row.

The widget ids are `tab-jobs` / `#jobs-panel` / `#jobs-queue-header` / `#jobs-queue`. **`#jobs-content` is a different widget** — the scheduler tab's cron table — and must not be reused.

There is no watch pane for a running job: that needs the worker to publish the running job's `session_uuid`, and the `run_id` in `agent_runner.py` is ephemeral and never persisted. Until it is, the daemon log takes the full width.

## Logging

`jobs/cli.py` has its own `_setup_logging` (adds a midnight-rotating file handler, 7 backups) rather than `preflight._setup_logging`, which is console-only. Without it `logs/jobs.log` is never written and the tab's log pane has nothing to tail. Console and file share one level: `basicConfig` sets the *root* logger from `LOG_LEVEL`, so the file handler's own `DEBUG` floor only bites when `LOG_LEVEL=DEBUG`.

## Config

| Env var | Default | Meaning |
|---|---|---|
| `JOBS_QUEUE_KIND` | `file` | Which `SUPPORTED_QUEUES` adapter to build |
| `JOBS_POLL_INTERVAL_S` | `2.0` | Idle wait between drain attempts |
| `JOBS_TIMEOUT` | `agent.DEFAULT_TIMEOUT` | Per-job wall clock; `0` or negative means no limit |
| `JOBS_SLACK_TOKEN` | falls back to `SLACK_TOKEN` | Notifier token |

Set `JOBS_SLACK_TOKEN` to a **bot** (`xoxb-`) token. Inheriting a user (`xoxp-`) token from `SLACK_TOKEN` makes the worker post results as that user, and the Slack frontend — a separate process with its own dedup set — re-ingests them as new input, one spurious agent turn per job. `cli._notifier_loop_warning` warns about exactly this at startup.
