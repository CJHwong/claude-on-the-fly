# Broker a credentialed command

A brokered CLI runs outside the sandbox with a narrowly constructed environment. Its
stdout and stderr return to the agent; its credential does not.

```yaml
commands:
  tools:
    - name: gh
      readback:
        - auth token
      readback_flags:
        - --show-token
      allow:
        - pr list
        - pr view
        - repo view
      env_passthrough: [GH_HOST, GH_REPO, NO_COLOR]
```

Before adding a tool:

1. Put the executable on the daemon's PATH.
2. Give its credential the smallest provider-side scope possible.
3. List the exact safe leading subcommands under `allow`. The list is deny-by-default;
   an omitted or empty list makes the shim refuse every invocation. Do not add generic
   API or mutation prefixes unless you have reviewed their full provider-side scope.
4. List commands and flags that print or mutate authentication state.
5. Pass only environment names the real CLI requires.
6. Restart the chat daemon so shims are rebuilt.
7. Test one allowed command, one rejected command, and every readback refusal.

The broker checks the configured leading prefix, rejects absolute or workspace-escaping
path arguments, and runs each turn only from its authenticated workspace. Arguments and
flags after an allowed prefix are still passed to the real CLI, so provider-side token
scope remains the reliable boundary for what the tool can ultimately do.

Operator entries override packaged tools by name. Dropping a packaged readback refusal
is legal but produces a warning. An override that omits `allow` intentionally disables
the packaged tool rather than inheriting its safe command list.
