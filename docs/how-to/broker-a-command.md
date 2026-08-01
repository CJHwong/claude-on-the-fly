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
      env_passthrough: [GH_HOST, GH_REPO, NO_COLOR]
```

Before adding a tool:

1. Put the executable on the daemon's PATH.
2. Give its credential the smallest provider-side scope possible.
3. List commands and flags that print or mutate authentication state.
4. Pass only environment names the real CLI requires.
5. Restart the chat daemon so shims are rebuilt.
6. Test one normal command and every readback refusal.

The broker does not authorize general command semantics. Subcommand deny lists are
easy to bypass through generic API commands; token scope is the reliable boundary.

Operator entries override packaged tools by name. Dropping a packaged readback refusal
is legal but produces a warning.
