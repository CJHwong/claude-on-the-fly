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

## Nothing blocks the minute scan

`_fire` starts an entry's work and returns, for every kind of entry. The
scheduling loop awaits it once per due entry, so a slow `_fire` is a delay
charged to every other entry due in the same minute. A producer's shell command
used to be awaited inline: a measured 38s poll, firing four times an hour, made
the four other entries due at `:00` start 38s late. Nothing was lost, because
`next_fire` advances before the await, but late is its own failure.

So a producer goes through `_spawn`, the same helper as a side-effect command:
one task, named after its entry, tracked in `_command_tasks`. That name is the
only route from a task back to its entry, and three things read it — `stop`,
`heartbeat_extra`, and `_is_running`.

Two things follow from moving work into a task, and both are easy to get wrong.

1. **The failure has to report itself.** `_fire`'s try/except now covers only
   *starting* the task. `_logged` wraps the work so an exception is logged
   against its entry, instead of sitting unretrieved in a Task object until the
   garbage collector mentions it. A swallowed poll failure is the bug this
   daemon can least afford, which is why `_fire_producer`'s own `_alert_failure`
   is inside the task rather than around it.
2. **`_spawn` takes the coroutine function, not a coroutine.** A task cancelled
   before its first step never calls it, and `stop` cancels exactly that way. A
   coroutine built by the caller would be left unawaited there.

**One producer per entry at a time.** `_spawn_producer` refuses a fire while
that entry's previous producer is still running, because two overlapping runs of
one poll emit the same work list twice. The fire is dropped, not queued: a
producer that cannot keep up with its own schedule is a config problem, and the
next scheduled fire polls again anyway. It logs at INFO, since the operator's fix
is to widen the cron expression or speed the command up. The guard is
`_is_running`, which asks each task rather than reading
`_running_command_names`: a finished task stays in `_command_tasks` until its
done-callback runs a tick later, and letting that veto the next fire would drop a
poll for no reason.

A side-effect command has no such guard, and never had one. Its overlap costs a
second subprocess, not a duplicated work list.

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

`_validate_entry` is the schema. New fields go on `CronEntry`, get validated
there, and get added to `ENTRY_KEYS`; there is no separate schema file. **A field
missing from `ENTRY_KEYS` is refused at load**, which is deliberate: an unknown
key used to be ignored, so an inert `model: sonnet` sat on a live entry doing
nothing and saying nothing while the operator who wrote it believed it worked.
The check runs after `_translate_legacy_entry`, so the legacy `script` and `args`
are gone by then and a pre-rename config still loads.

That strictness is only safe because the two failure paths differ, and both are
pinned by tests. `cron.main` turns the `ValueError` into `SystemExit`, so the
daemon refuses to start on a config it cannot fully read. `_maybe_reload` catches
it, logs `keeping prior entries`, and returns, so a typo saved into a live config
does not empty the schedule. Keep that asymmetry when you tighten anything here.

Reject illegal *combinations* explicitly rather than ignoring them —
`max_concurrent > 1` without a `command` is an error, because silently ignoring
it would leave somebody waiting for parallelism that cannot happen.

`profile` is the current example: it is refused on an entry that has a `command`
and no prompt, because such an entry runs a shell rather than an agent. It is also
checked against the profiles defined in `config.yaml` at load, which is the one
validation here that reads a different file.

`min_tool_calls` follows the same rule for the same reason: a bare command never
reaches an agent, so a floor on its tool calls would bound nothing.

## A run that did nothing is not a success

A fire that raises is a failure the worker already alerts on. A fire that *answers*
without acting was not, because the backend returns normally either way. That is not
hypothetical: a scheduled fire replied that a security boundary prohibited the work
it was asked to do, made zero tool calls, and went down `ok=True` with no alert.

`min_tool_calls` on an entry is the floor. `Response` already carries `tool_counts`,
so `agent_runner` sums it, puts the total on `Result.tool_calls`, and returns
`ok=False` when the total is below the entry's floor. That is deliberately all it
does: the worker already alerts on `not ok` and `LogNotifier` already writes `FAILED`,
so the floor rides the path a raised error takes instead of adding a second one.

It is off by default and per entry, not global. An entry can have a designed periodic
no-op: a guard that acts only every other run does nothing on the runs in between, and
says so in its reply. A blanket floor would alert on every one of those. An entry only
gets a floor when doing nothing is genuinely a fault for that entry.

## Why a profile change restarts a transcript

`current_backend_key()` is `backend:mode:model` and seeds the session uuid, so a
profile that changes the model gives a keyed entry a session it has never written
to. Nothing migrates the old transcript: the cross-backend handoff in `transcript`
already covers picking up prior context, and a migration would be machinery for a
case an operator caused deliberately. Effort is not part of the key, so changing it
leaves the session alone.

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

Two different limits, and the vocabulary keeps them apart:

- a producer command runs in its own task and gets `PRODUCER_TIMEOUT_S` — it only has to print a work list.
  An entry whose producer is genuinely slow raises its own with `producer_timeout`,
  bounded by `MAX_PRODUCER_TIMEOUT_S`; the constant is the default, not the ceiling.
  The key is refused on an entry with no producer, for the reason `profile` is
  refused on a bare command: it would bound nothing.
- a side-effect command, and the agent run each item becomes, get the entry's own
  `timeout`, which rides to the worker on `Job.timeout`

The two must not be collapsed into one number. A producer that takes minutes is
broken; an agent run that takes minutes is working.

## What the cron tab shows as "running"

An entry's own work is done the moment it enqueues, so the cron daemon cannot say whether the work it produced is running — the jobs worker owns that. The cron table answers it from the queue instead: `_running_by_entry` counts the in-flight rows whose `origin` names that entry, and the row's next-fire cell reads `running`, or `running (N)` for a producer entry with several items in flight. Only a claimed job counts; a queued one leaves the countdown, which is still the honest answer to when the entry next fires. The count depends on the producer stamping `{"kind": "cron", "entry": <name>}` on the origin (`_enqueue`) — drop that and the table goes quiet rather than wrong.

## What the preview shows

The cron tab opens on the bottom viewport's preview mode, which shows the highlighted
entry's own text: the command and the prompt as separate sections, and a `prompt_file`
inlined under its path. The path alone answers "where", never "what", and "what" is the
question an operator opens the schedule with.

It is the *source* text, never a rendering. A prompt is a Liquid template, so
`{{ item.key }}` is what the operator wrote and what the preview shows; rendering it
would need an item, and a producer's items are not knowable without running it.

`state._read_prompt_file` memoizes on the file's `(mtime, size)` rather than reading per
tick, and caps the text. It deliberately does not share `CronEntry.prompt_source()`,
which stays uncached: a fire happens at most once a minute and has to read what is on
disk right then, while the dashboard asks 60 times in that minute for the same bytes. A
read failure is not cached either, so restoring a deleted file recovers on the next tick.

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
WARNING, rather than telling anybody. Tasks are named after their entry
(`_spawn`) purely so that log line and `heartbeat_extra`'s `running_commands`
can identify them; the task object carries no other route back to the entry.
Producers are in that set too, so a stop cuts a poll in flight and says so.

`heartbeat_extra` publishes `running_commands` for one reader:
`supervisor.pending_work`, which says what a stop will cancel *before* it signals.
