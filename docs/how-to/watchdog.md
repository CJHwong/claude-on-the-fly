# Run the daemon watchdog

`claude-watchdog` is a one-shot health check for one daemon. It reads the same
heartbeat as the dashboard and, when the daemon is missing, stale, or past a managed
turn's real execution deadline, performs a serialized clean restart.

Run it once without changing anything:

```bash
claude-watchdog --frontend slack --dry-run
```

Then have the scheduler your machine already uses invoke it periodically. For
example, a systemd timer or cron entry can run:

```bash
claude-watchdog --frontend slack
```

Valid frontend names are `slack`, `telegram`, `cron`, and `jobs`. A watchdog run
does not stay resident and cotf does not install a launchd plist, systemd unit, cron
entry, or container policy. That remains deployment configuration because the
executable path, user, schedule, and service manager are machine-specific.

Tune the health thresholds in `config.yaml` only when the defaults do not fit:

```yaml
watchdog:
  heartbeat_stale_seconds: 90
  limit_grace_seconds: 120
```

The timeout check applies only when a heartbeat row advertises a real execution
timeout. Unmanaged turns and autonomous workers are recovered for missing or stale
heartbeats, not killed because they ran for an arbitrary duration.

## Managed release symlinks

A stable launcher can prevent an old dashboard or watchdog process from starting
daemons from a retired release. Set `COTF_EXPECTED_RUNTIME_EXECUTABLE` to the Python
executable under the managed `current` symlink before launching either command:

```bash
export COTF_EXPECTED_RUNTIME_EXECUTABLE=/opt/cotf/current/.venv/bin/python
exec "$COTF_EXPECTED_RUNTIME_EXECUTABLE" -m claude_on_the_fly.watchdog --frontend slack
```

The check compares virtual-environment directories after resolving the `current`
symlink. Stops remain available, but start and restart refuse to run from a dashboard
whose release is no longer current.
