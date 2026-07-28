# Background jobs

A single daemon that drains a durable queue: claim a job, run it through an agent, reply into wherever it came from. Where symphony polls a tracker for work, jobs waits for work someone handed it — a Slack user asking for something long-running, or `claude-jobs enqueue`.

The Slack half is on by default under `$job`. `SLACK_JOB_COMMAND` renames it; setting it **empty** turns it off, and only then does the frontend build no queue — absent versus present-but-blank is the whole opt-out, resolved identically by `slack._resolve_job_command` and `checks.effective_job_command` (a drift test pins them together). With it off the queue is reachable from `claude-jobs enqueue` alone, which is why `claude-jobs doctor` says so rather than passing silently.

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

`JobQueue` also carries `mark_delivered`/`undelivered` (see delivery tracking below) and `list_unfinished(limit)`, the read half of the port, returning `QueueRow`s. A producer answering "what is already queued?" goes through it rather than reaching into `file_queue`, so the answer survives a swap to a broker-backed adapter. Rows carry `origin` for the same reason a `Job` does: the core cannot filter by channel, but the Slack frontend can.

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

**Delivery is tracked separately from completion.** `complete()` archives the result, and a `<id>.delivered.json` marker is written only once a notifier returns. A result with no marker is a reply somebody is still waiting for — the worker was cancelled between finishing and posting, or the post failed — and `redeliver_pending` re-posts it at the next start, before claiming new work. Only the *reply* is retried: the job's agent run already happened, and re-running it would repeat every side effect it had. Bounded by `DELIVERY_RETRY_WINDOW_S` (24h), so a permanently undeliverable result is not retried on every start until the archive prunes it.

That is why `Notifier.notify` **raises** on a failed post rather than swallowing it: returning normally is what marks a result delivered, so an adapter that hides a failure turns a retryable miss into a reply nobody ever receives.

**Execution is at-least-once.** A crashed worker leaves its job in `cur/`; `recover_stale` moves it back to `new/` on the next start, and shutdown deliberately *cancels* an in-flight job rather than finishing it (so the process tree is reaped inside the supervisor's grace). A job must therefore be safe to re-run.

**`done/` is pruned to 7 days on completion** (`DONE_RETENTION_S`), matching the log retention so an archive and the logs covering it expire together. By age rather than count, so a burst of small jobs cannot evict this morning's.

**`complete()` writes two files into `done/`** — `<id>.result.json` first (so a crash between the two still leaves a durable result), then the job file moves in. Anything counting finished jobs must count `*.result.json`; counting `*.json` double-counts every one of them.

## Reading the queue from outside

`read_queue_depth(root)` and `read_queue_rows(root, limit)` at the bottom of `file_queue.py` are the observer half, used by the TUI's jobs tab.

They are module-level functions over a `root`, **not** `FileInboxQueue` methods, and that is load-bearing: every queue operation (`enqueue` / `claim` / `complete` / `recover_stale`) opens with `_ensure_tree()`, which creates five directories. A reader added as a sibling method would inherit that line by symmetry and start writing into a directory a live worker owns. As free functions there is nothing to inherit — a missing tree reads as an empty queue rather than being built on the spot.

Rules for any future observer:

- **Never create, move, or write.** A missing directory is zeros and an empty list, never an error and never a `mkdir`.
- **Never raise — and never make the caller raise.** A half-written or hand-mangled file degrades one field (`prompt=None`, `enqueued_at=None`), never the whole read. Ids are deduplicated across `cur/` and `new/` for the same reason: the two are listed one after the other, and `recover_stale` moving a file `cur → new` in between would otherwise hand back the same id twice, which is fatal to a caller keying rows by id.
- **Stay bounded.** `read_queue_rows` is hard-capped (default 20), which bounds the file reads — the expensive part. Listing and sorting the two directories is still O(depth); the cap cannot come before the sort without losing oldest-first.
- **The expensive reads are memoized on the directory's own mtime** — the `done/` and `failed/` counts, and the `cur/`+`new/` row list. A directory's mtime changes on any add, remove, or rename within it, so the memo is exact *for this queue's access pattern* — the queue only ever moves whole files. It would **not** be exact for in-place edits of existing files, which never happen here — and it is only ever exact to the resolution the filesystem stamps mtimes at: on a mount that stamps whole seconds rather than nanoseconds, a second change inside the same second reads stale until the next one. An idle tick therefore no longer walks `done/` or opens and parses every unfinished job file — which the 1Hz dashboard refresh would otherwise do whether or not the jobs tab is visible. The `new/` and `cur/` *counts* are not memoized: `read_queue_depth` lists those two directories on every tick.

## What preflight catches

Three ways a jobs setup fails quietly, each caught before a daemon starts rather than after it dies:

| Condition | Status | Why |
|---|---|---|
| `SLACK_JOB_COMMAND` set empty | `warn` on `jobs` | The worker runs, but only `claude-jobs enqueue` can reach it. Advisory, not blocking — an enqueue-only install (cron, a git hook) is legitimate. |
| `JOBS_QUEUE_KIND` not registered | `invalid` on `jobs` **and** `slack` | `make_queue()` raises on an unknown kind and the Slack frontend calls it while building the producer, so a jobs-side typo kills *Slack*. Only added to `check_slack` when the trigger is set, since that is the only time a queue is constructed. |
| Trigger live, no worker running | `warn` on `slack` | Advisory: the worker may start after the frontend. The ack itself also tells the truth at runtime — with no worker it says the job stays queued rather than promising a reply. |

`warn` is non-blocking by construction — `checks.is_blocking` is what every caller counting problems should use, so a `doctor` run reports the advice and still exits 0.

## Orphaned agent processes

`agent._exec` spawns the CLI with `start_new_session=True` so one `killpg` reaps the CLI *and* every tool subprocess it forked. The cost: the child is unreachable from the parent's group, and `supervisor.stop()` signals the worker pid alone before SIGKILLing it after a five-second grace. A worker that misses that window would leave a full agent CLI running with no parent and no record of its pid.

So `agent` announces every process group at spawn and after reap (`add_process_listener`), and the worker registers a `ProcessLedger` (`jobs/orphans.py`) that writes the group to `jobs/worker.pids` the moment it exists. What survives a SIGKILL is a file naming exactly what was orphaned; the next start sweeps it before claiming work, since `recover_stale` would otherwise re-run a job whose first copy is still executing.

The sweep refuses to signal a group whose current command no longer matches what was recorded — killing a stranger's recycled pid is far worse than missing an orphan. The recorded pgid is the child's own pid, which `start_new_session` guarantees without racing the child's `setsid`.

## The TUI tab

`[4]` on the dashboard (`tui/screens/dashboard.py`). Header liveness comes from the `jobs` heartbeat; the queue counts and rows come from the directory, so a backlog is visible with the worker stopped — the state an operator most needs to see. `k`/`r` stop/restart the worker when that tab is active, resolved through `_active_daemon()`; `jobs` is already in `supervisor._FRONTEND_MODULE` and `checks.SUPERVISABLE_FRONTENDS`, so `K`/`u` cover it too.

A queue deeper than the row cap gets a trailing `… N more` row, so the cap never truncates in silence. `N` is counted only when the page came back full — a shorter page means a job finished between the depth read and the row read, not a hidden row.

The widget ids are `tab-jobs` / `#jobs-panel` / `#jobs-queue-header` / `#jobs-queue`. **`#jobs-content` is a different widget** — the scheduler tab's cron table — and must not be reused.

There is no watch pane for a running job: that needs the worker to publish the running job's `session_uuid`, and the `run_id` in `agent_runner.py` is ephemeral and never persisted. Until it is, the daemon log takes the full width.

## Logging

`preflight.setup_daemon_logging("jobs")` adds a midnight-rotating file handler (7 backups) beside the console, which `preflight._setup_logging` — console-only — does not: without it `logs/jobs.log` is never written and the tab's log pane has nothing to tail. Symphony uses the same helper. Console and file share one level: `basicConfig` sets the *root* logger from `LOG_LEVEL`, so the file handler's own `DEBUG` floor only bites when `LOG_LEVEL=DEBUG`.

## Config

| Env var | Default | Meaning |
|---|---|---|
| `JOBS_QUEUE_KIND` | `file` | Which `SUPPORTED_QUEUES` adapter to build |
| `JOBS_POLL_INTERVAL_S` | `2.0` | Idle wait between drain attempts |
| `JOBS_TIMEOUT` | `agent.DEFAULT_TIMEOUT` | Per-job wall clock; `0` or negative means no limit |
| `JOBS_SLACK_TOKEN` | falls back to `SLACK_TOKEN` | Notifier token |

Set `JOBS_SLACK_TOKEN` to a **bot** (`xoxb-`) token. Inheriting a user (`xoxp-`) token from `SLACK_TOKEN` makes the worker post results as that user, and the Slack frontend — a separate process with its own dedup set — re-ingests them as new input, one spurious agent turn per job. `cli._notifier_loop_warning` warns about exactly this at startup.
