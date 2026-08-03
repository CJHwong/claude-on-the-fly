# Persona (CLAUDE.md)

Customize Claude's identity and behavior by placing a `CLAUDE.md` at the data root:

```bash
~/.claude-on-the-fly/CLAUDE.md
```

This file is automatically symlinked into every workspace. Claude Code loads it as project instructions. Use it for:

- Bot identity and tone (e.g., "You are Avery, an AI assistant for the EPD team")
- Team directory references
- Custom behavioral rules
- Channel-specific guidelines

The symlink is re-created on every message, so even if removed mid-session it self-heals.

If no `CLAUDE.md` exists, Claude runs with the default system prompt only.

For codex backend compatibility, the file is also symlinked as `AGENTS.md` in each workspace.

These persona links are intentional. The sandbox's symlink rejection applies to
untrusted attachment/outbox handoffs and daemon state; it does not reject the
workspace links that COTF creates for Claude Code and Codex to load their instructions.
