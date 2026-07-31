"""Preflight checks - validate tokens and tools before starting.

Env validation is delegated to claude_on_the_fly.checks (structured CheckResult
model, also consumed by the TUI doctor view). Live checks (httpx token probes,
subprocess CLI probes) live here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess

import httpx

from claude_on_the_fly import checks, settings
from claude_on_the_fly.checks import CheckResult

logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    """Read a required env var or exit with a clear message."""
    value = os.environ.get(name)
    if not value:
        raise _exit(f"Missing required environment variable: {name}")
    return value


def _raise_on_failures(results: list[CheckResult]) -> None:
    """Raise SystemExit on the first non-ok result, logging the failure."""
    fail = checks.first_failure(results)
    if fail is None:
        return
    msg = f"{fail.name}: {fail.detail}"
    if fail.fix_hint:
        msg += f" — {fail.fix_hint}"
    raise _exit(msg)


def _exit(message: str) -> SystemExit:
    """Log an error then return a SystemExit. Use as `raise _exit(msg)`.

    Preflight runs before the orchestator's file-logging handler is wired,
    so stderr-only SystemExit errors are invisible when the process is
    daemonized. Logging here ensures they survive in the console stream.
    """
    logger.error(message)
    return SystemExit(message)


def _extract_cli_result(stdout: str) -> str:
    """Pull the `result` text from `claude -p --output-format json` stdout.

    Falls back to scanning NDJSON lines if the output isn't a single JSON blob.
    """
    text = stdout.strip()
    if not text:
        return ""
    try:
        msg = json.loads(text)
        if isinstance(msg, dict) and msg.get("result"):
            return str(msg["result"])
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("result"):
            return str(msg["result"])
    return ""


_AGENT_INSTALL_HINTS = {
    "claude": "https://docs.anthropic.com/en/docs/claude-code",
    "codex": "https://github.com/openai/codex",
}


def check_backend() -> None:
    """Validate the configured agent backend before startup.

    Dispatches by `AGENT_BACKEND` (default `claude`) and `<AGENT>_MODE`
    (default `native`). Native mode runs the agent-specific live check;
    ollama mode skips it and validates the ollama wrap instead.
    """
    backend_name = settings.get("AGENT_BACKEND", "claude").lower()
    if backend_name == "claude":
        mode = settings.get("CLAUDE_MODE", "native").lower()
        if mode == "native":
            check_claude_cli()
            return
        if mode == "ollama":
            check_ollama_mode("claude")
            return
        if mode == "pty":
            check_pty_mode()
            return
        raise _exit(f"Unknown CLAUDE_MODE: {mode!r} (supported: native, ollama, pty)")
    if backend_name == "codex":
        mode = settings.get("CODEX_MODE", "native").lower()
        if mode == "native":
            check_codex_cli()
            return
        if mode == "ollama":
            check_ollama_mode("codex")
            return
        raise _exit(f"Unknown CODEX_MODE: {mode!r} (supported: native, ollama)")
    raise _exit(f"Unknown AGENT_BACKEND: {backend_name!r} (supported: claude, codex)")


def check_codex_cli() -> None:
    """Verify codex CLI is installed. Auth is checked at first use."""
    if not shutil.which("codex"):
        raise _exit(f"codex CLI not found. Install it: {_AGENT_INSTALL_HINTS['codex']}")
    logger.info("codex CLI: ok")


def check_ollama_mode(agent_name: str = "claude") -> None:
    """Validate `ollama launch <agent>` mode: both CLIs present, model available."""
    if not shutil.which(agent_name):
        raise _exit(
            f"{agent_name} CLI not found. Install it: {_AGENT_INSTALL_HINTS[agent_name]}"
        )
    if not shutil.which("ollama"):
        raise _exit("ollama CLI not found. Install it: https://ollama.com")
    model = settings.get("OLLAMA_MODEL").strip()
    if not model:
        env_var = f"{agent_name.upper()}_MODE"
        raise _exit(f"{env_var}=ollama requires OLLAMA_MODEL to be set")
    result = subprocess.run(
        ["ollama", "list"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise _exit(f"ollama list failed: {detail}")
    # First column of each non-header line is the model name.
    available = {
        line.split()[0] for line in result.stdout.splitlines()[1:] if line.strip()
    }
    if model not in available:
        # `:cloud` models (e.g. deepseek-v4-flash:cloud) are API-only and
        # won't appear in `ollama list`. Skip the model-availability check
        # for them. Non-cloud models not found still cause a hard exit.
        if model.endswith(":cloud"):
            logger.info(
                "ollama launch: cloud model %s (not in ollama list, relying on remote API)",
                model,
            )
        else:
            raise _exit(
                f"OLLAMA_MODEL={model!r} not found in `ollama list`. "
                f"Pull it first: ollama pull {model}"
            )
    logger.info("ollama launch mode: ok (agent=%s model=%s)", agent_name, model)


def check_pty_mode() -> None:
    """Validate `CLAUDE_MODE=pty`: binary resolves, jq present, hooks wired.

    Reuses the structured `checks.check_pty_setup` so doctor view and
    preflight see the same failures. If the binary is missing on an
    interactive TTY, offer to install it via the canonical curl script.
    """
    from claude_on_the_fly import checks as _checks
    from claude_on_the_fly import pty_install

    if not pty_install.is_pty_installed():
        outcome = pty_install.ensure_pty_installed()
        if not outcome.installed:
            raise _exit(outcome.message)
        logger.info("claude-pty: %s", outcome.message)

    results = _checks.check_pty_setup()
    _raise_on_failures(results)
    results = _refresh_stale_pty_hooks(results)
    for result in results:
        if result.status == "warn":
            logger.warning("claude-pty: %s", result.detail)
    logger.info("claude-pty mode: ok")


def _refresh_stale_pty_hooks(results: list[CheckResult]) -> list[CheckResult]:
    """Re-splice pty's hooks when the binary is fine but a hook is missing.

    The binary-missing gate above can't catch this: `is_pty_installed()` only
    asks whether `claude-pty` resolves, so an install whose hook set predates
    PostCompact sails past it and pty compaction then hangs at runtime.

    Hooks only — see `pty_install.HOOKS_ONLY_ENV` for why touching
    `statusLine.command` from a daemon is the wrong move. Returns the re-checked
    results, or the originals when nothing was attempted. Never raises: an
    install too old to have the hook still runs ordinary turns, so a failed
    refresh is a warning, not a startup failure.
    """
    from claude_on_the_fly import checks as _checks
    from claude_on_the_fly import pty_install

    stale = [r for r in results if r.name == "claude-pty hooks" and r.status == "warn"]
    if not stale:
        return results
    if not pty_install.auto_refresh_enabled():
        logger.warning(
            "claude-pty: hooks incomplete and %s disables the refresh — %s",
            pty_install.AUTO_REFRESH_VAR,
            stale[0].fix_hint or "update claude-pty manually",
        )
        return results

    logger.info("claude-pty: hooks incomplete, re-splicing (statusLine untouched)")
    ok, message = pty_install.refresh_hooks()
    logger.info("claude-pty: %s", message) if ok else logger.warning(
        "claude-pty: hook refresh failed: %s", message
    )
    rechecked = _checks.check_pty_setup()
    if (
        any(r.name == "claude-pty hooks" and r.status == "warn" for r in rechecked)
        and ok
    ):
        # The installer ran clean and the hook is still absent, so the published
        # claude-pty simply doesn't have it yet. Say that outright rather than
        # leaving the same warning to look like a failed write.
        logger.warning(
            "claude-pty: installer succeeded but the PostCompact hook is still "
            "absent — the published claude-pty predates it. Compaction stays "
            "off under CLAUDE_MODE=pty until it ships."
        )
    return rechecked


def check_claude_cli() -> None:
    """Verify claude CLI is installed and authenticated.

    Usage-limit / account-outage failures are downgraded to a warning so the
    server still starts — per-message handlers will surface the same error
    when someone actually tries to use it.
    """
    if not shutil.which("claude"):
        raise _exit(
            f"claude CLI not found. Install it: {_AGENT_INSTALL_HINTS['claude']}"
        )
    result = subprocess.run(
        ["claude", "-p", "--output-format", "json", "ping"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode == 0:
        logger.info("claude CLI: ok")
        return

    if "auth" in result.stderr.lower():
        raise _exit("claude CLI not authenticated. Run: claude auth login")

    message = _extract_cli_result(result.stdout) or result.stderr.strip()

    from claude_on_the_fly.agent import ClaudeUnavailableError, _classify

    if isinstance(_classify(message), ClaudeUnavailableError):
        logger.warning("claude CLI unavailable (server starting anyway): %s", message)
        return

    raise _exit(f"claude CLI check failed (exit {result.returncode}): {message}")


async def check_telegram(token: str) -> None:
    """Verify Telegram bot token is valid."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        data = resp.json()
        if not data.get("ok"):
            raise _exit(
                f"Invalid Telegram bot token: {data.get('description', 'unknown error')}"
            )
        bot_name = data["result"]["username"]
        logger.info("Telegram bot: @%s", bot_name)


async def check_slack(app_token: str, token: str) -> str:
    """Verify Slack tokens are valid. `token` is the bearer used for the API —
    either a user token (xoxp-) or a bot token (xoxb-). Returns the user_id it
    authenticates as (the bot's own user id for a bot token), which the frontend
    uses for the @mention gate and self-message checks."""
    kind = "bot" if token.startswith("xoxb-") else "user"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        if not data.get("ok"):
            raise _exit(f"Invalid Slack {kind} token: {data.get('error', 'unknown')}")
        user_id = data.get("user_id")
        if not user_id:
            raise _exit("Slack auth.test returned no user_id")
        logger.info(
            "Slack %s token: %s (%s, %s)",
            kind,
            data.get("user"),
            data.get("team"),
            user_id,
        )

        if not app_token.startswith("xapp-"):
            raise _exit("SLACK_APP_TOKEN must start with 'xapp-'")
        logger.info("Slack app token: format ok")
        return user_id


def _setup_logging() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def setup_daemon_logging(platform: str) -> None:
    """Wire this daemon's logging: see `claude_on_the_fly.logs.configure`.

    Kept as a named entry point because every supervised daemon calls it; the
    naming, rollover, and retention rules all live in `logs`.
    """
    from claude_on_the_fly import logs

    logs.configure(platform)


def run_telegram() -> tuple[str, int]:
    """Validate the config and the token. Returns (token, allowed_user_id)."""
    _setup_logging()
    _raise_on_failures(checks.check_telegram(settings.environment()))
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    # From the resolved mapping, not os.environ: `check_telegram` above validated it
    # there, so reading a different source here could pass the check and then raise a
    # KeyError on the very next line.
    allowed_user_id = int(settings.environment()["TELEGRAM_ALLOWED_USER_ID"])
    check_backend()
    asyncio.run(check_telegram(token))
    return token, allowed_user_id


def run_slack() -> tuple[str, str, str]:
    """Validate the config and the tokens. Returns (app_token, token, user_id).

    `token` is resolved from SLACK_TOKEN; user_id comes from Slack auth.test, so it
    never needs to be configured.

    The sender lists are validated and logged here but deliberately *not* returned.
    They used to be, and `SlackFrontend` was constructed with them pinned -- which
    meant adding an allowed sender took a restart, and the resolution existed in two
    places that could disagree. The frontend now reads them per message through the
    same `checks.resolve_slack_ids`, so this function's job is to fail loudly at
    startup on a list that cannot work, not to own the answer.
    """
    _setup_logging()
    _raise_on_failures(checks.check_slack(settings.environment()))
    app_token = os.environ["SLACK_APP_TOKEN"]
    _, token = checks.resolve_slack_token(settings.environment())
    for legacy, preferred in checks.slack_deprecations(settings.environment()):
        logger.warning("Slack env %s is deprecated; use %s", legacy, preferred)
    # One "allowed senders" list; ids route by Slack prefix. Bot ids (B…) get the
    # trusted-bot path (bypass @mention); everything else (U…/W…/"*") is a human.
    allowed = checks.resolve_slack_ids(
        settings.environment(), "SLACK_ALLOWED_SENDER_IDS"
    )
    allowed_bot_ids = {sid for sid in allowed if sid.startswith("B")}
    allowed_user_ids = allowed - allowed_bot_ids
    blocked_senders = checks.resolve_slack_ids(
        settings.environment(), "SLACK_BLOCKED_SENDER_IDS"
    )
    silent_sender_ids = checks.resolve_slack_ids(
        settings.environment(), "SLACK_SILENT_SENDER_IDS"
    )
    check_backend()
    user_id = asyncio.run(check_slack(app_token, token))
    logger.debug(
        "preflight: user_id=%s allowed_user_ids=%s allowed_bot_ids=%s blocked_senders=%s silent_sender_ids=%s",
        user_id,
        allowed_user_ids,
        allowed_bot_ids,
        blocked_senders,
        silent_sender_ids,
    )
    return app_token, token, user_id
