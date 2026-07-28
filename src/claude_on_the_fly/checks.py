"""Structured preflight checks.

Each checker takes an env mapping and returns a list of CheckResult. Pure
functions — no I/O for env checks, optional `shutil.which` for binary checks.

Used by:
- preflight.py: raises SystemExit on any non-ok result (backward-compatible behavior)
- tui/screens/doctor.py: renders results with fix hints
- tui/env_editor.py: maps changed env vars back to affected daemons via FRONTEND_ENV_VARS
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Status = Literal["ok", "missing", "invalid", "warn"]

DOTENV_HINT = "set in ~/.claude-on-the-fly/.env"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    fix_hint: str | None = None


# ---------------------------------------------------------------------------
# Env var declarations — used by env_editor to map changes to daemons.
# ---------------------------------------------------------------------------

TELEGRAM_ENV_VARS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "TELEGRAM_STATS_MODE",
)
SLACK_ENV_VARS: tuple[str, ...] = (
    "SLACK_APP_TOKEN",
    "SLACK_TOKEN",
    "SLACK_ALLOWED_SENDER_IDS",
    "SLACK_BLOCKED_SENDER_IDS",
    "SLACK_SILENT_SENDER_IDS",
    "SLACK_SLASH_COMMAND",
    # Declared here and not in JOBS_ENV_VARS: the slack daemon is the process
    # that binds it (slack.JOB_COMMAND), so it is the one env_editor must
    # restart when it changes.
    "SLACK_JOB_COMMAND",
    "SLACK_STATS_MODE",
    # Deprecated aliases (still honored; see SLACK_LEGACY).
    "SLACK_USER_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_ALLOWED_USER_IDS",
    "SLACK_BLOCKED_USER_IDS",
    "SLACK_ALLOWED_BOT_IDS",
)
GMAIL_ENV_VARS: tuple[str, ...] = (
    "GMAIL_GCP_PROJECT",
    "GMAIL_ALLOWED_SENDERS",
    "GMAIL_POLL_INTERVAL",
    "GMAIL_STATS_MODE",
)
# The worker shares SLACK_TOKEN with the slack frontend (editing it affects both);
# JOBS_SLACK_TOKEN optionally overrides it. JOBS_TIMEOUT is the per-job wall-clock
# limit in seconds (0 or negative = no limit). JOBS_STALE_TTL_S is deferred with
# multi-worker support.
JOBS_ENV_VARS: tuple[str, ...] = (
    "JOBS_QUEUE_KIND",
    "JOBS_POLL_INTERVAL_S",
    "JOBS_TIMEOUT",
    "JOBS_SLACK_TOKEN",
    "SLACK_TOKEN",
)
BACKEND_ENV_VARS: tuple[str, ...] = (
    "AGENT_BACKEND",
    "CLAUDE_MODE",
    "CODEX_MODE",
    "PI_MODE",
    "OPENCODE_MODE",
    "OLLAMA_MODEL",
    "CLAUDE_MODEL",
    "CODEX_MODEL",
    "PI_MODEL",
    "OPENCODE_MODEL",
    "PI_PROVIDER",
)

FRONTEND_ENV_VARS: dict[str, tuple[str, ...]] = {
    "telegram": TELEGRAM_ENV_VARS,
    "slack": SLACK_ENV_VARS,
    "gmail": GMAIL_ENV_VARS,
    "schedule": (),  # no per-frontend env vars; uses schedule.yaml
    "symphony": (),  # uses symphony.yaml
    "jobs": JOBS_ENV_VARS,
}

SUPERVISABLE_FRONTENDS: tuple[str, ...] = (
    "telegram",
    "slack",
    "gmail",
    "schedule",
    "symphony",
    "jobs",
)


def _config_files() -> dict[str, Path]:
    """Per-frontend required config files. Resolved lazily so DATA_DIR can be
    monkeypatched in tests."""
    from claude_on_the_fly.agent import DATA_DIR

    return {
        "schedule": DATA_DIR / "schedule.yaml",
        "symphony": DATA_DIR / "symphony.yaml",
    }


def check_config_file(frontend: str) -> list[CheckResult]:
    """Verify any config file the frontend needs at startup is on disk."""
    path = _config_files().get(frontend)
    if path is None:
        return []
    if path.is_file():
        return [CheckResult(name=path.name, status="ok", detail=str(path))]
    hint = (
        "See README for examples"
        if frontend == "schedule"
        else "See symphony.yaml.example"
    )
    return [
        CheckResult(
            name=path.name,
            status="missing",
            detail=f"required config not found at {path}",
            fix_hint=hint,
        )
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require(env: Mapping[str, str], name: str, hint: str | None = None) -> CheckResult:
    value = env.get(name)
    if value is None or value == "":
        return CheckResult(
            name=name,
            status="missing",
            detail="not set",
            fix_hint=hint or DOTENV_HINT,
        )
    return CheckResult(name=name, status="ok", detail="set")


# ---------------------------------------------------------------------------
# Per-frontend env checks
# ---------------------------------------------------------------------------


def check_telegram(env: Mapping[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = [_require(env, "TELEGRAM_BOT_TOKEN")]

    uid = env.get("TELEGRAM_ALLOWED_USER_ID", "")
    if not uid:
        results.append(
            CheckResult(
                name="TELEGRAM_ALLOWED_USER_ID",
                status="missing",
                detail="not set",
                fix_hint=DOTENV_HINT,
            )
        )
    else:
        try:
            int(uid)
            results.append(
                CheckResult(
                    name="TELEGRAM_ALLOWED_USER_ID", status="ok", detail=f"= {uid}"
                )
            )
        except ValueError:
            results.append(
                CheckResult(
                    name="TELEGRAM_ALLOWED_USER_ID",
                    status="invalid",
                    detail=f"must be an integer, got {uid!r}",
                    fix_hint="Use the numeric Telegram user ID, not the @handle",
                )
            )
    return results


def check_slack(env: Mapping[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = []

    app_check = _require(env, "SLACK_APP_TOKEN")
    if app_check.status == "ok":
        token = env["SLACK_APP_TOKEN"]
        if not token.startswith("xapp-"):
            app_check = CheckResult(
                name="SLACK_APP_TOKEN",
                status="invalid",
                detail="must start with 'xapp-'",
                fix_hint="Use the App-Level Token from Slack admin, not the user token",
            )
    results.append(app_check)

    results.append(_check_slack_bearer(env))

    command = env.get("SLACK_SLASH_COMMAND", "")
    if command:
        results.append(_check_slack_command(command))

    job_command = env.get("SLACK_JOB_COMMAND", "")
    if job_command:
        results.append(_check_slack_job_command(job_command))

    return results


def _check_slack_command(command: str) -> CheckResult:
    """Validate SLACK_SLASH_COMMAND if set. It is optional, but a malformed one
    fails silently at runtime: slack_bolt registers the handler and Slack simply
    never routes anything to it."""
    from claude_on_the_fly.slack_manifest import command_error

    problem = command_error(command)
    if problem is None:
        return CheckResult(name="SLACK_SLASH_COMMAND", status="ok", detail=command)
    return CheckResult(
        name="SLACK_SLASH_COMMAND",
        status="invalid",
        detail=f"{command} {problem}",
        fix_hint="Unset it to drop the slash command, or run "
        "`claude-slack --manifest` to pick one and get a matching manifest",
    )


def _job_command_error(value: str) -> tuple[Status, str] | None:
    """None when `value` is a usable job trigger, else (status, why).

    The status separates "this cannot work" from "this works, but probably not
    as you meant": only the first is a reason to refuse to start the daemon.

    Three kinds of mistake, worth keeping apart: Slack never delivers it
    ('/', '<>&'); one of our own prefixes eats it ($stop, $continue); or it
    fires, but not as its author meant (whitespace, a leading word character).
    Only that last group is a trap rather than a dead trigger. Deliberately not
    a full charset check, for the same reason as `slack_manifest.command_error`:
    Slack is the authority on what a message may contain.
    """
    if any(char.isspace() for char in value):
        # Fires, so don't read this as "can never match": *internal* whitespace
        # matches — "$my job" on "$my job <task>" — but only on exactly the
        # spacing its author typed, so a doubled one misses with no error. Only
        # the *leading* form can never match, and a *trailing* one kills the
        # bare-trigger usage form: `_ingest_event` strips before comparing.
        return "invalid", "cannot contain whitespace"
    if value.startswith("/"):
        return (
            "invalid",
            "cannot start with '/' — Slack routes that as a slash command",
        )
    if any(char in value for char in "<>&"):
        # `_ingest_event` reads `event["text"]` raw, with nothing unescaping it.
        return (
            "invalid",
            "cannot contain '<', '>' or '&' — Slack escapes them in message text",
        )
    # The turn-control prefixes as literals: importing them from
    # `claude_on_the_fly.slack` would pull slack_bolt into every checks import.
    # test_checks.py `test_turn_control_prefixes_match_the_slack_constants`
    # fails on drift, so the duplication is checked rather than merely noted.
    if value == "$stop":
        # Matched by exact equality and wins first, so the trigger is not merely
        # dead: a bare "$stop" still aborts the turn while "$stop do the thing"
        # falls past it and queues a job — one prefix, two behaviours.
        return "invalid", "collides with the $stop turn-control prefix"
    if value == "$continue":
        # Intercepted *after* the job branch, so a trigger equal to it shadows
        # the reply soft-limit reset entirely — a gated thread could never be
        # un-gated.
        return "invalid", "collides with the $continue turn-control prefix"
    if value[:1].isalnum():
        # A footgun, not a silent failure: it fires exactly as written, and the
        # trigger is matched against the head of every inbound message, so an
        # ordinary word swallows any message that opens with it.
        return (
            "warn",
            "should start with punctuation, e.g. `$job` — a plain word "
            "swallows every message beginning with it",
        )
    return None


def _check_slack_job_command(command: str) -> CheckResult:
    """Validate SLACK_JOB_COMMAND if set. It is optional, but a malformed one
    costs nothing at startup and everything at runtime — see
    `_job_command_error` for the three ways it goes wrong."""
    problem = _job_command_error(command)
    if problem is None:
        return CheckResult(name="SLACK_JOB_COMMAND", status="ok", detail=command)
    status, detail = problem
    return CheckResult(
        name="SLACK_JOB_COMMAND",
        status=status,
        detail=f"{command} {detail}",
        fix_hint="Unset it to drop the background-job trigger, or pick a "
        "punctuation-led value like `$job`",
    )


# Preferred env var -> deprecated fallbacks it replaces. Legacy names still work
# but are undocumented; run_slack warns when one is used. SLACK_ALLOWED_SENDER_IDS
# supersedes both the old user and bot allowlists (ids route by prefix).
SLACK_LEGACY: dict[str, tuple[str, ...]] = {
    "SLACK_TOKEN": ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN"),
    "SLACK_ALLOWED_SENDER_IDS": ("SLACK_ALLOWED_USER_IDS", "SLACK_ALLOWED_BOT_IDS"),
    "SLACK_BLOCKED_SENDER_IDS": ("SLACK_BLOCKED_USER_IDS",),
}


def _parse_ids(raw: str) -> set[str]:
    return {piece.strip() for piece in raw.split(",") if piece.strip()}


def resolve_slack_token(env: Mapping[str, str]) -> tuple[str | None, str]:
    """Return (var_name, token) for the first set Slack bearer var, or (None, "").
    The token kind (user vs bot) is inferred from its prefix, so one SLACK_TOKEN
    field covers both; SLACK_USER_TOKEN / SLACK_BOT_TOKEN still work."""
    for name in ("SLACK_TOKEN", *SLACK_LEGACY["SLACK_TOKEN"]):
        value = env.get(name, "")
        if value:
            return name, value
    return None, ""


def resolve_jobs_token(env: Mapping[str, str]) -> tuple[str | None, str]:
    """Return (var_name, token) for the claude-jobs notifier's Slack bearer.

    `JOBS_SLACK_TOKEN` overrides the shared `SLACK_TOKEN` so the worker can post
    under a different identity than the acking frontend (deployer-controlled).
    Falls back to `resolve_slack_token` (honoring the legacy aliases)."""
    override = env.get("JOBS_SLACK_TOKEN", "")
    if override:
        return "JOBS_SLACK_TOKEN", override
    return resolve_slack_token(env)


def resolve_slack_ids(env: Mapping[str, str], preferred: str) -> set[str]:
    """Resolve a comma-separated id set. The preferred var wins; otherwise its
    legacy fallbacks are merged (so the old split user/bot allowlists combine)."""
    raw = env.get(preferred, "")
    if raw:
        return _parse_ids(raw)
    ids: set[str] = set()
    for legacy in SLACK_LEGACY.get(preferred, ()):
        ids |= _parse_ids(env.get(legacy, ""))
    return ids


def slack_deprecations(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """(legacy_var, preferred_var) for every deprecated Slack var in use — i.e.
    set while its preferred replacement is not."""
    out: list[tuple[str, str]] = []
    for preferred, legacy_names in SLACK_LEGACY.items():
        if env.get(preferred, ""):
            continue
        out.extend(
            (legacy, preferred) for legacy in legacy_names if env.get(legacy, "")
        )
    return out


def _check_slack_bearer(env: Mapping[str, str]) -> CheckResult:
    """A single bearer token, either kind: `xoxp-` (replies as you) or `xoxb-`
    (replies as the app). Set it as SLACK_TOKEN."""
    name, token = resolve_slack_token(env)
    if not token:
        return CheckResult(
            name="SLACK_TOKEN",
            status="missing",
            detail="set SLACK_TOKEN to an xoxp- (user) or xoxb- (bot) token",
            fix_hint=DOTENV_HINT,
        )
    if not token.startswith(("xoxp-", "xoxb-")):
        return CheckResult(
            name=name or "SLACK_TOKEN",
            status="invalid",
            detail="must start with 'xoxp-' (user) or 'xoxb-' (bot)",
            fix_hint="Use the User or Bot OAuth Token from Slack admin",
        )
    kind = "bot" if token.startswith("xoxb-") else "user"
    return CheckResult(name=name or "SLACK_TOKEN", status="ok", detail=f"set ({kind})")


def check_gmail(env: Mapping[str, str]) -> list[CheckResult]:
    results: list[CheckResult] = [_require(env, "GMAIL_GCP_PROJECT")]

    senders_raw = env.get("GMAIL_ALLOWED_SENDERS", "")
    senders = {s.strip() for s in senders_raw.split(",") if s.strip()}
    if not senders:
        results.append(
            CheckResult(
                name="GMAIL_ALLOWED_SENDERS",
                status="missing",
                detail="must contain at least one email address, pattern, or '*'",
                fix_hint=DOTENV_HINT,
            )
        )
    else:
        results.append(
            CheckResult(
                name="GMAIL_ALLOWED_SENDERS",
                status="ok",
                detail=f"{len(senders)} sender(s)",
            )
        )
    return results


def check_jobs(env: Mapping[str, str]) -> list[CheckResult]:
    """The worker's notifier needs a resolvable Slack bearer token, and the
    worker itself needs something able to enqueue into it."""
    return [_check_jobs_token(env), _slack_job_producer_note(env)]


def _check_jobs_token(env: Mapping[str, str]) -> CheckResult:
    """Honors JOBS_SLACK_TOKEN (worker-specific override) first, else the shared
    SLACK_TOKEN via `_check_slack_bearer`. Same token-kind rules as slack — no
    hardcoded identity, no mandated kind."""
    override = env.get("JOBS_SLACK_TOKEN", "")
    if not override:
        return _check_slack_bearer(env)
    if not override.startswith(("xoxp-", "xoxb-")):
        return CheckResult(
            name="JOBS_SLACK_TOKEN",
            status="invalid",
            detail="must start with 'xoxp-' (user) or 'xoxb-' (bot)",
            fix_hint="Use the User or Bot OAuth Token from Slack admin",
        )
    kind = "bot" if override.startswith("xoxb-") else "user"
    return CheckResult(name="JOBS_SLACK_TOKEN", status="ok", detail=f"set ({kind})")


def _slack_job_producer_note(env: Mapping[str, str]) -> CheckResult:
    """Report whether Slack can enqueue into this worker at all.

    `claude-jobs doctor` runs `check_jobs`, never `check_slack`, so without this
    line a worker whose SLACK_JOB_COMMAND is unset reports "all checks passed"
    while nothing in Slack can ever reach it. A malformed value reports its
    problem here too, because the doctor view renders the `slack` and `jobs`
    groups on one screen (`tui/screens/doctor.py`) and a bare "producer on"
    would contradict `check_slack`'s `invalid` row of the same name. Always
    `ok`: an `enqueue`-only install is legitimate, and any other status would
    make `claude-jobs doctor` (`jobs/cli.py` `_cmd_doctor`) exit 1 on it.
    """
    command = env.get("SLACK_JOB_COMMAND", "")
    if not command:
        detail = "unset — Slack cannot enqueue; `claude-jobs enqueue` only"
    else:
        problem = _job_command_error(command)
        detail = (
            f"Slack producer on ({command})"
            if problem is None
            else f"Slack producer misconfigured: {command} {problem}"
        )
    return CheckResult(name="SLACK_JOB_COMMAND", status="ok", detail=detail)


# ---------------------------------------------------------------------------
# Backend (CLI selection) env checks
# ---------------------------------------------------------------------------

_VALID_BACKENDS = ("claude", "codex", "pi", "opencode")
_VALID_MODES = ("native", "ollama")
# claude-pty is claude-only; codex has no equivalent wrapper.
_VALID_CLAUDE_MODES = ("native", "ollama", "pty")


def check_backend(env: Mapping[str, str]) -> list[CheckResult]:
    backend = env.get("AGENT_BACKEND", "claude").lower()
    results: list[CheckResult] = []

    if backend not in _VALID_BACKENDS:
        results.append(
            CheckResult(
                name="AGENT_BACKEND",
                status="invalid",
                detail=f"{backend!r} (supported: {', '.join(_VALID_BACKENDS)})",
                fix_hint=f"Set AGENT_BACKEND to one of: {', '.join(_VALID_BACKENDS)}",
            )
        )
        return results

    results.append(
        CheckResult(name="AGENT_BACKEND", status="ok", detail=f"= {backend}")
    )

    mode_var = f"{backend.upper()}_MODE"
    valid_modes = _VALID_CLAUDE_MODES if backend == "claude" else _VALID_MODES
    mode = env.get(mode_var, "native").lower()
    if mode not in valid_modes:
        results.append(
            CheckResult(
                name=mode_var,
                status="invalid",
                detail=f"{mode!r} (supported: {', '.join(valid_modes)})",
                fix_hint=f"Set {mode_var}={' or '.join(valid_modes)}",
            )
        )
        return results
    results.append(CheckResult(name=mode_var, status="ok", detail=f"= {mode}"))

    if mode == "ollama":
        model = env.get("OLLAMA_MODEL", "").strip()
        if not model:
            results.append(
                CheckResult(
                    name="OLLAMA_MODEL",
                    status="missing",
                    detail=f"required when {mode_var}=ollama",
                    fix_hint=DOTENV_HINT,
                )
            )
        else:
            results.append(
                CheckResult(name="OLLAMA_MODEL", status="ok", detail=f"= {model}")
            )

    return results


# ---------------------------------------------------------------------------
# Binary checks (optional shutil.which probes)
# ---------------------------------------------------------------------------


def _which(binary: str, install_hint: str) -> CheckResult:
    if shutil.which(binary):
        return CheckResult(name=binary, status="ok", detail="installed")
    return CheckResult(
        name=binary,
        status="missing",
        detail="not on PATH",
        fix_hint=install_hint,
    )


def check_binaries(env: Mapping[str, str]) -> list[CheckResult]:
    backend = env.get("AGENT_BACKEND", "claude").lower()
    results: list[CheckResult] = []

    if backend == "claude":
        results.append(
            _which("claude", "Install: https://docs.anthropic.com/en/docs/claude-code")
        )
    elif backend == "codex":
        results.append(_which("codex", "Install: https://github.com/openai/codex"))
    elif backend == "pi":
        results.append(
            _which("pi", "Install: https://github.com/earendil-works/pi-coding-agent")
        )
    elif backend == "opencode":
        results.append(_which("opencode", "Install: https://opencode.ai"))

    mode = env.get(f"{backend.upper()}_MODE", "native").lower()
    if mode == "ollama":
        results.append(_which("ollama", "Install: https://ollama.com"))
    if mode == "pty" and backend == "claude":
        results.extend(check_pty_setup())

    return results


def check_pty_setup() -> list[CheckResult]:
    """Validate claude-pty install + hook wiring for CLAUDE_MODE=pty.

    Three sub-checks: the pty binary resolves, jq is on PATH (pty shells out
    to it), and ~/.claude/settings.json has pty's Stop hook + statusline shim
    wired in (without them, pty hangs forever waiting on an envelope file).
    """
    from claude_on_the_fly.backends.claude import (
        PTY_INSTALL_HINT,
        PTY_PROJECT_SLUG,
        resolve_pty_binary,
    )

    results: list[CheckResult] = []

    pty_path = resolve_pty_binary()
    if pty_path:
        results.append(CheckResult(name="claude-pty", status="ok", detail=pty_path))
    else:
        results.append(
            CheckResult(
                name="claude-pty",
                status="missing",
                detail=f"not on PATH or ~/.local/share/{PTY_PROJECT_SLUG}/bin",
                fix_hint=PTY_INSTALL_HINT,
            )
        )

    results.append(_which("jq", "Install: brew install jq (pty shells out to jq)"))

    results.append(check_pty_hooks())
    return results


# Environment variables the two shims are built around: the statusline shim
# writes the sidecar, the Stop hook writes the envelope whose appearance is
# pty's "turn done" signal. A script that names one is that shim; a script that
# does not cannot satisfy the contract whatever it is called or wherever it
# lives.
PTY_SIDECAR_MARKER = "CLAUDE_PTY_SIDECAR"
PTY_ENVELOPE_MARKER = "CLAUDE_PTY_ENVELOPE"
# Bytes read from a wired script when identifying it. The markers sit in the
# first few lines of both shims; this only has to be generous, not exact.
PTY_SHIM_PROBE_BYTES = 64 * 1024


def _is_pty_shim(command: str, marker: str) -> bool:
    """Whether `command` refers to a script implementing pty's side of `marker`.

    Read the script and look for the environment variable it must act on,
    rather than matching the install path. The path told us which *install*
    wired the hook, not whether a working one is wired — so a shim installed
    anywhere else (a sibling tool vendoring the same project, a user's own
    prefix) read as "missing" and took down every daemon that runs under
    CLAUDE_MODE=pty, over a functioning setup.

    Falls back to the install-path match when the script cannot be read, so an
    unreadable-but-correctly-named shim is no worse off than before.
    """
    from claude_on_the_fly.backends.claude import PTY_PROJECT_SLUG

    command = command.strip()
    if not command:
        return False
    # The wired value can carry arguments; the script is the first field.
    script = command.split()[0]
    try:
        with open(script, encoding="utf-8", errors="replace") as handle:
            return marker in handle.read(PTY_SHIM_PROBE_BYTES)
    except OSError:
        return PTY_PROJECT_SLUG in command


def check_pty_hooks() -> CheckResult:
    """Verify ~/.claude/settings.json wires pty's Stop hook + statusline shim.

    The two hooks are what make pty work — without them claude-pty hangs
    forever. install.sh writes them; this check catches a stale config.

    Identified by what the wired scripts do, not where they were installed —
    see `_is_pty_shim`. Several tools vendor the same shims into their own
    prefixes and rewrite `statusLine.command` to their copy, so a path match
    fails whenever the last writer was not the install this repo expects.
    """
    from claude_on_the_fly.backends.claude import (
        PTY_INSTALL_HINT,
    )

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    settings_path = Path(config_dir) / "settings.json"
    if not settings_path.is_file():
        return CheckResult(
            name="claude-pty hooks",
            status="missing",
            detail=f"no settings.json at {settings_path}",
            fix_hint=PTY_INSTALL_HINT,
        )

    import json as _json

    try:
        config = _json.loads(settings_path.read_text())
    except (_json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            name="claude-pty hooks",
            status="invalid",
            detail=f"cannot parse {settings_path}: {exc}",
            fix_hint="Fix the JSON or restore from the backup install.sh left",
        )

    statusline_cmd = ""
    statusline_node = config.get("statusLine")
    if isinstance(statusline_node, dict):
        statusline_cmd = str(statusline_node.get("command", ""))
    has_statusline = _is_pty_shim(statusline_cmd, PTY_SIDECAR_MARKER)

    has_stop_hook = False
    for entry in config.get("hooks", {}).get("Stop", []) or []:
        for hook in entry.get("hooks", []) or []:
            if _is_pty_shim(str(hook.get("command", "")), PTY_ENVELOPE_MARKER):
                has_stop_hook = True
                break
        if has_stop_hook:
            break

    if has_statusline and has_stop_hook:
        return CheckResult(
            name="claude-pty hooks",
            status="ok",
            detail="Stop hook + statusline shim wired",
        )

    missing = []
    if not has_statusline:
        missing.append("statusLine shim")
    if not has_stop_hook:
        missing.append("Stop hook")
    return CheckResult(
        name="claude-pty hooks",
        status="missing",
        detail=f"settings.json missing: {', '.join(missing)}",
        fix_hint=PTY_INSTALL_HINT,
    )


def check_gws_binary() -> CheckResult:
    return _which(
        "gws",
        "Install: npm install -g @googleworkspace/cli",
    )


# ---------------------------------------------------------------------------
# Top-level aggregator
# ---------------------------------------------------------------------------


_ENV_CHECKERS: dict[str, Callable[[Mapping[str, str]], list[CheckResult]]] = {
    "telegram": check_telegram,
    "slack": check_slack,
    "gmail": check_gmail,
    "jobs": check_jobs,
}


def check_symphony_acli() -> CheckResult:
    """Symphony shells out to `acli` for every Jira read.

    Auth lives in `acli auth login`. We can probe the binary cheaply; full
    auth verification would require a network call so we leave that to the
    daemon's first poll (which raises with a clear error if unauth'd).
    """
    if shutil.which("acli"):
        return CheckResult(
            name="acli",
            status="ok",
            detail="installed (auth verified on first poll)",
        )
    return CheckResult(
        name="acli",
        status="missing",
        detail="not on PATH",
        fix_hint=(
            "Install acli (https://developer.atlassian.com/cloud/acli/) "
            "and run `acli auth login`"
        ),
    )


def check_frontend(frontend: str, env: Mapping[str, str]) -> list[CheckResult]:
    """Per-frontend checks: env vars + required config files + tool auth."""
    if frontend not in SUPERVISABLE_FRONTENDS:
        raise ValueError(f"unknown frontend: {frontend!r}")
    env_checker = _ENV_CHECKERS.get(frontend)
    env_checks = env_checker(env) if env_checker else []
    extra: list[CheckResult] = []
    if frontend == "symphony":
        extra.append(check_symphony_acli())
    return env_checks + check_config_file(frontend) + extra


def check_all(env: Mapping[str, str] | None = None) -> dict[str, list[CheckResult]]:
    """Run every check group. Used by the doctor view."""
    e = os.environ if env is None else env
    return {
        **{name: check_frontend(name, e) for name in SUPERVISABLE_FRONTENDS},
        "backend": check_backend(e),
        "binaries": check_binaries(e),
    }


# Statuses that do not stop a daemon from starting. `warn` reports a setting
# that works exactly as written but is likely not what its author meant — advice
# worth surfacing in `doctor`, never a reason to refuse to run. `missing` and
# `invalid` stay blocking: they describe a daemon that cannot do its job.
NON_BLOCKING_STATUSES: frozenset[Status] = frozenset({"ok", "warn"})


def first_failure(results: list[CheckResult]) -> CheckResult | None:
    """Return the first blocking result, or None if none blocks startup."""
    for r in results:
        if r.status not in NON_BLOCKING_STATUSES:
            return r
    return None


def all_ok(results: list[CheckResult]) -> bool:
    """True when nothing blocks startup. Advisory `warn` results do not."""
    return all(r.status in NON_BLOCKING_STATUSES for r in results)
