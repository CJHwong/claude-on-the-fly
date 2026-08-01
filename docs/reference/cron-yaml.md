# `cron.yaml` reference

Path: `~/.claude-on-the-fly/cron.yaml`. The cron daemon reloads it at most one minute
after a valid save. A failed reload keeps the prior entries.

```yaml
entries:
  - name: standup
    cron: "30 6 * * 1-5"
    prompt: "Summarize yesterday."
```

## Entry fields

| Key | Type / default | Constraints |
|---|---|---|
| `name` | string / required | Unique; letters, digits, `_`, and `-` |
| `cron` | string / required | Standard five-field expression |
| `prompt` | non-empty string | Mutually exclusive with `prompt_file` |
| `prompt_file` | path | Re-read every fire; relative to `cron.yaml` |
| `command` | non-empty shell string | Producer when paired with a prompt; side effect otherwise |
| `timeout` | integer / `1800` | `1..86400` seconds |
| `max_concurrent` | integer / `1` | At least 1; above 1 requires a producer command |
| `max_fires` | integer / `3` | Unchanged fires before parking; `0` disables parking |

Every entry needs a prompt, prompt file, or command. A producer command writes one JSON
object per line with a non-empty `key`. Prompt text is strict Liquid; producer fields
are available under `item`.

The old root key `jobs` is accepted and migrated to `entries`. Legacy `script` plus
`args` is accepted and translated to `command`.

See [Run scheduled work](../how-to/cron.md) for examples and operating guidance.
