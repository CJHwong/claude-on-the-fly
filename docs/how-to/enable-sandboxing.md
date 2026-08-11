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

`deny-most` hides most of `$HOME`. Add the directories containing interpreters and
package managers. The directory holding the agent binary is granted automatically.

On Linux, install bubblewrap first (`apt install bubblewrap`, or your distribution's
equivalent), and note two differences the daemon logs at startup:

- `fs` has no effect. A mount namespace cannot express "readable except for these
  files", so `deny-most` is the only available shape and `allow-reads` resolves to it.
- `broker_only_loopback` has no effect either. The namespace contains only the
  brokered services, so there is never a wider set of host ports to narrow.

Linux is at or above the macOS posture under either value of either setting.

Unprivileged user namespaces must be enabled. Some distributions restrict them by
default (Ubuntu 23.10 and later do, via AppArmor); the daemon refuses to start with
`jail` when they are unusable rather than running a turn unsandboxed.

## 3. Verify

Run doctor and inspect startup logs for the selected mode, broker endpoints, curated
environment, preflight, and denial probes.

`jail` no longer degrades. If the mechanism is missing or unusable the daemon refuses
to serve, because a jail that was configured and silently did not apply is worse than
one that was never requested. Startup also proves two things before accepting work:
that the jail runs a trivial command, and that a jailed process cannot reach the
internet directly.

Changing `sandbox.mode` requires another restart. The other sandbox fields apply to
the next spawned turn.
