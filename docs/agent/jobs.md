# Background jobs

A single daemon that drains a durable queue: claim a job, run it through an agent, reply into wherever it came from. It takes work from two producers: `claude-cron`, which polls and enqueues (see [cron.md](cron.md)), and Slack, where a user asks for something long-running. `claude-jobs enqueue` is the third, for smoke tests.

The Slack half is on by default under `$job`. `slack.job_command` renames it; setting it **empty** turns it off, and only then does the frontend build no queue — absent versus present-but-blank is the whole opt-out, resolved identically by `slack._resolve_job_command` and `checks.effective_job_command` (a drift test pins them together). With it off the queue is reachable from `claude-jobs enqueue` alone, which is why `claude-jobs doctor` says so rather than passing silently.

Entry point: `src/claude_on_the_fly/jobs/cli.py`. Use case: `jobs/worker.py`. Ports and data types: `jobs/core.py`.

The worker's `jobs.concurrency`, `jobs.poll_interval_s`, and `jobs.timeout`
settings are construction-time settings and require a worker restart. They are
listed in `settings.RESTART_REQUIRED`; changing the YAML does not partially
reconfigure an already-running worker.

## Layers

`jobs/core.py` is the clean core — stdlib only, no chat/DB/network/LLM/filesystem client. It holds `Job`, `Result`, and the five ports the worker depends on:

| Port | Adapter shipped | Where |
|---|---|---|
| `JobQueue` | `FileInboxQueue` (maildir) | `jobs/file_queue.py` |
| `AgentRunner` | `OrchestratorAgentRunner` | `jobs/agent_runner.py` |
| `Notifier` | `SlackThreadNotifier` | `jobs/slack_notifier.py` |
| `OutcomeRecorder` | `KeyStateOutcomeRecorder` | `jobs/key_state.py` |
| `AlertSink` | `build_alert_sink` factory | `jobs/alerts.py` |

`jobs/worker.py` imports nothing but those ports plus `asyncio`/`logging`, so an adapter can be swapped with no change to the loop. `jobs/cli.py` is the composition root: it names `OrchestratorAgentRunner` and `SlackThreadNotifier` directly, and takes the queue from `registry.make_queue()` — `jobs/registry.py` is where `FileInboxQueue` is named.

One worker drains both producers, so delivery fans back out by where a job came from: `notifiers.RoutingNotifier` dispatches on `origin["kind"]` to the Slack thread notifier or to `LogNotifier`, which appends a cron reply to that entry's own log. It raises on an unknown kind rather than returning, so a typo'd kind shows up as a stuck reply in `undelivered()` instead of one marked delivered to nowhere.

A `Job` carries an opaque `origin` dict. The core, the queue, and the worker pass it through untouched; only the notifier reads it. That is what keeps vendor vocabulary (`channel`, `thread_ts`) out of the core.

`JobQueue` also carries `mark_delivered`/`undelivered` (see delivery tracking below) and `list_unfinished(limit)`, the read half of the port, returning `QueueRow`s. A producer answering "what is already queued?" goes through it rather than reaching into `file_queue`, so the answer survives a swap to a broker-backed adapter. Rows carry `origin` for the same reason a `Job` does: the core cannot filter by channel, but the Slack frontend can.

## Adding a new queue adapter

`SUPPORTED_QUEUES` in `jobs/registry.py` maps a kind name to a factory over a root directory; `make_queue()` picks one via `jobs.queue_kind` (default `file`). Implement the `JobQueue` Protocol, register the kind, set the key — no worker changes. The file also marks the attach point for a Python entry-points group, not built yet.

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
- **A keyed job's filename carries its key**: `<id>__<entry>__<item>.json` in `new/` and `cur/` (`keys.queue_filename`). Unkeyed jobs stay `<id>.json`. The key is in the *name* so `count_unfinished` can answer a producer's dedup and concurrency questions with two globs and zero file reads — it is asked on every poll, and reading every queued job to answer it would put the cost of the whole queue on every fire. The id stays the first component, so FIFO and `_enqueued_at` are unaffected. **Anything wanting the id from a filename goes through `keys.job_id_from_filename`**; `Path.stem` is the id *plus* its key on a keyed file. `done/` deliberately does not follow the scheme — `complete()` archives to a bare `<id>.json` so `undelivered()` pairs a job with its result by id alone. That is also why the key charset excludes `.`: a key containing one could mint an id whose job file is indistinguishable from another id's `.result.json`.

**A job payload carries `profile`.** It names an `agent.profiles` block, or is null for
the daemon's global agent config, and `agent_runner` resolves it once per run for both
the session-uuid seed and the `agent.run` call. `_load` reads it with `.get`, like the
other dispatch fields, which means the tolerance runs both ways: a worker older than
the field ignores a `profile` it does not understand and runs the daemon default. A
mixed-version rollout therefore downgrades the model rather than failing, which is
worth knowing before you deploy one half of a pair.

**Delivery is tracked separately from completion.** `complete()` archives the result, and a `<id>.delivered.json` marker is written only once a notifier returns. A result with no marker is a reply somebody is still waiting for — the worker was cancelled between finishing and posting, or the post failed — and `redeliver_pending` re-posts it at the next start, before claiming new work. Only the *reply* is retried: the job's agent run already happened, and re-running it would repeat every side effect it had. Bounded by `DELIVERY_RETRY_WINDOW_S` (24h), so a permanently undeliverable result is not retried on every start until the archive prunes it.

That is why `Notifier.notify` **raises** on a failed post rather than swallowing it: returning normally is what marks a result delivered, so an adapter that hides a failure turns a retryable miss into a reply nobody ever receives.

**Outcomes are reported back to the producer.** `OutcomeRecorder` is the fourth port: the worker calls it once per completed job, after the result is durable and before delivery. Only a *producer* needs it — `cron.py` records attempts when it enqueues, and without the matching outcome its backoff reads a `failures` count nothing increments. It shipped broken exactly that way once, with unit tests green because they called the store directly, so the composition test now asserts the daemon's own wiring leaves a failure on record. Implementations must not raise, and the worker guards the call anyway: it sits between a durable result and its delivery, so a bookkeeping bug must not cost the reply.

**Failures are alerted to a monitoring surface.** `AlertSink` is the fifth port: the worker calls it once per completed job, after delivery, only when `result.ok` is False. A cron-origin failure has no live thread — the entry's log is the only record, and nobody is watching it — so the sink posts a compact heads-up to a configured channel or chat. The sink decides whether the origin is alertable; the core never reads `origin`. The factory `build_alert_sink` (`jobs/alerts.py`) reads `slack.alert_target` and `telegram.alert_target` and returns None when neither is set, so alerts are opt-in and an install that never configured one behaves exactly as it did before. The sinks are wrapped in three layers: `CronOriginAlertSink` alerts only cron-origin failures (a Slack-origin job's failure already replies in its thread, where the requester is watching); `MultiAlertSink` fans out to every configured platform, each guarded, and raises only when every sink failed — which is what keeps the cooldown from silencing the next attempt; `CooldownAlertSink` allows one alert per entry per `ALERT_COOLDOWN_S` (30 min), in-memory, because a failing entry fires on its own schedule and would otherwise spam the channel at every fire. The cron producer alerts through the same factory for its own failures — a side-effect command or a producer exiting non-zero — see [cron.md](cron.md). Two paths deliberately never alert: a cancelled job's interrupted notice is not a failure (the job re-runs), and a redelivered result does not re-alert (the alert fired at completion; a worker that crashed between completing and delivering loses it, which the entry's log already records).

A cancelled job's origin is **told**, from `worker.run_once`'s `except CancelledError`. It is a notice, not a delivery: nothing is marked, because there is no result to redeliver — the job itself re-runs. The notice is shielded and bounded by `NOTICE_BUDGET_S`, so the cancellation it reports cannot be delayed past the supervisor's grace, and a notifier that raises is logged rather than allowed to swallow the cancel. "It re-runs at the next start" is invisible from the thread that asked, which is the whole reason this exists.

**Execution is at-least-once.** A crashed worker leaves its job in `cur/`; `recover_stale` moves it back to `new/` on the next start, and shutdown deliberately *cancels* an in-flight job rather than finishing it (so the process tree is reaped inside the supervisor's grace). A job must therefore be safe to re-run.

**`done/` is pruned to 7 days on completion** (`DONE_RETENTION_S`), deliberately shorter than the 30-day log and workspace windows: the archive is rescanned in full on every completion, so widening it charges the hot path rather than the disk. By age rather than count, so a burst of small jobs cannot evict this morning's.

**`complete()` writes two files into `done/`** — `<id>.result.json` first (so a crash between the two still leaves a durable result), then the job file moves in. Anything counting finished jobs must count `*.result.json`; counting `*.json` double-counts every one of them.


**A failed run can explain itself, behind a flag.** `jobs.diagnose_failures` (default off, codex only) makes `OrchestratorAgentRunner` read the run's own rollout before it builds the failure `Result`, and append what the timestamps say. The alert already carries whatever the CLI reported, which for a timeout is only that it timed out; the signals answer the next question. Four rules, all arithmetic and substring matching, no model call: whether the rollout reached `task_complete` (if it did, the agent finished and the fault is on our side of the CLI boundary, which is the difference between a real agent crash and cotf mis-reading a healthy run); how the wall clock split between the model and its tool calls; whether a `.py` or `.sh` path named in the prompt ever appears in a tool call; and whether three or more tool results came back as errors. They live in `transcript.diagnose_codex`, and `_failure_signals` in `jobs/agent_runner.py` is the gate that keeps them opt-in, codex-only, and non-fatal — a diagnosis is a courtesy on a path that is already failing, so a broken read must never replace the real error with its own traceback.

Locating the rollout is the part with a trap. `_find_codex_rollout_by_cwd` looks right and is not: it reads only the single freshest rollout in the store, which is correct for its 1Hz live tailer (the run being watched *is* the freshest file) and wrong for a post-mortem. On a host firing cron every 15 minutes, several newer rollouts exist by the time a failure is diagnosed, and that helper found nothing on every real failure it was tried against. `_find_finished_rollout_by_cwd` is the post-mortem lookup: newest first, one line read per candidate, first cwd match wins, capped by `_MAX_ROLLOUT_CANDIDATES`. Unit tests do not catch this — each fixture writes one rollout, so any lookup passes. `TestRollutLookupUnderLoad` writes several.

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
| `slack.job_command` set empty | `warn` on `jobs` | The worker runs, but only `claude-jobs enqueue` can reach it. Advisory, not blocking — an enqueue-only install (cron, a git hook) is legitimate. |
| `jobs.queue_kind` not registered | `invalid` on `jobs` **and** `slack` | `make_queue()` raises on an unknown kind and the Slack frontend calls it while building the producer, so a jobs-side typo kills *Slack*. Only added to `check_slack` when the trigger is set, since that is the only time a queue is constructed. |
| Trigger live, no worker running | `warn` on `slack` | Advisory: the worker may start after the frontend. The ack itself also tells the truth at runtime — with no worker it says the job stays queued rather than promising a reply. |

`warn` is non-blocking by construction — `checks.is_blocking` is what every caller counting problems should use, so a `doctor` run reports the advice and still exits 0.

## The sandbox self-test is part of startup

`_run` calls `sandbox.verify_boundary()` before `build_components`, so the jail is proven before anything claims a job. Inert unless `sandbox.mode` is `jail`.

It is here because this worker spawns jailed agents through `agent.run` exactly as a chat turn does, and for a while only `orchestrator._start_sandbox` ran the self-test: a jail that could not hold `state/` stopped Slack loudly and left this worker draining the same queue across an unverified boundary.

A failure is **fatal** — `_cmd_run` returns 2 with one line on stderr, the same shape as the already-running and no-token refusals, so the supervisor treats all three alike. Nobody watching is the argument *for* that: the worker runs `bypassPermissions` turns against whatever a producer queued. The queue is durable and the jobs wait, so a refusal costs a restart; a start on an unverified boundary cannot be undone once the reads have happened. See [broker.md](broker.md) for the outcomes and for why the cron producer is not gated.

## Orphaned agent processes

`agent._exec` spawns the CLI with `start_new_session=True` so one `killpg` reaps the CLI *and* every tool subprocess it forked. The cost: the child is unreachable from the parent's group, and `supervisor.stop()` signals the worker pid alone before SIGKILLing it after a five-second grace. A worker that misses that window would leave a full agent CLI running with no parent and no record of its pid.

So `agent` announces every process group at spawn and after reap (`add_process_listener`), and the worker registers a `ProcessLedger` (`jobs/orphans.py`) that writes the group to `jobs/worker.pids` the moment it exists. What survives a SIGKILL is a file naming exactly what was orphaned; the next start sweeps it before claiming work, since `recover_stale` would otherwise re-run a job whose first copy is still executing.

The sweep refuses to signal a group whose current command no longer matches what was recorded — killing a stranger's recycled pid is far worse than missing an orphan. The recorded pgid is the child's own pid, which `start_new_session` guarantees without racing the child's `setsid`.

## The TUI tab

`[4]` on the dashboard (`tui/screens/dashboard.py`). Header liveness comes from the `jobs` heartbeat; the queue counts and rows come from the directory, so a backlog is visible with the worker stopped — the state an operator most needs to see. `k`/`r` stop/restart the worker when that tab is active, resolved through `_active_daemon()`; `jobs` is already in `supervisor._FRONTEND_MODULE` and `checks.SUPERVISABLE_FRONTENDS`, so `K`/`u` cover it too.

The `source` column names the producer, read off `origin`: a cron job shows the entry that fired it, so a queue row can be matched against `cron.yaml` and against that entry's own log, and every other producer shows its `origin["kind"]`. An origin with no kind predates the field and shows `-`. Adding a producer therefore means stamping `kind` on the origin it enqueues, or its jobs list as anonymous. `prompt` is the flexible column, sized to the width the other four leave over, because five fixed widths no longer fit an 80-column terminal.

A queue deeper than the row cap gets a trailing `… N more` row, so the cap never truncates in silence. `N` is counted only when the page came back full — a shorter page means a job finished between the depth read and the row read, not a hidden row.

The widget ids are `tab-jobs` / `#jobs-panel` / `#jobs-queue-header` / `#jobs-queue`. **`#cron-entries` is a different widget** — the cron tab's cron table — and must not be reused.

The watch pane tails a running job's live agent session: the runner publishes each in-flight job's `session_uuid` and workspace in `in_flight` (`jobs/agent_runner.py`), and the worker's heartbeat carries it as `running_jobs` — the same shape the chat orchestrator publishes, so the dashboard resolves both with one normalizer. The entry is cleared when the run ends (including on cancel), so the pane goes blank when the job leaves the queue.

## Workspaces

`session_key` decides where a job runs and how long that directory lives.

- **Unkeyed** (`session_key is None`): `workspaces/<platform>/__runs/<uuid4>`. A one-shot. Nothing records the run id, so nothing can ask for that workspace again.
- **Keyed**: `workspaces/<platform>/<safe_segment(session_key)>`, reused by every later job with the same key. Both the path and the session uuid derive from the key, so the resume needs nothing else persisted.

**No run deletes its own workspace**, keyed or not. Finished one-shots are retired in bulk by `sweep_run_workspaces` at worker startup (`jobs/cli.py`), which drops `__runs/` entries whose mtime is older than `jobs.workspace_keep_days` and takes each one's backend session directory with it — `remove_workspace_sessions` first, while the path still resolves, or `~/.claude/projects/<hash>/` keeps a dead directory per job that nothing can name again.

Three things about that split are load-bearing:

- **Isolation does not come from the delete.** Each one-shot gets its own uuid, so the next run is clean whatever happened to the last one. Keeping a finished workspace only costs disk, and buys an operator the files a run that failed overnight left behind.
- **Retention is at startup, never on the run path.** A per-run rmtree lands during shutdown, where an agent that cloned a repo leaves a tree that takes seconds to remove, competing with the in-flight cancel for the supervisor's 5s grace. Losing that race means a SIGKILLed worker with an orphaned agent CLI still holding `bypassPermissions`. `tests/jobs/test_agent_runner.py::test_a_run_never_discards_a_workspace_itself` guards it.
- **`__runs` has two underscores on purpose.** `safe_segment` collapses runs of unsafe characters to a single `_`, so no sanitized key can produce that name. A single-underscore `_runs` is reachable from a `session_key` of `/runs`, which would point the sweep at a live keyed workspace and have it delete the files inside as if they were dead runs.

A keyed workspace is never swept, however old. It *is* the continuity for the next job with that key, and age cannot distinguish "abandoned" from "waiting" — a ticket nobody has touched in a year still expects turn 2 to resume turn 1. Growth is bounded by the number of distinct keys, which an operator controls, rather than by the number of runs.

## Logging

`preflight.setup_daemon_logging("jobs")` adds a midnight-rotating file handler (7 backups) beside the console, which `preflight._setup_logging` — console-only — does not: without it `logs/jobs.log` is never written and the tab's log pane has nothing to tail. Every supervised daemon uses the same helper. Console and file share one level: `basicConfig` sets the *root* logger from `LOG_LEVEL`, so the file handler's own `DEBUG` floor only bites when `LOG_LEVEL=DEBUG`.

## Config

| Setting | Default | Meaning |
|---|---|---|
| `jobs.queue_kind` | `file` | Which `SUPPORTED_QUEUES` adapter to build |
| `jobs.poll_interval_s` | `2.0` | Idle wait between drain attempts |
| `jobs.timeout` | `agent.DEFAULT_TIMEOUT` | Per-job wall clock; `0` or negative means no limit. A `Job.timeout` from a producer overrides it |
| `jobs.concurrency` | `1` | How many jobs run at once. A property of the machine, deliberately separate from a producer's own `max_concurrent` |
| `jobs.workspace_keep_days` | `30` | How long a finished one-shot workspace is kept before the startup sweep retires it; `0` keeps them forever. Keyed workspaces are never swept |
| `jobs.diagnose_failures` | `false` | Experimental. On failure, read the run's own codex rollout and append deterministic signals to the alert text. Codex only |
| `JOBS_SLACK_TOKEN` | falls back to `SLACK_TOKEN` | Notifier token. Stays in `.env`: it is a credential |

Set `JOBS_SLACK_TOKEN` to a **bot** (`xoxb-`) token. Inheriting a user (`xoxp-`) token from `SLACK_TOKEN` makes the worker post results as that user, and the Slack frontend — a separate process with its own dedup set — re-ingests them as new input, one spurious agent turn per job. `cli._notifier_loop_warning` warns about exactly this at startup.


## Panes

A job runs inside its own tmux pane when tmux is present, named
`cotf-job-<run id>` where the run id is the workspace's directory name. The TUI's watch
pane mirrors it, and `OrchestratorAgentRunner` reaps it with `tmux.kill` in the same
`finally` that clears `in_flight`. The worker sweeps whatever a killed worker left behind
at startup, beside the workspace sweep. See [panes](panes.md).
