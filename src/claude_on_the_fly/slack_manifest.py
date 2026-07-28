"""Slack app manifest generator (`claude-slack --manifest`).

The manifest can't be a static file people hand-edit, because two of its blocks
depend on choices only the installer can make:

- **Token kind.** A manifest declaring both bot and user scopes makes every
  installer grant both, so a bot install also hands over user scopes on the
  installer's own account. Each mode gets only its own blocks.
- **Slash command.** Slack does not namespace slash commands: install an app
  using a command another app already registered and the newest install wins
  workspace-wide, silently breaking the older one. So the command is opt-in and
  named per install, matching `SLACK_SLASH_COMMAND` in the daemon's env.

`slack_manifest.json` next to this module is the single template; render() prunes
it down to the blocks a given install actually uses.
"""

from __future__ import annotations

import getpass
import json
import re
import sys
from pathlib import Path
from typing import Any

TEMPLATE_PATH = Path(__file__).with_name("slack_manifest.json")
# The command in the template. Loud on sight, harmless if someone pastes the
# template verbatim, and refused by render() so it can't reach a real install.
PLACEHOLDER_COMMAND = "/cof-CHANGEME"
DEFAULT_NAME = "Claude On The Fly"
DEFAULT_OUT = "slack_manifest.json"
MODES = ("bot", "user")

MODE_HELP = """Which token kind is this install for?

  bot   The app replies as itself. DMs work right away, but it has to be
        invited to each channel. You get a slash command, the skill picker,
        and the "Run a skill" message shortcut.
  user  It replies as you, and sees every channel you can. None of the
        interactive surface exists under a user token, so turn control is the
        $stop / $continue / $compact text prefixes.
"""

COMMAND_HELP = """Slash command (optional).

Slack does not namespace slash commands. If a coworker installs an app using
the same one, theirs wins for the whole workspace and yours stops firing, with
no error. So the suggested default carries your login name, which differs from
your coworkers'.

Answer 'none' to skip the command entirely: the skill picker is still reachable
from any message's "..." menu, and $stop / $continue / $compact still work.
"""


def command_error(value: str) -> str | None:
    """None when `value` works as a slash command, else why it doesn't.

    Deliberately not a full charset check — Slack's own validator is the
    authority on what it accepts, and guessing its rules here would reject
    commands that are actually fine. This catches the mistakes that fail
    silently at runtime instead: a missing '/' registers a handler that never
    matches anything.
    """
    if not value.startswith("/"):
        return "must start with '/'"
    if value == PLACEHOLDER_COMMAND:
        return f"still the template placeholder, pick your own instead of {value}"
    body = value[1:]
    if not body:
        return "missing a name after the '/'"
    if any(char.isspace() for char in value):
        return "cannot contain spaces"
    if len(value) > 32:
        return "Slack caps a command at 32 characters"
    return None


def suggested_command() -> str | None:
    """`/cof-<login name>`, or None when there's no usable one.

    Seeded from the login name rather than the app name because this value has
    to be unique in the workspace, and the app name isn't: two people installing
    with the default name would derive the same command and hijack each other,
    which is the whole failure this prompt exists to prevent. A prompt default
    gets accepted without thinking, so it has to be safe to accept without
    thinking.
    """
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        # No passwd entry and no USER/LOGNAME in the env, e.g. some containers.
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", user.lower()).strip("-")
    if not slug:
        return None
    command = f"/cof-{slug}"[:32].rstrip("-")
    return command if command_error(command) is None else None


def render(mode: str, name: str, command: str | None) -> dict[str, Any]:
    """Build the manifest for one install. `command` None/empty omits it."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {' | '.join(MODES)}, got {mode!r}")
    if command:
        problem = command_error(command)
        if problem:
            raise ValueError(f"slash command {command!r} {problem}")

    manifest = json.loads(TEMPLATE_PATH.read_text())
    features = manifest["features"]
    scopes = manifest["oauth_config"]["scopes"]
    events = manifest["settings"]["event_subscriptions"]
    manifest["display_information"]["name"] = name

    if mode == "bot":
        features["bot_user"]["display_name"] = name
        del scopes["user"], events["user_events"]
        if command:
            features["slash_commands"][0]["command"] = command
        else:
            del features["slash_commands"]
    else:
        # A user token receives none of the app-interaction payloads (slack.py
        # gates them on _is_bot_token) and creates no bot to DM, so every block
        # below would be config that can never fire.
        for dead in ("bot_user", "app_home", "slash_commands", "shortcuts"):
            del features[dead]
        del scopes["bot"], events["bot_events"], manifest["settings"]["interactivity"]
        if not features:
            del manifest["features"]

    return manifest


def generate(
    *,
    mode: str | None,
    name: str | None,
    command: str | None,
    out: str | None,
) -> int:
    """Render a manifest, asking for anything not passed as a flag.

    Prompts and guidance go to stderr, the manifest to stdout or a file, so
    `--manifest --mode bot > manifest.json` redirects cleanly.
    """
    if mode is None:
        if not sys.stdin.isatty():
            _err(f"--mode {' | '.join(MODES)} is required when there's no terminal")
            return 2
        mode, name, command, out = _ask(name, command, out)

    try:
        manifest = render(mode, name or DEFAULT_NAME, command)
    except ValueError as exc:
        _err(str(exc))
        return 2

    blob = json.dumps(manifest, indent=2) + "\n"
    if out:
        target = Path(out)
        if target.exists() and not _confirm(f"{target} exists, overwrite?"):
            _err("cancelled")
            return 1
        target.write_text(blob)
        _err(f"\nWrote {target}")
    else:
        sys.stdout.write(blob)

    _next_steps(mode, command, out)
    return 0


def _ask(
    name: str | None, command: str | None, out: str | None
) -> tuple[str, str, str | None, str]:
    _err(MODE_HELP)
    mode = ""
    while mode not in MODES:
        mode = _line(f"Mode ({' | '.join(MODES)})", "bot")

    name = name or _line("App name, as it appears in Slack", DEFAULT_NAME)

    if mode == "bot" and command is None:
        _err("\n" + COMMAND_HELP)
        suggestion = suggested_command() or ""
        while True:
            answer = _line("Slash command ('none' to skip)", suggestion)
            if not answer or answer.lower() == "none":
                command = None
                break
            problem = command_error(answer)
            if not problem:
                command = answer
                break
            _err(f"  {answer} {problem}")
    elif mode == "user":
        command = None

    return mode, name, command, out or _line("Write the manifest to", DEFAULT_OUT)


def _line(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    print(f"{question}{suffix}: ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    if not answer:
        raise SystemExit("\nmanifest: input closed")
    return answer.strip() or default


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return _line(f"{question} (y/N)").lower() in ("y", "yes")


def _next_steps(mode: str, command: str | None, out: str | None) -> None:
    source = f"the contents of {out}" if out else "the JSON above"
    token = "xoxb-" if mode == "bot" else "xoxp-"
    env = ["SLACK_APP_TOKEN=xapp-...", f"SLACK_TOKEN={token}..."]
    if command:
        env.append(f"SLACK_SLASH_COMMAND={command}")
    _err(
        "\nNext steps\n"
        "  1. https://api.slack.com/apps -> Create New App -> From a manifest\n"
        "  2. Pick your workspace, open the JSON tab, paste "
        f"{source}\n"
        "  3. Socket Mode -> toggle ON\n"
        "  4. Basic Information -> App-Level Tokens -> create one with "
        "connections:write\n"
        "  5. Install App -> Install to Workspace\n"
        "\nThen put these in .env (see docs/how-to/slack.md for the rest):\n"
        + "".join(f"  {line}\n" for line in env)
    )


def _err(message: str) -> None:
    print(message, file=sys.stderr)
