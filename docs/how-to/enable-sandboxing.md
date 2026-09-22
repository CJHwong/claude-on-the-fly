# Enable sandboxing

Use `env` on every platform to keep credentials out of the agent process. Use `jail`
to add filesystem and network restrictions: Seatbelt on macOS, bubblewrap plus a
network namespace on Linux.

## 1. Provision provider credentials

The credential broker reads supported API keys from the host keychain. For Claude:

```bash
security add-generic-password -a "$USER" -s cotf-anthropic -w "<key>" -U
```

OAuth credentials cannot always be injected by the broker; check the
[authentication boundaries](../explanation/security-model.md#authentication-boundaries).

## 2. Select a mode

```yaml
sandbox:
  mode: env
```

Restart the chat daemon. Confirm a model turn works before tightening further.

To add the jail:

```yaml
sandbox:
  mode: jail
  fs: deny-most
  extra_paths:
    - /opt/homebrew
  broker_only_loopback: true
```

`deny-most` hides most of `$HOME`. The agent binary is granted automatically, along
with the interpreter behind it, the `lib/` beside a `bin/` install, and the binaries a
wrapper execs. If an entry in your codex home is a symlink pointing elsewhere under
`$HOME` -- one set of agents or skills shared between backends -- the target is granted
too, since the grant on the home itself covers the link and not the file behind it. Add
anything else the agent needs to read.

If the agent works with git, add `~/.gitconfig` as well: git refuses to run when it cannot read its global config, and `deny-most` hides that file with the rest of the home.

If you run claude with hooks, add the directory holding the hook scripts. A hook that
cannot be read does not stop the turn: claude answers, then the hook fails, and in pty
mode the failing hook is the one that writes the turn's envelope. The error names the
script rather than the sandbox, so it is easy to misread.

A toolchain shim works, and logs one line that looks worse than it is. `mise` reads its
own config, which a grant covers, and then tries to write a tracking symlink under
`~/.local/state/mise`, which no `extra_paths` entry can permit: those grants are
read-only. It warns (`tracking config: failed to ln -sf`) and runs the tool anyway.
Grant `~/.config/mise` if you want the config read to succeed as well, and
`~/.local/state/mise` under `write_paths` if you want the warning to stop.

## Let the agent write outside its workspace

`extra_paths` grants reads. `write_paths` is the separate setting for writes, and an
entry there is readable as well:

```yaml
sandbox:
  write_paths:
    - ~/notes
```

Use it when the agent's real job lives outside the workspace -- a notes tree, a
generated site, a scratch area a later turn reads back.

Grant the narrowest tree that works. This is the setting that decides what a turn can
still change after it ends, so a broad entry gives back most of what the jail is for.

`write_paths` refuses everything `extra_paths` refuses, and then refuses more. A write
grant can leave behind something that runs later, *outside* the jail:

- a shell rc the next login sources
- a git config, where `core.hooksPath` and a `!`-prefixed alias are both executables
- a systemd unit, an autostart entry or a LaunchAgent that init starts
- a directory on the daemon's PATH, where a name collision is enough
- a hook under `~/.claude`, `~/.codex` or `~/.agents`

An entry reaching any of those is logged at ERROR and dropped, and the other entries
still apply.

Treat that as a guard against the common mistakes, not as a boundary. The PATH
directories are read from the daemon's own environment, so a directory that is only on
your interactive PATH is not covered. Granting the narrowest tree that works is what
actually keeps the jail meaningful.

A `.env` file is never readable, under either `sandbox.fs` value and at any depth. One
rule at the end of each profile covers every tree the jail grants, so a grant you add
here cannot re-open a token file sitting beside the files you wanted. This is why a
credentialed CLI fails on its own config inside the jail, and why a read grant is the
wrong remedy for it: granting the path would hand the token to the session. Put the tool
in the `commands:` section instead, so it runs outside the sandbox with your credentials
and the agent only ever sees its output. The shim matches on PATH, so the agent has to
invoke the tool by bare name -- an absolute path runs the real binary inside the jail,
where it starts but finds no credential.

Name each directory, never `$HOME` itself. A grant is written after the profile's
denies and wins over them, so `$HOME` gives the agent back `~/.ssh` and `~/.aws` in one
line. The daemon refuses such an entry: the home directory, any ancestor of it, and any
path that reaches a credential store are logged at ERROR and dropped, and the remaining
entries are still granted. If the agent then reports a blocked read, grant the narrowest
directory that unblocks it.

To also stop a turn reading other conversations' transcripts, add `scope_sessions: true`.
The boundary is the conversation's directory, so the threads of one DM, group DM or
channel still see each other's. It moves the session stores, so the first turn of each
existing chat thread has to find its history in the new place. A codex thread carries its rollout across and keeps
its memory. A claude thread keeps its session where it already is, and a rollout that
no longer exists anywhere starts the thread again rather than failing the turn.

On Linux, install bubblewrap first (`apt install bubblewrap`, or your distribution's
equivalent), and note two differences the daemon logs at startup:

- `fs` has no effect. A mount namespace cannot express "readable except for these
  files", so `deny-most` is the only available shape and `allow-reads` resolves to it.
- `broker_only_loopback` has no effect either. The namespace contains only the
  brokered services, so there is never a wider set of host ports to narrow.

Linux is at or above the macOS posture under either value of either setting.

Unprivileged user namespaces must be enabled. Ubuntu 23.10 and later restrict them
by default via AppArmor, and the failure does not look like the cause: bubblewrap
still creates the namespace and is then refused netlink bringing up loopback
inside it, so the error mentions `Failed RTM_NEWADDR` rather than namespaces.

```bash
sysctl kernel.apparmor_restrict_unprivileged_userns          # 1 means restricted
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

Persist it in `/etc/sysctl.d/` if you want it across reboots. Startup preflight
detects this specific case and prints the same remedy. The daemon refuses to start
with `jail` when the mechanism is unusable rather than running a turn unsandboxed.

## 2b. Choose an egress posture

`sandbox.egress` decides what happens to the agent's outbound HTTPS. It is a
separate axis from `sandbox.mode`, which decides whether the agent holds a
credential.

| Value | Proxy | Host outside `egress.allow` |
|---|---|---|
| `gated` (default) | runs | asks you |
| `open` | runs | tunnels without asking |
| `off` | not started | no outbound HTTPS at all |

Keep `gated` unless the agent's job is reading the web. `egress.allow` matches
host names exactly, with no wildcard and no suffix match, so an agent that follows
news links or search results reaches a new host most turns. Each one pauses the
call to ask you, and the approval rate limit then starts auto-denying.

```yaml
sandbox:
  mode: jail
  egress: open
```

`open` drops one question, not the protections. A malformed host name, a
`never_ask` metadata endpoint, and an address that resolves private or loopback
without `egress.private_allow` are all refused exactly as under `gated`. The
proxy still resolves and pins the address before connecting.

What it gives up is inspection. The tunnel is not read (no TLS interception), so
a host reached this way is a covert channel. Pair it with `mode: jail` and that
is the intended trade: the filesystem denies decide what the agent can read, and
this decides who it can talk to.

Prefer `open` over dropping the network jail. Both let the agent reach the web,
but `open` keeps the namespace free of the host's own loopback services, which on
Linux is the property that makes `broker_only_loopback` unnecessary.

`off` is a lockdown rather than a mistake, including under `jail`. The relay still
bridges whatever brokered loopback ports the turn has, so model calls keep working
through the credential broker and the agent has no internet.

Quote the value if you write `off`. YAML reads a bare `off` as a boolean; the
setting accepts that spelling too, but the quoted one is what it says.

## 3. Verify

Run doctor and inspect startup logs for the selected mode, broker endpoints, curated
environment, preflight, and denial probes.

`jail` no longer degrades. If the mechanism is missing or unusable the daemon refuses
to serve, because a jail that was configured and silently did not apply is worse than
one that was never requested. Startup also proves two things before accepting work:
that the jail runs a trivial command, and that a jailed process cannot reach the
internet directly.

Changing `sandbox.mode` or `sandbox.egress` requires another restart. The other
sandbox fields apply to the next spawned turn.
