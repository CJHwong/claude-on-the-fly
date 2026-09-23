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
      allow:
        - pr list
        - pr view
        - repo view
      env_passthrough: [GH_HOST, GH_REPO, NO_COLOR]
```

Before adding a tool:

1. Put the executable on the daemon's PATH.
2. Give its credential the smallest provider-side scope possible.
3. List the exact safe leading subcommands under `allow`. The list is deny-by-default;
   an omitted or empty list makes the shim refuse every invocation. Do not add generic
   API or mutation prefixes unless you have reviewed their full provider-side scope.
4. List commands and flags that print or mutate authentication state.
5. Pass only environment names the real CLI requires.
6. Restart the chat daemon so shims are rebuilt.
7. Test one allowed command, one rejected command, and every readback refusal.

## Admit a REST subcommand for reads only

Some CLIs put an entire API behind one word. `gh api` fetches a file and also rewrites
repository settings, so `allow` is all-or-nothing for it. List such a prefix under
`allow_read_only` instead, and the broker admits the reads and refuses the writes:

```yaml
      allow_read_only:
        - api
```

An explicit `--method` or `-X` decides. Without one, a parameter flag counts as a write,
because gh switches to POST as soon as a parameter is added. That is `-f`/`--raw-field`,
`-F`/`--field` and `--input`, which is gh's whole list of body-supplying flags, taken
from its own `--help`. Each is read in every spelling gh's parser accepts, including the
attached short form: `-XPOST` and `-fname=x` are a method and a parameter just as much as
`-X POST` and `-f name=x`. Send parameters on a read with `--method GET`, which gh turns
into a query string.

`gh api graphql` with no parameters is a GET and is admitted; measured, not assumed. A
GraphQL mutation needs its query passed in, and every way of passing one is a parameter
flag, so the gate refuses it on that path rather than on the subcommand name.

A refused write says so, rather than reporting a missing allowlist entry. That matters:
the generic wording tells the agent to ask you for a prefix you have already configured.

Omit the key to leave a tool exactly as it was. The gate is opt-in per tool, and a
prefix on both lists is allowed outright.

The broker checks the configured leading prefix, rejects absolute or workspace-escaping
path arguments, and runs each turn only from its authenticated workspace. Arguments and
flags after an allowed prefix are still passed to the real CLI, so provider-side token
scope remains the reliable boundary for what the tool can ultimately do.

Operator entries override packaged tools by name. Dropping a packaged readback refusal
is legal but produces a warning. An override that omits `allow` intentionally disables
the packaged tool rather than inheriting its safe command list.

## Declare a tool's boolean flags

The broker has no flag table, so it reads every bare flag as taking the next token as
its value. That is right for `gh --repo o/r pr view` and wrong for a flag that takes
none: `systemctl --user status cotf` reads as the subcommand `cotf`, so an `allow` entry
of `status` refuses it. Name those flags:

```yaml
commands:
  tools:
    - name: systemctl
      allow: [status, is-active, is-enabled, list-units, list-timers, show]
      boolean_flags: [--user, --system, --quiet, --no-pager, --all, --full]
      env_passthrough: [XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS]
```

`systemctl --user` finds the user manager through those two variables. The broker
passes the real binary a curated environment, so without them it fails with `Failed
to connect to user scope bus`.

List every boolean flag that can come before the subcommand, not only the ones you
use. An unlisted boolean flag still swallows the next word, and the words after it are
matched instead. With `status` allowed and `--quiet` unlisted, the broker admits
`systemctl --quiet stop status`. systemctl reads `--quiet` as boolean, so the verb it
runs is `stop`.

The readback check keeps its second reading, where no flag takes a value, so a wrong
declaration cannot open a credential readback.

## Let a tool read a file outside the workspace

The broker refuses every absolute path argument. That is right for a credentialed
CLI, which is not a file-read primitive, and wrong for one whose job is to read a
file the agent names.

Two things changed that, and only one of them needs configuration.

An absolute path that lands inside the session workspace is allowed with no key at
all. The relative spelling of that same file was always allowed, so refusing the
long form guarded nothing.

For a tree outside the workspace, name it:

```yaml
commands:
  tools:
    - name: slacker.sh
      allow: [send, read-channel]
      allow_paths:
        - /tmp
```

Now `slacker.sh send '#chan' --file /tmp/report.md` runs, while `--file /etc/passwd` is
still refused.

The guard reads a bare argument, the value of a `-o`/`--opt=` flag, and the value of a
bare `key=value` token. It also looks behind the two path introducers CLIs share: `@`,
the convention curl set and `gh`, `http` and `jq` follow, and a `file://` URL. Those
nest, so `gh api -F body=@file:///etc/passwd` is read down to the absolute path and
refused. A `file://` URL is read the way a URL parser reads it: the scheme matches in
any case (`FILE://` too), an authority is dropped, because RFC 8089 makes
`file://localhost/etc/passwd` mean `/etc/passwd` and curl reads it, and the path is
percent-decoded, so `%2e%2e` cannot smuggle a `..` past the check.

An introducer is not a claim about the tool's grammar, so an argument that merely starts
with `@` costs nothing: `send @alice` yields the extra candidate `alice`, which is
relative and inside the workspace, exactly like the argument it came from. A tool with a
path syntax outside these shapes still hands the guard a token it reads as ordinary
text, so check what path shapes a tool accepts before you broker it.

Keep the roots narrow. The broker runs the real binary **outside** the sandbox with
the operator's real credential, so a root here is a sharper grant than the same path
in `sandbox.extra_paths`: the file is read as the operator, not as the agent.

**A root grants writing as well as reading.** The guard asks where a path lands, not
what the tool will do when it gets there, and it has no per-tool flag table that could
tell an output flag from an input one. Measured: with `/tmp/shared` granted,
`-o /tmp/shared/new.txt` is admitted. Grant a tree you would let the tool overwrite. An
entry reaching a credential store is logged at ERROR and dropped, and the remaining
entries still apply.

Containment is checked after resolving, so a symlink planted inside an allowed root
cannot lead out of it.
