# Credential broker + seatbelt jail

Keeps API keys out of the agent. A bypassPermissions agent that gets prompt-injected
will exfiltrate any secret it can reach, so the defense is architectural: the key
value never enters the agent process, and a local broker injects it at the point of use.

Two layers, both opt-in via `COTF_SANDBOX` (default `off`):

| mode   | env curation | seatbelt jail | platform |
|--------|--------------|---------------|----------|
| `off`  | no (inherit full env) | no | all (no change) |
| `env`  | yes (allowlist only) | no | all |
| `jail` | yes | vendored profile (egress→loopback, keychain denied) | macOS |

## How it fits together

1. `broker.start_default_broker()` reads the real key from the keychain, starts a
   loopback reverse proxy, and publishes `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>/anthropic`
   into the daemon env.
2. `sandbox.agent_env()` forwards only an allowlist to the spawned agent (essentials +
   `*_BASE_URL` + proxy vars). Every `*_API_KEY` / `*_TOKEN` is dropped by omission.
3. The agent's SDK sends plain HTTP to the base-url. The broker strips any caller-supplied
   auth, injects the real key on the broker→upstream leg, and never re-injects on redirect.
4. In `jail` mode the agent is also wrapped in `sb -j`: external egress is denied (loopback
   allowed, so the broker and local dev servers work), and keychain reads are denied.

## Provisioning credentials

The broker reads generic-password keychain items named `cotf-<provider>`:

```bash
security add-generic-password -a "$USER" -s cotf-anthropic -w "<your key>" -U
```

A route activates only if its item exists (`routes_from_keychain`), so the broker starts
with whatever is provisioned. Add providers by extending `DEFAULT_ROUTES` with a `Route`
(prefix, upstream, header, keychain_service, base_url_env_var).

## Seatbelt (jail mode)

Nothing to install. The profiles live in `src/claude_on_the_fly/seatbelt/`
(`my.sb` base + `my.jail.sb`, vendored from agent-seatbelt). `sandbox.wrap`
invokes `sandbox-exec` against them directly; the jail imports the base and
overrides only egress + keychain. macOS only; on other platforms jail mode
degrades to `env` (curation without the seatbelt). Re-vendor by copying the two
`.sb` files from agent-seatbelt.

## Scope and caveats

- **Wired into the chat orchestrator** (`orchestrator.run`: telegram/slack/gmail). Symphony
  and scheduler share the spawn-site env curation but do **not** auto-start the broker. Do
  not set `COTF_SANDBOX` for them until the broker is started in their run loops too,
  or curation removes the key with no base-url to replace it and breaks LLM auth.
- **Auth model:** works for backends that authenticate with an injectable key header
  (codex/pi/openrouter, and claude when using `ANTHROPIC_API_KEY`). Claude via OAuth
  (claude.ai login) does not use a header key, so the broker does not cover it.
- **Residual risk:** a hijacked agent cannot steal a key it never holds, but it can still
  drive the allowlisted broker routes as a confused deputy (e.g. smuggle data inside an
  allowlisted API call). The allowlist is the security knob; keep routes minimal.
