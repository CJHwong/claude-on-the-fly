"""Preflight checks - validate tokens and tools before starting."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess

import httpx

logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    """Read a required env var or exit with a clear message."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def check_claude_cli() -> None:
    """Verify claude CLI is installed and authenticated."""
    if not shutil.which("claude"):
        raise SystemExit(
            "claude CLI not found. Install it: https://docs.anthropic.com/en/docs/claude-code"
        )
    result = subprocess.run(
        ["claude", "-p", "--output-format", "json", "ping"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        if "auth" in result.stderr.lower():
            raise SystemExit("claude CLI not authenticated. Run: claude auth login")
        raise SystemExit(
            f"claude CLI check failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    logger.info("claude CLI: ok")


async def check_telegram(token: str) -> None:
    """Verify Telegram bot token is valid."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        data = resp.json()
        if not data.get("ok"):
            raise SystemExit(
                f"Invalid Telegram bot token: {data.get('description', 'unknown error')}"
            )
        bot_name = data["result"]["username"]
        logger.info("Telegram bot: @%s", bot_name)


async def check_slack(app_token: str, user_token: str, user_id: str) -> None:
    """Verify Slack tokens are valid."""
    async with httpx.AsyncClient() as client:
        # Check user token
        resp = await client.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        data = resp.json()
        if not data.get("ok"):
            raise SystemExit(
                f"Invalid Slack user token: {data.get('error', 'unknown')}"
            )
        actual_user = data.get("user_id")
        if actual_user != user_id:
            raise SystemExit(
                f"SLACK_USER_ID mismatch: token belongs to {actual_user}, but config says {user_id}"
            )
        logger.info("Slack user token: %s (%s)", data.get("user"), data.get("team"))

        # Check app token format
        if not app_token.startswith("xapp-"):
            raise SystemExit("SLACK_APP_TOKEN must start with 'xapp-'")
        logger.info("Slack app token: format ok")


def check_gws_cli() -> None:
    """Verify gws CLI is installed and authenticated with Gmail scope."""
    if not shutil.which("gws"):
        raise SystemExit(
            "gws CLI not found. Install it: npm install -g @googleworkspace/cli"
        )
    result = subprocess.run(
        ["gws", "auth", "status"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise SystemExit(f"gws auth check failed: {result.stderr.strip()}")
    status = json.loads(result.stdout)
    if not status.get("token_valid"):
        raise SystemExit("gws token invalid. Run: gws auth login")
    if "gmail.googleapis.com" not in status.get("enabled_apis", []):
        raise SystemExit("Gmail API not enabled in GCP project. Enable it first.")
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
    token = require_env("TELEGRAM_BOT_TOKEN")
    raw_user_id = require_env("TELEGRAM_ALLOWED_USER_ID")
    try:
        allowed_user_id = int(raw_user_id)
    except ValueError:
        raise SystemExit(
            f"TELEGRAM_ALLOWED_USER_ID must be an integer, got: {raw_user_id!r}"
        )
    check_claude_cli()
    asyncio.run(check_telegram(token))
    return token, allowed_user_id


def run_slack() -> tuple[str, str, str, set[str]]:
    """Validate env vars and tokens. Returns (app_token, user_token, user_id, allowed_user_ids)."""
    _setup_logging()
    app_token = require_env("SLACK_APP_TOKEN")
    user_token = require_env("SLACK_USER_TOKEN")
    user_id = require_env("SLACK_USER_ID")
    allowed_raw = os.environ.get("SLACK_ALLOWED_USER_IDS", "")
    allowed_user_ids = {uid.strip() for uid in allowed_raw.split(",") if uid.strip()}
    logger.debug("preflight: user_id=%s allowed_user_ids=%s", user_id, allowed_user_ids)
    check_claude_cli()
    asyncio.run(check_slack(app_token, user_token, user_id))
    return app_token, user_token, user_id, allowed_user_ids


def run_gmail() -> tuple[str, set[str]]:
    """Validate env vars and gws CLI. Returns (gcp_project, allowed_senders)."""
    _setup_logging()
    gcp_project = require_env("GMAIL_GCP_PROJECT")
    allowed_raw = require_env("GMAIL_ALLOWED_SENDERS")
    allowed_senders = {s.strip() for s in allowed_raw.split(",") if s.strip()}
    if not allowed_senders:
        raise SystemExit(
            "GMAIL_ALLOWED_SENDERS must contain at least one email address"
        )
    check_claude_cli()
    check_gws_cli()
    return gcp_project, allowed_senders
