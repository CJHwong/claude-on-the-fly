# Cron

Run agent prompts and shell commands on a schedule. Two daemons cooperate:
`claude-cron` decides *what* to work on, `claude-jobs` runs it.

Write `~/.claude-on-the-fly/cron.yaml`:

```yaml
entries:
  # 1. plain: one job per fire, fresh session each time
  - name: standup
    cron: "30 6 * * 1-5"          # Mon-Fri 06:30
    prompt: "Summarise yesterday's merged PRs and post them to the team channel."
    timeout: 1800                 # optional, default 1800s, max 86400

  # 2. producer: the command lists work items, each becomes its own job
  - name: jira
    cron: "*/2 * * * *"
    max_concurrent: 3             # how many of THIS entry's items run at once
    max_fires: 3                  # fires against an unchanged item before parking
    prompt_file: ./prompts/jira.md
    command: |
      acli jira workitem search --jql 'assignee = currentUser() AND status not in (Done)' \
        --fields key,status,summary --limit 20 --json \
        | jq -c '.[] | {key, status: .fields.status.name, summary: .fields.summary}'

  # 3. side effect: no prompt, no agent, just runs
  - name: prune
    cron: "0 4 * * *"
    command: ~/scripts/prune.sh --verbose
```

Then run both daemons:

```bash
uv run claude-cron    # fires entries, enqueues work
uv run claude-jobs    # runs the queued work
```

Output goes to `~/.claude-on-the-fly/logs/cron-<name>-<host>-<date>.log`, agent
replies included. Config edits are picked up within a minute, no restart.
`prompt_file` is re-read on every fire, so editing a brief takes effect next fire.

## The three shapes

| Keys | What happens |
|---|---|
| `prompt` or `prompt_file` | One job per fire. Keyed to the entry, so a slow run blocks the next fire instead of overlapping. Fresh session every time. |
| `command` + a prompt | Producer. Each stdout line is one work item; each becomes a job whose session **resumes** across fires. |
| `command` alone | Subprocess run for its side effects. No job, no agent. |

`prompt` and `prompt_file` are mutually exclusive. `max_concurrent` above 1 needs
a `command`, since without one there is only ever a single item.

## Writing a producer

The command must print **one JSON object per line**, each with a `key`:

```
{"key":"ACE-1","status":"In Progress","summary":"Fix the thing"}
{"key":"ACE-2","status":"Backlog","summary":"Other thing"}
```

`jq` emits an array by default, so pipe through `jq -c '.[]'` to get lines.

The `key` is what makes dedup and session resume work: an item already queued or
running is not enqueued again, and the next fire for the same key continues the
same agent session rather than starting over.

**Emit a field that moves when the work moves.** This is the one rule worth
getting right. Each item is fingerprinted, and an item whose fingerprint has not
changed after `max_fires` fires is *parked* until it does — that is what stops an
item the agent cannot advance from being worked forever.

For a Jira poller, that field is `status`. Note that `acli jira workitem search`
rejects `updated` outright (`field 'updated' is not allowed`), so status is what
you have, and it works: if your workflow moves a ticket's status as the agent
progresses, progress is visible. An agent that only comments will park after
`max_fires`, which is usually the right outcome — three attempts with no movement
is when a human should look.

Emit the object's fields consistently. Templates render strictly, so an item
missing a field the template references is skipped with a warning. Give optional
fields a default in `jq`:

```
jq -c '.[] | {key, status: .fields.status.name, priority: (.fields.priority.name // "none")}'
```

## Prompt templates

Both `prompt` and `prompt_file` are [Liquid](https://shopify.github.io/liquid/)
templates. A producer's items arrive as `item`:

```markdown
Work on {{ item.key }}: {{ item.summary }}
Current status: {{ item.status }}

Transition the ticket as you progress. Stop when it is done.
```

A plain entry has no `item`, and referencing one is rejected when the config
loads rather than at 3am.

## Failure handling

A key whose job fails backs off exponentially (10s, 20s, 40s… capped at 5min)
instead of retrying every poll. A success clears the streak, and an item that
*changes* skips the wait — new information should not sit behind a backoff.

## Checking your config

```bash
uv run claude-tui   # then `d` for doctor
```

Doctor parses the config and compiles every template, so a bad cron expression, a
missing `prompt_file`, or a template typo shows up before you start the daemon.
`claude-cron` also refuses to start on a config it cannot load.

## Concurrency

Two independent numbers:

- `max_concurrent` on an entry — how much of *that entry's* work may be
  outstanding at once. Default 1.
- `jobs.concurrency` on the worker — how many agents *this machine* runs at once.
  Default 1.

## Coming from `schedule.yaml`

The first `claude-cron` start migrates an existing config for you: it writes
`cron.yaml` and renames the original to `schedule.yaml.migrated`.

The migration is a text move, not a rewrite. Exactly one line changes, the root
`jobs:` key becoming `entries:`, and everything else survives byte for byte
because your comments are the part of a config that cannot be regenerated. That
also means a `script:` + `args:` pair is left exactly as you wrote it: it still
loads (the two are read as one `command:`), and `command:` is simply the current
spelling if you want to modernize it later.

The flip side of preserving comments is preserving stale ones, so notes describing
the old key names come across too. The migrated file says as much at the top. To
start over from the fully commented current example, delete `cron.yaml` and open
the config from the TUI, which re-seeds it.

Nothing is destroyed and the migration is idempotent: it is a no-op once
`cron.yaml` exists, so it will never overwrite a config you have since edited.
