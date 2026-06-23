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

from claude_on_the_fly import checks
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
    "pi": "https://github.com/earendil-works/pi-coding-agent",
    "opencode": "https://opencode.ai",
}


def check_backend() -> None:
    """Validate the configured agent backend before startup.

    Dispatches by `AGENT_BACKEND` (default `claude`) and `<AGENT>_MODE`
    (default `native`). Native mode runs the agent-specific live check;
    ollama mode skips it and validates the ollama wrap instead.
    """
    backend_name = os.environ.get("AGENT_BACKEND", "claude").lower()
    if backend_name == "claude":
        mode = os.environ.get("CLAUDE_MODE", "native").lower()
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
        mode = os.environ.get("CODEX_MODE", "native").lower()
        if mode == "native":
            check_codex_cli()
            return
        if mode == "ollama":
            check_ollama_mode("codex")
            return
        raise _exit(f"Unknown CODEX_MODE: {mode!r} (supported: native, ollama)")
    if backend_name == "pi":
        mode = os.environ.get("PI_MODE", "native").lower()
        if mode == "native":
            check_pi_cli()
            return
        if mode == "ollama":
            check_ollama_mode("pi")
            return
        raise _exit(f"Unknown PI_MODE: {mode!r} (supported: native, ollama)")
    if backend_name == "opencode":
        mode = os.environ.get("OPENCODE_MODE", "native").lower()
        if mode == "native":
            check_opencode_cli()
            return
        if mode == "ollama":
            check_ollama_mode("opencode")
            return
        raise _exit(f"Unknown OPENCODE_MODE: {mode!r} (supported: native, ollama)")
    raise _exit(
        f"Unknown AGENT_BACKEND: {backend_name!r} "
        "(supported: claude, codex, pi, opencode)"
    )


def check_codex_cli() -> None:
    """Verify codex CLI is installed. Auth is checked at first use."""
    if not shutil.which("codex"):
        raise _exit(f"codex CLI not found. Install it: {_AGENT_INSTALL_HINTS['codex']}")
    logger.info("codex CLI: ok")


def check_pi_cli() -> None:
    """Verify pi CLI is installed. Auth is checked at first use."""
    if not shutil.which("pi"):
        raise _exit(f"pi CLI not found. Install it: {_AGENT_INSTALL_HINTS['pi']}")
    logger.info("pi CLI: ok")


def check_opencode_cli() -> None:
    """Verify opencode CLI is installed. Auth is checked at first use."""
    if not shutil.which("opencode"):
        raise _exit(
            f"opencode CLI not found. Install it: {_AGENT_INSTALL_HINTS['opencode']}"
        )
    logger.info("opencode CLI: ok")


def check_ollama_mode(agent_name: str = "claude") -> None:
    """Validate `ollama launch <agent>` mode: both CLIs present, model available."""
    if not shutil.which(agent_name):
        raise _exit(
            f"{agent_name} CLI not found. Install it: {_AGENT_INSTALL_HINTS[agent_name]}"
        )
    if not shutil.which("ollama"):
        raise _exit("ollama CLI not found. Install it: https://ollama.com")
    model = os.environ.get("OLLAMA_MODEL", "").strip()
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
    logger.info("claude-pty mode: ok")


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


async def check_slack(app_token: str, user_token: str) -> str:
    """Verify Slack tokens are valid. Returns the user_id owning the user token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        data = resp.json()
        if not data.get("ok"):
            raise _exit(f"Invalid Slack user token: {data.get('error', 'unknown')}")
        user_id = data.get("user_id")
        if not user_id:
            raise _exit("Slack auth.test returned no user_id")
        logger.info(
            "Slack user token: %s (%s, %s)",
            data.get("user"),
            data.get("team"),
            user_id,
        )

        if not app_token.startswith("xapp-"):
            raise _exit("SLACK_APP_TOKEN must start with 'xapp-'")
        logger.info("Slack app token: format ok")
        return user_id


def check_gws_cli() -> None:
    """Verify gws CLI is installed and authenticated with Gmail scope."""
    if not shutil.which("gws"):
        raise _exit(
            "gws CLI not found. Install it: npm install -g @googleworkspace/cli"
        )
    result = subprocess.run(
        ["gws", "auth", "status"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise _exit(f"gws auth check failed: {result.stderr.strip()}")
    status = json.loads(result.stdout)
    if not status.get("token_valid"):
        raise _exit("gws token invalid. Run: gws auth login")
    if "gmail.googleapis.com" not in status.get("enabled_apis", []):
        raise _exit("Gmail API not enabled in GCP project. Enable it first.")
    logger.info("gws CLI: ok (user: %s)", status.get("user", "unknown"))


def _setup_logging() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def run_telegram() -> tuple[str, int]:
    """Validate env vars and tokens. Returns (token, allowed_user_id)."""
    _setup_logging()
    _raise_on_failures(checks.check_telegram(os.environ))
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed_user_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    check_backend()
    asyncio.run(check_telegram(token))
    return token, allowed_user_id


def run_slack() -> tuple[str, str, str, set[str], set[str], set[str], set[str]]:
    """Validate env vars and tokens.

    Returns (app_token, user_token, user_id, allowed_user_ids, blocked_user_ids,
    allowed_bot_ids, silent_sender_ids). user_id is resolved from Slack
    auth.test — no need to pass it via env.
    """
    _setup_logging()
    _raise_on_failures(checks.check_slack(os.environ))
    app_token = os.environ["SLACK_APP_TOKEN"]
    user_token = os.environ["SLACK_USER_TOKEN"]
    allowed_raw = os.environ.get("SLACK_ALLOWED_USER_IDS", "")
    allowed_user_ids = {uid.strip() for uid in allowed_raw.split(",") if uid.strip()}
    blocked_raw = os.environ.get("SLACK_BLOCKED_USER_IDS", "")
    blocked_user_ids = {uid.strip() for uid in blocked_raw.split(",") if uid.strip()}
    # No "*" wildcard here on purpose: it would let our own app's echoed posts
    # through and loop. Bot senders must be allowlisted by explicit bot_id.
    bot_raw = os.environ.get("SLACK_ALLOWED_BOT_IDS", "")
    allowed_bot_ids = {bid.strip() for bid in bot_raw.split(",") if bid.strip()}
    silent_raw = os.environ.get("SLACK_SILENT_SENDER_IDS", "")
    silent_sender_ids = {sid.strip() for sid in silent_raw.split(",") if sid.strip()}
    check_backend()
    user_id = asyncio.run(check_slack(app_token, user_token))
    logger.debug(
        "preflight: user_id=%s allowed_user_ids=%s blocked_user_ids=%s allowed_bot_ids=%s silent_sender_ids=%s",
        user_id,
        allowed_user_ids,
        blocked_user_ids,
        allowed_bot_ids,
        silent_sender_ids,
    )
    return (
        app_token,
        user_token,
        user_id,
        allowed_user_ids,
        blocked_user_ids,
        allowed_bot_ids,
        silent_sender_ids,
    )


def run_gmail() -> tuple[str, set[str]]:
    """Validate env vars and gws CLI. Returns (gcp_project, allowed_senders)."""
    _setup_logging()
    _raise_on_failures(checks.check_gmail(os.environ))
    gcp_project = os.environ["GMAIL_GCP_PROJECT"]
    allowed_raw = os.environ["GMAIL_ALLOWED_SENDERS"]
    allowed_senders = {s.strip() for s in allowed_raw.split(",") if s.strip()}
    check_backend()
    check_gws_cli()
    return gcp_project, allowed_senders
