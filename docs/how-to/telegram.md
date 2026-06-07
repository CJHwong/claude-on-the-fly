# Telegram

## Setup (2 minutes)

1. Open Telegram, search for `@BotFather`
2. Send `/newbot`, pick a name and username (must end in `bot`)
3. Copy the API token
4. Search for `@userinfobot` on Telegram, send any message
5. Copy your numeric user ID

```bash
export TELEGRAM_BOT_TOKEN="your-token-from-step-3"
export TELEGRAM_ALLOWED_USER_ID="your-id-from-step-5"
uv run claude-telegram
```

Send a message to your bot. That's it.

## Commands

- `/new` - Start a fresh session (clears conversation history)
- `/status` - Check if Claude is working or idle

## Supported input

- Text messages
- Files (documents, code files) - saved to workspace, Claude can read them
- Photos/images - saved to workspace, Claude can view them
- Multiple photos - batched into a single prompt

See [Environment Variables](../reference/env-vars.md#telegram) for the full Telegram config.
