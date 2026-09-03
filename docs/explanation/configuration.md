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

## Two ways `.env` reaches a process, with opposite precedence

A daemon started from a shell reads the file itself at startup, and an existing
environment variable wins. A daemon the TUI supervises never calls that: the supervisor
merges the file into the child's environment at spawn, and there the file wins. Both are
right for who is asking. A frontend loading its own configuration is accepting that the
shell may override it; the spawn path is deciding what the child gets.

The consequence is that "what is in the environment" is not one question. A process that
resolves a *path* from a variable an operator set in `.env` is really asking where some
other process wrote, and only the spawn path's answer is that. Reading its own
environment instead means reading its own configuration and calling it the daemon's.

That distinction had teeth. The session-log directory was derived from `CLAUDE_CONFIG_DIR`
at import time, so a deployment setting it in `.env` had the daemon writing transcripts
under one directory while the dashboard looked under another, and the live view reported
that the agent had not run a turn over a session that was streaming. The symptom was
indistinguishable from nothing having happened.

So the merge is one reader (`envfile`) rather than a habit each module repeats. Anything
resolving a path from an operator-set variable goes through it and gets the daemon's
answer. A checker handed an environment mapping must use that mapping, too: a function
that accepts one and then consults its own environment is a signature no caller can
work around.

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
