# Migrate old thread directories

Since this release a workspace is one directory per conversation: a Slack DM, group DM or channel, or a Telegram chat. Every thread or `/new` session in it runs there. Earlier releases gave each thread its own directory. Those directories still hold the thread's session files, and the daemon folds each one into its conversation's directory the first time that thread gets a message.

A thread that never gets another message never migrates on its own. This command folds all of them at once.

## 1. Stop the daemon

The command moves session files a running turn could be writing.

## 2. Back up the stores you are about to move

```bash
STAMP=$(date +%Y%m%d)
mkdir -p ~/cotf-backup-$STAMP
cp -R ~/.claude-on-the-fly/workspaces ~/cotf-backup-$STAMP/
cp -R ~/.claude-on-the-fly/codex-sessions ~/cotf-backup-$STAMP/
cp -R ~/.claude/projects ~/cotf-backup-$STAMP/
```

Add `~/.claude-on-the-fly/codex-homes` when `sandbox.scope_sessions` is on. Nothing is deleted by the migration and nothing is overwritten, so the backup only matters if you roll the release back afterwards.

## 3. Run the dry run

```bash
claude-slack --migrate-workspaces
claude-telegram --migrate-workspaces
```

Slack reads `SLACK_TOKEN` from `.env` in the data directory, or from the shell, because the old names hold display names and the new names need Slack ids. One line prints per directory:

```
dm-hoss-1786342813-662689 -> slack/dm/U01ABCDEF/threads/1786342813-662689  sessions=1
old-channel-1786342813  SKIP: conversation 'old-channel' not on Slack
smoke  SKIP: not a thread directory
141 directories to move (139 sessions), 2 skipped
```

A skipped directory is left exactly where it is. A handle or channel Slack no longer knows is not guessed; if the thread ever gets a message, the daemon folds it then.

## 4. Apply

```bash
claude-slack --migrate-workspaces --apply
claude-telegram --migrate-workspaces --apply
```

Each session's claude transcript moves under the new directory's hash, each codex mapping is rewritten for the new path, and the thread's files move to `threads/<thread key>/` in the new directory. The old directory is removed once empty.

## 5. Start the daemon and check one thread

Post in one migrated thread. The reply should show it remembers the conversation. Delete the backup when you are satisfied.
