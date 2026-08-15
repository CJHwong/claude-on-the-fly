# Cron

The producer half of the work loop. `cron.py` fires entries on a schedule and puts
`Job`s in the queue; `jobs/` runs them. See [jobs.md](jobs.md) for the other half
and [how-to/cron.md](../how-to/cron.md) for the config surface.

**This daemon runs shell and nothing else.** It never calls `agent.run`. Work that
needs an agent becomes a job. That split is the design, not an implementation
detail: the daemon decides *what* to work on, the worker decides *how long* and
*how many at once*.

That split is why `cron.main` does **not** call `sandbox.verify_boundary()`, unlike
the chat daemon and the job worker. This process crosses no jail, so the self-test
could only report on a boundary it never uses, and a fatal result would stop the
producer for a fault that cannot reach it. The worker already refuses to drain what
cron queued when the boundary is unproven, so nothing runs unverified either way.
Move the call in the moment cron spawns an agent itself.

## Why there is no reconcile pass

The daemon this replaced held a worker open per ticket across turns, which forced
everything else it had: `is_active` / `is_terminal` predicates, a per-tick
reconcile that cancelled workers mid-turn, terminal-state tracking, a retry queue.

One fire equals one agent run, so none of that exists. A ticket that moved to Done
stops matching the query, so it stops being enqueued, and that *is* the stop
signal. If you find yourself wanting to cancel an in-flight job because its item
changed, don't: let the run finish and let the next fire decide.

## The three gates on an item

`_admit` applies them cheapest first, and logs every rejection — a silent skip is
indistinguishable from a producer that found nothing, which is the hardest kind of
"why did this never run" to answer.

1. **Already queued** — `queue.count_unfinished(entry, item)`. The queue's own
   contents are the truth, so a worker killed mid-job cannot leave an item looking
   permanently outstanding.
2. **Over the entry's cap** — `count_unfinished(entry)` vs `max_concurrent`.
3. **Held by its own history** — `KeyStateStore.should_skip`: backing off after a
   failure, or parked for making no progress.

## Key state is the only thing the queue cannot answer

`jobs/key_state.py` holds one file per key: `{fingerprint, fires_since_change,
failures, last_failed_at}`. It replaces a cursor store, a retry queue, `max_turns`
and `max_no_progress_turns` with one file and one rule.

The fingerprint is a hash of the emitted item, which generalizes the timestamp
comparison it replaced: it needs no `updated` field from the source and works for
any producer. The cost is a burden on the producer, documented in the how-to — an
item whose emitted fields do not move will park.

**The loop only closes because the worker reports back.** The producer records
*attempts* at enqueue (`record_fire`); the worker records *outcomes* through the
`OutcomeRecorder` port (`record_outcome`). Wire both or the backoff is dead code
reading a `failures` count nothing increments — which is exactly how it shipped
broken once, with unit tests passing because they called `record_outcome` directly.

## Template rendering is strict

`StrictUndefined`, deliberately. Lenient rendering would put a silent hole in a
prompt when a producer omits a field, which is worse than a loud failure. Failures
split by blast radius: every item failing is a config bug (ERROR once, skip the
fire), some items failing is data variance (WARNING per item, run the rest).

Templates are compiled at config load, and a *plain* entry is additionally
dry-rendered against an empty context — that is what catches an entry referencing
`item` with no producer to supply one.

## Adding a config field

`_validate_entry` is the schema. New fields go on `CronEntry` and get validated
there; there is no separate schema file. Reject illegal *combinations* explicitly
rather than ignoring them — `max_concurrent > 1` without a `command` is an error,
because silently ignoring it would leave somebody waiting for parallelism that
cannot happen.

## Producer output

`parse_items` is deliberately forgiving per line and unforgiving per contract: a
bad line costs that line and logs it with the offending text. An array (the shape
`jq` emits by default) is rejected with the fix named in the message. An item with
no usable `key` is skipped, since it could be neither deduplicated nor resumed.

Producer stdout is capped (`MAX_PRODUCER_BYTES`) so a command that accidentally
streams a log file cannot exhaust memory, and its stderr is logged rather than
swallowed.

## Failures alert to the configured channel

A side-effect command or a producer that exits non-zero is logged, and — when
`slack.alert_target` or `telegram.alert_target` is set — also posted to that
monitoring surface through the same `build_alert_sink` factory the worker
uses, so one platform sender is written once and shared. The alert is a
heads-up, not a delivery: it is guarded, and the entry's log has the full
story either way. Agent-run job failures alert from the worker instead; see
[jobs.md](jobs.md).

## Timeouts

Two different limits, both spelled `timeout` in different places:

- a producer command gets a fixed short `PRODUCER_TIMEOUT_S` — it only has to
  print a work list
- a side-effect command, and the agent run each item becomes, get the entry's own
  `timeout`, which rides to the worker on `Job.timeout`

## Run-now trigger

The TUI's run-now key (`n` on the cron tab) is a file, not a signal: the
operator writes `state/cron.trigger` (`request_run_now`) and the daemon polls
it in `_sleep_to_next_minute` at 1s granularity, so a request lands within a
second instead of waiting out the minute. `_drain_triggers` renames the file
to `.draining` before reading it — a request written between the read and the
remove survives as a fresh file for the next drain instead of being deleted
unseen — then fires each named entry through `_fire`, so a run-now respects
the same gates as a scheduled fire (outstanding-job skip, `max_concurrent`,
key backoff). An unknown entry is logged and ignored; a `.draining` leftover
from a crashed drain is processed by the next one.

## Shutdown

`stop()` sets the stop flag and cancels every task in `_command_tasks`. Nothing is
lost that a notice could recover: an enqueued job is already durable, and a cancelled
command fires again on its own schedule. So the daemon names the entries it cut, at
WARNING, rather than telling anybody. Command tasks are named after their entry
(`_spawn_command`) purely so that log line and `heartbeat_extra`'s `running_commands`
can identify them; the task object carries no other route back to the entry.

`heartbeat_extra` publishes `running_commands` for one reader:
`supervisor.pending_work`, which says what a stop will cancel *before* it signals.
