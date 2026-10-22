# Check what a jail would break before you switch

A deployment that ran for months under `sandbox.mode: env` has agents with habits:
files they write outside the workspace, tools they call by absolute path, directories
they read under `$HOME`. The jail refuses some of those. This page shows how to find
them from the transcripts you already have, before the switch.

The audit replays every recorded tool call against the Linux jail's grants and the
`commands:` broker's rules. It runs nothing and changes nothing.

## 1. Run it on the host that will be jailed

Run it as the daemon's user, with the installed interpreter:

```bash
python -m claude_on_the_fly.audit --data-dir ~/.claude-on-the-fly > audit.md
```

It reads `~/.claude/projects` by default. Pass transcript files or directories after
the options to narrow it. It always judges the jail, whatever `sandbox.mode` the config
names, and it reads `sandbox.write_paths`, `sandbox.extra_paths` and `commands:` from the
config in that data dir.

To test a candidate grant without editing the config, set its environment variable for
the run. The environment wins over the file:

```bash
COTF_SANDBOX_WRITE_PATHS=~/notes python -m claude_on_the_fly.audit --data-dir ~/.claude-on-the-fly
```

A host with a few thousand transcripts takes several minutes.

## 2. Read the report

The report has four tables.

- **Brokered tools.** One row per `commands:` tool. `Bare` counts calls by bare name,
  which reach the shim. `Path` and `Variable` count calls by an absolute path or through
  a shell variable. Those run the real binary inside the jail, where it has no
  credential and fails. `Refused` counts calls the broker would refuse.
- **Refusals.** Each broker refusal with its reason and the subcommand, cut at the depth
  the allowlist names so no argument value appears.
- **Blocked writes.** Writes outside every write grant, grouped by the top two path
  segments. Paths resolve through links first, so a link into a granted tree counts as
  granted.
- **Blocked reads.** Reads under the hidden `$HOME` that no grant covers.

## 3. Act on each row

| Row | Action |
|---|---|
| A tool with a high `Path` or `Variable` count | Tell the agents to call it by bare name, in their instructions or skills |
| A refusal the agents need | Widen the tool's `allow` list, or accept the refusal |
| A blocked write the agents need | Add the narrowest tree to `sandbox.write_paths` |
| A blocked read the agents need | Add the narrowest tree to `sandbox.extra_paths` |
| A blocked write under `workspaces` | Leave it. A conversation writing into another's workspace is what the jail is meant to stop |

Run the audit again after each change. A row that stays after you granted its tree
usually names a path the grant refused; the daemon logs that refusal at ERROR.

The audit reads what the agents did, not what they will do. After the switch, watch
the first days of logs for denials the history did not contain.
