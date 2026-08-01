# Configuration files and lifecycle

Claude-on-the-fly reads three operator-owned files.

| File | Purpose | Reload |
|---|---|---|
| `~/.claude-on-the-fly/.env` | Credentials and `LOG_LEVEL` | Restart the affected daemon |
| `~/.claude-on-the-fly/config.yaml` | Shared, non-secret settings | Usually live; exceptions below |
| `~/.claude-on-the-fly/cron.yaml` | Scheduled entries | Within one minute |

See the exact schemas for [`config.yaml`](config-yaml.md), [environment
variables](environment.md), and [`cron.yaml`](cron-yaml.md).

## Precedence and upgrades

Legacy environment variables for `config.yaml` settings still work and win over YAML.
The daemon warns once and names the replacement key. New options are documented only
in YAML.

The first daemon start seeds `config.yaml` from the packaged template. An existing
operator file is never overwritten on upgrade. Missing fields inherit current packaged
defaults; open the packaged reference through `[g]` in the TUI to see new options.

Sections are merged independently. A malformed operator section logs an error and
falls back for that section without discarding unrelated sections.

## Lifecycle vocabulary

| Lifecycle | Meaning |
|---|---|
| Immediate | The next authorization, policy, or formatting check reads it |
| Next turn | The next agent spawn reads it |
| Next grant | Existing grants keep their expiry; newly requested grants use it |
| Startup | Read while constructing a frontend, worker, handler, or service |
| Restart required | The running process deliberately retains its startup value |

## Restart-required settings

| Setting | Restart | Reason |
|---|---|---|
| `sandbox.mode` | Chat daemon | Broker, proxy, and jail posture are constructed together |
| `commands` | Chat daemon | PATH shims and command broker are constructed once |
| `permissions.mode` | Chat daemon | Approval service and backend artifacts are constructed once |
| `slack.slash_command` | Slack daemon | Registered with Slack at startup |
| `slack.job_command` | Slack daemon | Trigger and producer queue are constructed together |
| `jobs.queue_kind` | Slack and jobs daemons | Producer and worker must use the same adapter |
| `jobs.concurrency` | Jobs daemon | Worker pool size is fixed for the run |
| `jobs.poll_interval_s` | Jobs daemon | Worker loop receives it at startup |
| `jobs.timeout` | Jobs daemon | Agent runner is constructed with it |

The chat frontend reports changes represented by `settings.RESTART_REQUIRED` on the
next turn. The jobs worker cannot post that notice; restart it explicitly after editing
worker settings.

## Startup-only controls

These are not safe to describe as live even though they do not all appear in the
restart notification:

- `agent.pty.auto_install` and `agent.pty.auto_refresh` act during preflight.
- `logs.keep_days` acts when pruning runs, normally daemon startup.
- `logs.host_tag` selects the current log filename at handler construction; a daily
  rollover reads it again.

## Validation

Unknown top-level YAML sections are reported. Individual readers validate security
policy and numeric values, but unknown nested keys are not universally rejected. Use
doctor after editing rather than treating a silent key as proof it was accepted:

```bash
uv run claude-tui  # press d
```
