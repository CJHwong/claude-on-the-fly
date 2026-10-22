# Select accounts by channel or user

Use existing Claude and Codex login profiles. Keep credentials in those profiles.
The YAML file contains profile paths and account aliases only.

1. Add account definitions and routing rules to `~/.claude-on-the-fly/config.yaml`.

   ```yaml
   accounts:
     claude_default:
       backend: claude
       home: null
     claude_local:
       backend: claude
       home: ~/.claude-local
       mode: ollama
       model: qwen3:30b
     codex_default:
       backend: codex
       home: null
     codex_avery:
       backend: codex
       home: ~/.codex-avery
     codex_bela:
       backend: codex
       home: ~/.codex-bela

   routing:
     defaults:
       claude: claude_default
       codex:
         - codex_default
         - codex_avery
         - codex_bela
     slack:
       channels:
         C0123456789:
           codex:
             - codex_avery
             - codex_bela
       users:
         U0123456789:
           claude: claude_local
     telegram:
       channels:
         "-1001234567890":
           codex:
             - codex_avery
             - codex_bela
       users:
         "123456789":
           claude: claude_default
   ```

2. Replace the example paths and identifiers with your profile paths and platform identifiers.
3. Restart each affected chat daemon and the jobs worker.
4. Start a new Slack thread or send Telegram `/new` to apply the rules.
5. Send a prompt and check the account selection in the daemon log.

A channel rule wins over a user rule. The backend default applies when neither rule matches.
A user rule uses the person who starts the conversation.
The selected account and its fallback list stay with that conversation through replies, resume, and daemon restarts.
Routing edits apply to new conversations. Account definition edits require another restart.

`home: null` leaves Claude's `CLAUDE_CONFIG_DIR` unset and selects `~/.codex` authentication for Codex.
An account's optional `mode` and `model` override the existing backend settings.
The example's local Claude account needs Ollama and the named model.
Routing rules apply only to the active backend; they do not change it.

Jobs use `routing.defaults` for their backend. This version has no per-job account override.
A missing selected account or invalid profile produces an error without account fallback.

## Enable fallback after exhausted credits

A string pins a backend to one account. An ordered list permits fallback only within that list.
The example tries the default Codex account, then Avery, then Bela.
The example Slack channel uses only Avery and Bela.
Replace a list with `codex: codex_avery` to pin that route to Avery.

1. The daemon starts each turn with the saved account.
2. If the provider confirms exhausted credits or quota, the daemon tries the next account.
3. The daemon wraps around the saved list and attempts each account at most once per turn.
4. The daemon saves the successful replacement for later replies and resume.
5. If all accounts are exhausted, the daemon reports the failure.

Fallback does not apply to authentication errors, network errors, temporary rate limits, overload, or unavailable model capacity.
A generic HTTP 429 response does not prove exhausted quota.
The daemon does not poll quota or rotate accounts in the background.

If a turn completes tools before exhaustion, fallback needs a durable native transcript with completed tool results.
The replacement continues from that transcript instead of replaying the original request.
An unknown or incomplete checkpoint stops automatic retry.

The workspace history already spans its account profiles.
Fallback lets the replacement account receive that history, including earlier messages and tool results.
Only group accounts that can receive the same conversation history.

See the [account and routing schema](../reference/config-yaml.md#accounts) for profile and history details.

The dashboard and history screen copy resume commands with the saved account.
Codex commands select the workspace's account home. Claude commands select the account profile.
Ollama commands keep the configured model provider.
If history contains conflicting platform selections, the command fails instead of choosing an account.
