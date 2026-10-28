# `cron.yaml` reference

Path: `~/.claude-on-the-fly/cron.yaml`. The cron daemon reloads it at most one minute
after a valid save. A failed reload keeps the prior entries.

```yaml
entries:
  - name: standup
    cron: "30 6 * * 1-5"
    prompt: "Summarize yesterday."
```

## Entry fields

| Key | Type / default | Constraints |
|---|---|---|
| `name` | string / required | Unique; letters, digits, `_`, and `-` |
| `cron` | string / required unless `at` | Standard five-field expression |
| `at` | string / unset | One-shot fire time, in local time: ISO (`2026-09-19 09:00`, bare `2026-09-19`) or relative (`30m`, `2h`, `1d`). Mutually exclusive with `cron`; needs a `prompt` or `prompt_file` and no `command`. Fires once, then the entry is spent -- the fire is recorded durably, so a daemon restart cannot run it a second time. An `at` already in the past fires on the next loop tick (a daemon that was down at the fire time runs the work late rather than never). To run it again, set a new `at` |
| `prompt` | non-empty string | Mutually exclusive with `prompt_file` |
| `prompt_file` | path | Re-read every fire; relative to `cron.yaml` |
| `command` | non-empty shell string | Producer when paired with a prompt; side effect otherwise |
| `timeout` | integer / `1800` | `1..86400` seconds; bounds the agent run, or a bare command |
| `producer_timeout` | integer / `120` | `1..3600` seconds; bounds the producer command that prints the work list. Requires a producer |
| `max_concurrent` | integer / `1` | At least 1; above 1 requires a producer command |
| `max_fires` | integer / `3` | Unchanged fires before parking; `0` disables parking |
| `profile` | string / unset | Names an `agent.profiles` block in `config.yaml`; needs a prompt, since a bare command runs no agent |
| `min_tool_calls` | integer / `0` | Fewest tool calls a fire must make to count as a success; `0` is no floor. Needs a prompt, since a bare command runs no agent |
| `catch_up` | string / unset | A window: `30m`, `2h`, `1d`. On start, if the entry's latest missed fire is inside the window, it fires once for that time. Needs `cron`, not `at` |

Every entry needs a prompt, prompt file, or command. A producer command writes one JSON
object per line with a non-empty `key`.

Any key outside this table is rejected at load. A key nobody reads would sit on a live
entry doing nothing and saying nothing, which reads exactly like a setting that works.
The legacy `script` and `args` are the exception: they are translated to `command`
before the check.

`timeout` and `producer_timeout` bound two different things. `producer_timeout` bounds
the command that prints the work list, and is measured in seconds. `timeout` bounds the
agent run each printed item becomes, and is measured in tens of minutes.

`min_tool_calls` catches a fire that ran and did nothing. A backend that answers
raises nothing, so an agent that refused, or replied without acting, is recorded as a
success and alerts nobody. Set the floor and a run below it is recorded `FAILED` and
alerts on the ordinary failure path. Leave it unset on an entry that legitimately has
nothing to do on some fires, or every such fire alerts.

## Missed fires

The daemon records each scheduled fire of a `cron` entry. On start it counts the fires
that came due while it was stopped:

1. Each entry that missed fires logs `cron: missed <name> xN (last <time>)`.
2. The start sends one alert to the configured alert channel. The alert lists every
   entry that missed fires.
3. If the entry has `catch_up` and its latest miss is inside that window, the entry
   fires once, for the latest missed time only. An entry that missed several fires
   still runs once.

An entry with no record yet (new, or first start after an upgrade) starts its record
then and replays nothing. A changed `cron` expression also starts a new record, so the
old schedule's times never count as missed. Fires missed while the daemon runs are not
caught up here; a late wake-up already fires them.

## Scheduled time

Every fire carries the local time it was scheduled for, as `2026-10-28T18:00:00`:

- A prompt reads it as `{{ scheduled_at }}`.
- A command or producer command reads it as `$COTF_SCHEDULED_AT`.

A catch-up fire carries the missed time, not the restart time. A run-now fire carries
the time of the request.

An unknown `profile` fails at load, so a typo is caught when you save the file rather
than on the entry's next fire. Changing a profile's model changes the session identity,
so a keyed entry starts a fresh transcript on its next fire instead of resuming. Prompt text is strict Liquid; producer fields
are available under `item`.

The old root key `jobs` is accepted and migrated to `entries`. Legacy `script` plus
`args` is accepted and translated to `command`.

See [Run scheduled work](../how-to/cron.md) for examples and operating guidance.
