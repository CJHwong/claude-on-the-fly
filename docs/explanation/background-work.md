# Background work

Cron is a producer and `claude-jobs` is a worker. Separating them keeps schedules,
durable queueing, agent capacity, and result delivery independently observable.

## Three cron entry shapes

- A prompt creates one fresh-session job per fire.
- A command plus prompt emits keyed items whose sessions resume across fires.
- A command without a prompt runs directly for side effects.

Keyed producer output provides deduplication and progress detection. An unchanged item
is parked after `max_fires`; a changed item skips failure backoff because it contains new
information.

## Two concurrency limits

`max_concurrent` caps outstanding work from one cron entry. `jobs.concurrency` caps agent
processes on the machine. Raising the first cannot exceed worker capacity; raising the
second lets unrelated producers run together.

## Delivery and recovery

The file queue claims by atomic rename. Completion and reply delivery are tracked
separately, so a crash after the agent finishes retries only the reply, not the tool
effects. In-flight work is at-least-once after a worker crash.

Background agents do not receive interactive tool approvals. Their credentials and
prompts must be scoped for unattended execution.
