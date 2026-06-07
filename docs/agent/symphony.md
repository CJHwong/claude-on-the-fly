# Symphony

Long-running daemon that polls one or more trackers (Jira, GitHub PRs), claims tickets, and runs Claude Code in per-ticket sessions. Inspired by [openai/symphony](https://github.com/openai/symphony), extended for multi-source dispatch.

Entry point: `src/claude_on_the_fly/symphony/cli.py`. Main loop: `symphony/orchestrator.py`. Watch tail: `symphony/watch.py`. Cursor (per-tick progress): `symphony/cursor.py`.

## Adding a new tracker

The `Tracker` Protocol lives at `src/claude_on_the_fly/symphony/tracker/base.py:18` — it's the read-only contract. A new source (Linear, GitHub Issues, etc.) means:

1. Implement the Protocol in a new file under `symphony/tracker/` (see `jira.py` and `github.py` for reference).
2. Register the class in `SUPPORTED_TRACKERS` in `src/claude_on_the_fly/symphony/tracker/__init__.py`.
3. Add a `kind: <name>` stanza under `trackers:` in `symphony.yaml` — no orchestrator changes.

The Protocol's docstring (`tracker/base.py:18`) is the contract. Pay attention to the `extra` field on `IssueSummary` — it's how adapters carry source-specific state (Jira: labels; GitHub: review-requested status) into `is_terminal`/`is_active`. Don't omit keys from `fetch_summaries_by_keys` returns: the orchestrator treats a missing key as "transient failure, skip", not "cancel".

The orchestrator never writes through the Tracker Protocol. Status transitions, comments, and label edits are agent-side — done with whatever tools the agent has (acli, gh, REST).

## Adding a new field to the YAML config

`src/claude_on_the_fly/symphony/config.py` is the schema. New fields go on `TrackerCommonConfig` (or a subclass) and become available to adapters via `cfg.<field>`. Mtime hot reload picks up edits on the next tick, but **restart the daemon** for schema changes that affect already-claimed tickets.

## Loop semantics

`orchestrator.py` runs the same `poll → reconcile → dispatch` cycle every `polling_ms`. Reconciliation (`is_terminal` / `is_active` predicates) runs every tick and catches mid-turn state transitions — e.g. Jira ticket moved to Done while agent is working, or `@me` removed from a PR's reviewRequests. On transition, the worker is cancelled and the workspace removed.

Failures (`ClaudeUnavailableError` from `src/claude_on_the_fly/agent.py:232`, exceptions, stalls) go through `symphony/retry.py` with exponential backoff. `max_turns` exhaustion gets a 1s continuation retry.

## Per-ticket workspace

Created at `~/.claude-on-the-fly/workspaces/symphony/<source>/<KEY>/` by `symphony/workspace.py`. The global persona `~/.claude-on-the-fly/CLAUDE.md` is symlinked in via `ensure_persona()`. Spawned with `claude --resume <deterministic-uuid>`. Loops turns until terminal, leaves active states, or hits `max_turns`.

**The daemon does NOT run git.** The agent owns worktrees and branch hygiene. For Jira, the agent `git worktree add`s alongside source-repo clones declared in the prompt, NOT inside the scratch dir. For GitHub PR review, the agent uses `gh` directly without cloning.

**The agent owns all tracker writes** — Jira transitions via `acli`, comments via direct ADF POST to REST; GitHub reviews via `gh pr review` and `gh api`.

## Stop signals (escalating force)

- **Jira**: remove the gate label → transition to terminal state → SIGINT.
- **GitHub**: submit any review → close the PR → SIGINT.
