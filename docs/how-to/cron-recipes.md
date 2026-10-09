# Cron recipes

Each recipe is a pattern a real deployment runs. A short recipe is an entry on this page. A longer one is a directory under `docs/examples/`, holding its `cron.yaml` entry, its script, and a `SKILL.md` when the agent needs instructions. Copy the entry, then change the paths, the skill names and the times. [Cron](cron.md) explains producers, keys and alerts; read it first.

Most recipes rely on two cron daemon rules:

- A bare `command` that exits non-zero, and a producer that exits non-zero, alert `slack.alert_target` or `telegram.alert_target`. At most one alert goes out per entry per 30 minutes. A script can therefore raise an alert by printing the reason and exiting 1, with no Slack code.
- A producer that prints nothing starts no agent. A poll that finds no work costs one shell command, not a model session.

Each script in an example directory opens with how to set it up. The scripts run with `uv run --script`, and a skill is linked into the agent's skills directory, so clone the repository once:

```bash
git clone https://github.com/CJHwong/claude-on-the-fly ~/claude-on-the-fly-src
```

The cron daemon runs commands with its own `PATH`. If `uv` or `gh` is not on it, write the absolute path.

## Commit the agent's own repo every night

The agent edits its memory, skills and notes during the day. A producer starts a commit run only when the repo is dirty. A plain command pushes later, so the agent never holds push rights in an unattended run.

```yaml
- name: self-commit
  cron: "30 23 * * *"
  command: |
    repo=~/my-agent-soul
    dirty="$(git -C "$repo" status --porcelain)" || exit 1
    [ -z "$dirty" ] || printf '{"key":"self-commit","change":"%s"}\n' \
      "$(printf '%s\n%s' "$(git -C "$repo" rev-parse HEAD)" "$dirty" | git hash-object --stdin | cut -c1-12)"
  prompt: >-
    Work in ~/my-agent-soul. Commit every uncommitted change there in logical commits.
    No one reviews this run: treat your commit plan as approved. Do not push.

- name: self-push
  cron: "0 0 * * *"
  command: git -C ~/my-agent-soul push origin HEAD
```

If `git status` fails, for example because the path is wrong, the producer exits 1 and alerts instead of finding nothing forever. The key is fixed, so every run resumes one session. `change` hashes `HEAD` and the dirty file list. It moves whenever new work appears. If the same dirty set survives three nights, the key parks (`max_fires` defaults to 3), and a person should look.

## Wake the agent only when there is work

A watcher that runs every 15 minutes as a plain prompt starts a model session every time, even when nothing happened. Make the poll a producer instead. This one hands each failed CI run to the agent once:

```yaml
- name: ci-failures
  cron: "*/15 * * * *"
  max_fires: 1              # one attempt per failed run
  max_concurrent: 2
  command: >-
    gh run list --repo OWNER/REPO --status failure --limit 20
    --json databaseId,workflowName,headBranch
    | jq -c '.[] | {key: "run-\(.databaseId)", workflow: .workflowName, branch: .headBranch}'
  prompt: |
    CI run {{ item.key }} ({{ item.workflow }} on {{ item.branch }}) failed.
    Read its log with gh, find the cause, and post a short diagnosis to the team channel.
```

A failed run stays failed, so the producer prints it on every poll. `max_fires: 1` parks the key after its first run, so each failure is handled once. A failed attempt is not retried until the item's fields change.

Print only the work list on stdout. Write errors to stderr: every stdout line must be a JSON object.

## Hand new mail to a person

**Warning:** mail text reaches the agent's prompt. Allow only senders you trust. The skill tells the agent to treat the mail as data.

Example: [`docs/examples/mail-handoff/`](../examples/mail-handoff/).

`scripts/mail_poll.py` prints one item per new Gmail message from the allowed senders, and marks it read. The `mail-handoff` skill reads the message, sends the right person a short summary in a Slack DM, and asks how to handle it. Nothing else happens to the mail until that person answers.

1. Set up `gws` and seed the poll, as the script's header says. Without the seed, every message that is already unread arrives on the first poll.
2. Link `docs/examples/mail-handoff` into the agent's skills directory.
3. Copy `cron.yaml` into your cron file, with your senders and a fallback Slack user.

The person answers in the DM thread. If that DM is a conversation the chat frontend already answers in, the reply arrives as an ordinary chat turn. The frontend adds the earlier messages of the thread as context, so the agent sees its summary and the Gmail id on the summary's last line. The skill's second phase covers that turn. The cron session itself is not resumed.

Google expires the login of an OAuth app in "Testing" status after 7 days. Publish the app, or schedule a check that alerts when `gws` stops answering.

## Run every other week

Cron has no "every 14 days". Fire weekly, and let the producer print an item only on the right weeks:

```yaml
- name: sprint-review
  cron: "0 12 * * 1"        # every Monday at noon
  max_fires: 1
  command: >-
    python3 -c 'import datetime as d, json, sys;
    today = d.date.today(); days = (today - d.date.fromisoformat(sys.argv[1])).days;
    days >= 0 and days % 14 == 0 and print(json.dumps({"key": f"review-{today}"}))'
    2026-09-21
  prompt: Draft the sprint review for the two weeks ending today. Post it as a preview, not to the team.
```

The argument is the first Monday of the cadence. Each due week prints a new key, so each review is a fresh session.

## Alert when a scheduled post did not happen

A job can finish without posting: the agent hit a limit, or replied instead of acting. Have the posting job leave a marker, and check for the marker after its deadline:

```yaml
- name: standup
  cron: "0 9 * * 1-5"
  prompt: >-
    Post the daily standup to the team channel. After the post succeeds, run
    `touch ~/.claude-on-the-fly/state/standup/$(date +%F).posted`.

- name: standup-delivered
  cron: "30 9 * * 1-5"
  command: >-
    mkdir -p ~/.claude-on-the-fly/state/standup &&
    test -f ~/.claude-on-the-fly/state/standup/$(date +%F).posted ||
    { echo "standup not posted by 09:30"; exit 1; }
```

Silence means the post happened. For a run that skips its work entirely, see `min_tool_calls` in the [`cron.yaml` reference](../reference/cron-yaml.md).

## Alert when the cron file stops loading

When an edit breaks `cron.yaml`, the cron daemon logs the error and keeps the entries it loaded before. A new entry then never fires, and nothing says so. This check loads the file with cotf's own parser:

```yaml
- name: cron-config-check
  cron: "43 * * * *"
  timeout: 60
  command: >-
    "$(uv tool dir)/claude-on-the-fly/bin/python" -c
    'from claude_on_the_fly.cron import load_config, resolve_config_path;
    path = resolve_config_path(); print(path, len(load_config(path)), "entries")'
```

The check itself keeps running after a bad edit, because the daemon keeps the last good entries.

## Watch CPU and memory without an agent

Example: [`docs/examples/resource-watch/`](../examples/resource-watch/).

`resource_watch.py` exits 1 when CPU or memory stays high. It waits out short spikes, repeats an alert at most once per reminder interval, and clears only after usage drops below a lower threshold. The state lives in `~/.claude-on-the-fly/state/resource-watch.json`.

The cron daemon alerts only on failure, so an alert that clears is silent by default. To hear about it, give the script a sender. It runs the command once, with the text in `$RESOURCE_WATCH_MESSAGE`:

```bash
--recovery-command 'slacker.sh send @U0123456789 "$RESOURCE_WATCH_MESSAGE"'
```

## Sweep finished clones from workspaces

Example: [`docs/examples/sweep-clones/`](../examples/sweep-clones/).

Agents clone repositories into their conversation workspaces, and cotf never removes them. `sweep_workspace_clones.py` removes a clone only when it is older than `--days`, clean, fully pushed, and not lending its objects to another clone. It moves the clone to the trash when `trash`, `trash-put` or `gio` is installed.

Run it once without `--apply` first. It prints what it would remove and why it keeps the rest.

## Review the agent's skills every week

Example: [`docs/examples/skill-reflect/`](../examples/skill-reflect/).

The `skill-reflect` skill reads the week's transcripts and proposes skill changes to one approver. See [Review skill proposals from past sessions](reflect-on-skills.md).
