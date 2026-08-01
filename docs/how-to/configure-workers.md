# Configure background workers

Slack `$job` requests and cron prompts share one `claude-jobs` worker.

```yaml
jobs:
  queue_kind: file
  concurrency: 2
  poll_interval_s: 2
  timeout: 3600
```

Restart `claude-jobs` after editing any worker setting. Restart Slack too after changing
`queue_kind`, because its producer must construct the same queue adapter.

Use a bot token for job replies:

```dotenv
JOBS_SLACK_TOKEN=xoxb-...
```

Without it, the worker falls back to `SLACK_TOKEN`. A user token can make Slack ingest
the worker's result as a new request.

`jobs.concurrency` is machine-wide agent capacity. A cron entry's `max_concurrent` is
that producer's outstanding-work cap. A cron entry's `timeout` overrides the worker
default for its own job.

Start the worker with `uv run claude-jobs`; run doctor before relying on unattended
work. Tool approvals do not apply to jobs.
