# Troubleshoot configuration

## An edit has no effect

Check the setting lifecycle in the [settings reference](../reference/settings.md).
Restart the affected daemon for startup settings. Worker edits require restarting the
jobs daemon; a chat restart alone is insufficient.

Legacy environment variables win over YAML. Search the daemon environment and startup
log for a warning naming the old variable.

## A key is ignored

Run doctor. Unknown top-level sections are reported, but unknown nested fields are not
universally rejected. Compare the spelling with the [`config.yaml`
reference](../reference/config-yaml.md).

## Sandbox mode breaks model access

Confirm the backend uses an authentication method the credential broker supports and
that the keychain item exists. `env` and `jail` remove token-like environment variables;
without a broker endpoint there is no replacement credential path.

## A brokered command is missing

The executable must be on the daemon's PATH at startup, the tool must be under
`commands.tools`, and the chat daemon must be restarted after changes.

## Approval prompts do not arrive

Slack needs a bot token. Claude pty needs tmux. Confirm `permissions.mode: ask`, restart
the chat daemon, and check that the spawned session receives an approval endpoint.

## The watch pane shows a transcript instead of the live terminal

The dashboard mirrors the agent's own terminal when the run is hosted in a tmux pane, and
falls back to tailing the session transcript when it is not. Install tmux if it is
missing. Native `claude -p` is never hosted, so it always shows the tail. A run whose
`COTF_DATA_DIR` is very deep is not hosted either: the daemon log names the socket length
it would have needed.

## A malformed section changed behavior

Readers normally log an error and use packaged or code defaults for that section. Fix
the first configuration error in the log; later symptoms may only reflect the fallback.
