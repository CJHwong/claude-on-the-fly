# Symphony

Long-running daemon that polls one or more trackers (Jira, GitHub PRs), claims tickets, and runs Claude Code in per-ticket sessions until each ticket leaves an active state. Inspired by [openai/symphony](https://github.com/openai/symphony), extended for multi-source dispatch.

## Two config files

**`~/.claude-on-the-fly/symphony.yaml`** — daemon settings. Multi-tracker shape (each source has its own active/terminal states, gate label, and prompt):

```yaml
trackers:
  jira:
    kind: jira
    base_url: https://your-org.atlassian.net
    email: $JIRA_EMAIL
    api_token: $JIRA_API_TOKEN
    project_key: PROJ
    jql_extra: 'AND assignee = currentUser() AND labels = "stevedore"'
    gate_label: stevedore
    prompt: ./symphony-prompt-jira.md

  github:
    kind: github                  # PRs requesting your review (gh CLI auth)
    prompt: ./symphony-prompt-github.md
    # No gate label: submitting any review removes you from reviewRequests,
    # which is symphony's "done" signal.

# Defaults shown; uncomment to override:
# polling_ms: 30000
# max_concurrent: 1               # global cap across ALL trackers
# max_turns: 20                   # -1 = unlimited (rely on stall_timeout_ms)
# stall_timeout_ms: 1800000
```

The legacy singular `tracker:` form (with top-level `prompt:`, `gate_label:`, `max_concurrent_by_state:`) is still accepted and auto-wrapped into a single-entry trackers map. Existing configs keep working unchanged.

For adding a new tracker (Linear, GitHub Issues, etc.), see [CLAUDE.md](../../CLAUDE.md#symphony-adding-a-new-tracker).

**`~/.claude-on-the-fly/symphony-prompt-{source}.md`** — Liquid-templated instructions the agent follows per ticket. Variables available: `issue.identifier`, `issue.title`, `issue.state`, `issue.url`, `issue.labels`, `issue.description_json` (Jira), `issue.body_text` (GitHub), `attempt`, `workspace_path`, `gate_label`. See `symphony-prompt-jira.md.example` and `symphony-prompt-github.md.example` in this directory.

Edits to either file are picked up on the next tick (mtime hot reload). Restart the daemon to apply schema changes that affect already-claimed tickets.

## Run

```bash
export JIRA_EMAIL="you@your-org.com"
export JIRA_API_TOKEN="..."
uv run claude-symphony
# or: uv run claude-symphony /path/to/symphony.yaml
```

See [Environment Variables](../reference/env-vars.md#symphony) for required Jira vars.

## How it works

- Polls every configured tracker each `polling_ms`. Jira: JQL `project = PROJ AND status in <active_states> {jql_extra}`. GitHub: `gh search prs --review-requested=@me --state=open --draft=false`.
- Gating: Jira uses a label (default `stevedore`) that the agent removes when done. GitHub uses review assignment — submitting any review removes you from `reviewRequests` automatically.
- Per ticket: creates a per-source scratch dir at `~/.claude-on-the-fly/workspaces/symphony/<source>/<KEY>/` (with the global `CLAUDE.md` persona symlinked in), spawns Claude Code with `--resume <deterministic-uuid>`, loops turns until the ticket reaches a terminal state, leaves active states, or hits `max_turns`.
- The agent works on existing source-repo clones declared in your prompt — it `git worktree add`s alongside those clones, NOT inside the scratch dir. GitHub PR review uses `gh` directly without cloning. Edit the repo list in `symphony-prompt-jira.md` to match your machine.
