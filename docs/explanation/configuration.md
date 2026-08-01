# Configuration model

Configuration is split by risk and ownership.

Credentials stay in `.env`, where secret scanners expect them and daemons read them at
startup. Non-secret settings live at an absolute `config.yaml` path, so behavior does
not depend on the process working directory. Scheduled entries use `cron.yaml` because
they have their own schema and reload loop.

## Defaults versus operator intent

Packaged `config.yaml` supplies vetted defaults. The operator file is never overwritten;
missing values therefore mean “use this version's default,” while uncommenting a value
pins an explicit choice across upgrades.

`egress`, `commands`, and `permissions` ship real policy. Most other sections are
commented examples whose defaults remain in the reader. Environment forms from older
versions still win so an upgrade cannot silently remove an existing jail or access rule.

## Reload is a property of the consumer

Re-parsing YAML does not rebuild services. A sender list can reload at the next message;
a bound broker, generated PATH shim, or worker pool cannot. The lifecycle belongs in the
setting reference because “the file reloads” and “the running subsystem changed” are
different claims.

## Failure boundaries

Sections fail independently. A malformed egress list should not erase command policy,
and a broken cosmetic setting should not prevent a daemon starting. Security-sensitive
readers log their fallback direction explicitly.

The file sits outside agent-writable paths. Otherwise an agent could pre-authorize its
own host, broker a credentialed CLI, or weaken a readback refusal.
