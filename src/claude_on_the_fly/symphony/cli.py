"""claude-symphony entrypoint.

Usage:
    claude-symphony [config-path]

Default: ~/.claude-on-the-fly/symphony.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import orchestrator

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
DEFAULT_CONFIG = DATA_DIR / "symphony.yaml"


def _setup_logging(platform: str = "symphony") -> None:
    """Console respects LOG_LEVEL; file always DEBUG with daily rotation, 7-day retention."""
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format=log_fmt,
    )
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / f"{platform}.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_fmt))
    logging.getLogger().addHandler(file_handler)


async def _run(config_path: Path) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows
    await orchestrator.run_loop(config_path, stop)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="claude-symphony",
        description=(
            "Long-running daemon that polls a tracker and runs Claude Code in "
            "per-ticket sessions."
        ),
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=str(DEFAULT_CONFIG),
        help=f"Path to symphony.yaml (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args()
    config_path = Path(args.config).expanduser()

    if not config_path.exists():
        sys.stderr.write(f"config not found: {config_path}\n")
        sys.stderr.write(
            "See symphony.yaml.example and symphony-prompt.md.example at the repo root.\n"
        )
        return 2

    _setup_logging()
    logger.info("claude-symphony: config=%s", config_path)

    try:
        asyncio.run(_run(config_path))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
