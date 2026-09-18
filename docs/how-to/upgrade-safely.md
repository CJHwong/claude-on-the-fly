# Upgrade safely

An upgrade fetches the new code while the daemons keep serving, then stops them, swaps the
code, and starts them again. The daemons are down for the swap alone.

Warning: a turn an agent had already started is resumed with a note telling it to check
what it already did, but a side effect it had completed before the stop cannot be undone.

Chat turns, background jobs, and cron commands all come back by themselves afterwards.

## Upgrade from the terminal

```bash
claude-tui upgrade
```

The command reports what it is about to interrupt, asks to continue, runs the prepare step
with the daemons still up, stops them, runs the activate step, and starts them again:

```
upgrade: git fetch (daemons stay up), then git merge --ff-only && uv sync   [git checkout at /srv/cotf]
pending work:
  slack: 1 running, 2 queued (lost, needs resending)
  jobs: 1 running, 3 queued (resumes after the restart)
stop everything (3 unrecoverable) and upgrade? [y/N]
```

A prepare step that fails stops the upgrade before anything is stopped, so a fetch that
cannot reach the network costs an error message rather than an outage. Use `--yes` to skip
the prompt in a script. Use `--no-resume` to leave the daemons stopped.

The daemons start again even when the activate step fails, so a failed swap leaves the old
build running rather than nothing.

All of it is on disk and comes back on its own. "Lost, needs resending" describes what a
stop costs a chat *now*, before the resume; see the table below.

## Upgrade from the dashboard

Press `U`. The modal shows the same commands, which step costs uptime, and the same pending
work. The dashboard relaunches itself on the new code once the daemons are back, and writes
both steps' output to `~/.claude-on-the-fly/logs/upgrade-<host>-<date>.log`.

## The two steps

An upgrade is up to two commands, split by whether the daemons have to be down for them.

| Step | When it runs | What belongs in it |
|---|---|---|
| `prepare_command` | First, with every daemon serving | Work that changes nothing a running daemon reads. A fetch. |
| `command` | Second, with the daemons stopped | Work that replaces the code underneath them. A merge, a checkout, a sync. |

The split is what keeps the outage short. A command that changes files a daemon loads has to
be in the second step; a command that only reaches the network belongs in the first one.

## Choose the commands

Leave both unset unless the derived commands are wrong for your deployment:

| Install | Prepare | Activate |
|---|---|---|
| Git checkout | `git fetch` | `git merge --ff-only && uv sync` |
| `uv tool install` | none | `uv tool upgrade <tool>` |
| Anything else | none | Refused; set `upgrade.command` |

A `uvx --from git+...` run needs no upgrade: it fetches the current code at every start.

```yaml
upgrade:
  prepare_command: git fetch --tags
  command: git checkout v1.4.0 && uv sync
```

Set `prepare_command` only when the split is safe. Everything it changes is read by a daemon
that is still serving, so a fetch is fine and a checkout is not. Setting `command` alone
keeps the single-step behaviour, because an operator's own command is not split for them.

## What each daemon does when it stops

| Daemon | Pending work | On stop |
|---|---|---|
| Slack, Telegram | Running turn, queued turns | Both are journaled and resumed on the next start, silently |
| `claude-jobs` | Claimed job | The origin thread is told; the job re-runs at the next start |
| `claude-cron` | Running command | Named in the daemon log; the entry fires again on schedule |

Stop, restart, and stop-all also report pending work and give the daemon 20 seconds to
send those notices. `--force` cuts the grace to 5 seconds, which stops the notices
mid-send. Use it only when a daemon is already unresponsive.

## What comes back, and what does not

A chat turn is written to `~/.claude-on-the-fly/state/<platform>.turns.json` when it
is accepted, so recovery also covers a crash, an OOM kill, and `--force`, not only a
clean stop.

| State when the daemon stopped | On the next start |
|---|---|
| Queued, never started | Runs, exactly as sent |
| Already running | Runs again, told to check what it already did first |
| Stopped on purpose with `$stop` | Nothing. You stopped it |
| Older than 30 minutes | Dropped, so yesterday's question does not resurface |
| Already retried twice | Offered back instead, since retrying is what kept failing |

Both ends of the pause are silent on Slack, and each state has its own reaction:

| Reaction | Means |
|---|---|
| 🔄 | Interrupted by a stop. It resumes when the daemon is back |
| ⏳ | Waiting its turn behind other work |
| 👀 | Running now. Cleared when the reply lands |

There is nothing to read at either end. A turn with no message behind it (a slash
command) gets one short line instead, since there is nothing to put a reaction on.

A turn that was already running is resumed rather than held back, on the assumption
that you asked for the work because you want it done. Its prompt says a restart
interrupted it and asks it to check the current state before repeating anything that
writes, sends, or publishes. That is a mitigation, not a guarantee: a push or an email
it had already completed is out of reach.

The file holds the text of unanswered messages. It is denied to the agent in both
directions under `sandbox.mode: jail`, and the daemon proves that at startup, but it
is plain text on disk: it falls under the same retention thinking as
`~/.claude-on-the-fly/logs`.
