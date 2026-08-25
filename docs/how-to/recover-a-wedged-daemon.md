# Recover a wedged daemon

A daemon can keep its process alive while it stops doing anything: an event loop
that never yields again, a disk that stops accepting writes, a task that dies and
takes the connection with it. The dashboard shows this as **broken**, because the
heartbeat stops advancing while the pid stays alive.

Until you open the dashboard, nothing acts on that. `claude-watchdog` is the
part that acts.

## Run it

```bash
claude-watchdog --frontend slack
```

It checks once, decides, and exits. It restarts the daemon through the same
`restart` path an operator uses, so the replacement gets the same preflight.

Check what it would do without changing anything:

```bash
claude-watchdog --frontend slack --dry-run
```

Exit codes: `0` for a decision made, `2` for a supervisor refusal, with the
reason on one line.

## Give it a schedule

The command installs no timer. Point whatever already supervises this machine at
it, every minute or two.

launchd:

```xml
<key>ProgramArguments</key>
<array>
  <string>/path/to/.venv/bin/claude-watchdog</string>
  <string>--frontend</string>
  <string>slack</string>
</array>
<key>StartInterval</key>
<integer>120</integer>
```

systemd, as a timer plus a `Type=oneshot` service:

```ini
[Timer]
OnUnitActiveSec=2min
```

cron:

```cron
*/2 * * * * /path/to/.venv/bin/claude-watchdog --frontend slack
```

Run one per frontend you want covered.

## What it will and will not do

It restarts a daemon whose **process is alive and whose heartbeat has gone
stale**. That state is never something you asked for, so recovering it cannot
work against you.

It does **not** start a daemon that is stopped. Nothing on disk separates a crash
from `claude-tui stop slack`, or from the moment inside `claude-tui upgrade`
between stopping the daemons and bringing them back. Starting one would undo a
stop you meant, and during an upgrade it would launch the daemon from
half-replaced code while racing the upgrade's own resume. If a daemon is down and
you want it up, `claude-tui start` is the command.

## Tune the patience

```yaml
watchdog:
  stale_seconds: 90
```

The dashboard calls a daemon broken after 15 seconds. That threshold is built for
a colour: showing a cell early costs nothing. Restarting early costs every turn
in flight, and the heartbeat coroutine can be briefly starved by a busy poll or a
slow tracker call. So the watchdog waits longer before acting. At the 5 second
heartbeat interval, the default is 18 consecutive misses.

Read per run, so an edit applies on the next tick with nothing to restart.
