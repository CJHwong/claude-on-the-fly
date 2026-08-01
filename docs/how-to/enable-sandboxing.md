# Enable sandboxing

Use `env` on every platform to keep credentials out of the agent process. Use `jail`
on macOS to add Seatbelt filesystem and network restrictions.

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

On macOS:

```yaml
sandbox:
  mode: jail
  fs: deny-most
  extra_paths:
    - /opt/homebrew
  broker_only_loopback: true
```

`deny-most` hides most of `$HOME`. Add the directories containing the agent binary,
interpreters, and package managers. At most three extra paths are accepted.

## 3. Verify

Run doctor and inspect startup logs for the selected mode, broker endpoints, curated
environment, and denial probes. A missing `sandbox-exec` degrades `jail` to environment
curation and logs a warning.

Changing `sandbox.mode` requires another restart. The other sandbox fields apply to
the next spawned turn.
