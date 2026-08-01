# Documentation

Choose the kind of help you need.

## Learn by doing

[Build a safe Slack installation](tutorials/first-safe-deployment.md) starts from an
empty configuration and ends with a sandboxed agent whose tool calls require approval.

## Complete a task

- Set up [Slack](how-to/slack.md), [Telegram](how-to/telegram.md), or
  [scheduled work](how-to/cron.md).
- [Enable sandboxing](how-to/enable-sandboxing.md).
- [Enable tool approvals](how-to/enable-tool-approvals.md).
- [Allow network access](how-to/manage-egress.md).
- [Broker a credentialed CLI](how-to/broker-a-command.md).
- [Configure background workers](how-to/configure-workers.md).
- [Troubleshoot configuration](how-to/troubleshoot-configuration.md).

## Look something up

- [Configuration files and reload behavior](reference/settings.md)
- [`config.yaml` schema](reference/config-yaml.md)
- [Environment variables and credentials](reference/environment.md)
- [`cron.yaml` schema](reference/cron-yaml.md)
- [Persona](reference/persona.md) and [response footer](reference/footer.md)

## Understand the design

- [Configuration model](explanation/configuration.md)
- [Security model](explanation/security-model.md)
- [Tool approval model](explanation/tool-approvals.md)
- [Background work](explanation/background-work.md)

Implementation notes for contributors remain under [`docs/agent/`](agent/).
