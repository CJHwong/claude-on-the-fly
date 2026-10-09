#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Cron producer: print one JSON line per new Gmail message from an allowed sender.

How to make it work:
  1. Install uv (https://docs.astral.sh/uv/) and the Google Workspace CLI `gws`, then
     run `gws auth login` as the user the cron daemon runs as. An OAuth app left in
     Google's "Testing" status expires its refresh token after 7 days; publish it, or
     schedule a check that alerts when `gws` stops answering.
  2. Seed once, so mail that is already unread does not all arrive on the first poll:
       uv run --script mail_poll.py --senders a@example.com --seed
  3. Check what a poll would print, without marking anything read:
       uv run --script mail_poll.py --senders a@example.com --dry-run
  4. Link ../ (the mail-handoff skill) into the agent's skills directory, and copy
     the entry in ../cron.yaml into ~/.claude-on-the-fly/cron.yaml.
  Only mail from --senders is touched. Everything else stays unread and is never
  printed, so a stranger cannot put text in front of the agent.

Each matching message is marked read before it is printed, and its id is kept in the
state file. Either one alone stops a repeat; together a crash between them cannot.
The `key` is the Gmail message id, so each message becomes one job. If the mailbox
cannot be listed, it exits 1 and the cron daemon alerts. A single message that fails
stays unread and is retried on the next poll; the reason goes to the entry's log.
"""

from __future__ import annotations

import argparse
import email.utils
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

STATE = Path.home() / ".claude-on-the-fly" / "state" / "mail-poll.json"
KEEP_IDS = 2000  # processed ids kept in the state file; older ones are long since read


def gws(*args: str) -> dict:
    """One `gws gmail ...` call, parsed. gws can print log lines before its JSON."""
    binary = shutil.which("gws")
    if not binary:
        raise RuntimeError("gws not found on PATH")
    proc = subprocess.run(
        [binary, "gmail", *args], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gws {args[:3]} failed: {proc.stderr.strip()[:300]}")
    out = proc.stdout.strip()
    start = out.find("{")
    if start < 0:
        raise RuntimeError(f"gws printed no JSON: {out[:200]}")
    return json.JSONDecoder().raw_decode(out[start:])[0]


def unread_ids(query: str, limit: int) -> list[str]:
    params = {"userId": "me", "q": query, "maxResults": limit}
    listing = gws("users", "messages", "list", "--params", json.dumps(params))
    return [message["id"] for message in listing.get("messages") or []]


def metadata(message_id: str) -> dict[str, str]:
    params = {
        "userId": "me",
        "id": message_id,
        "format": "metadata",
        "metadataHeaders": ["From", "Subject", "Date"],
    }
    data = gws("users", "messages", "get", "--params", json.dumps(params))
    headers = {
        header["name"]: header.get("value", "")
        for header in data.get("payload", {}).get("headers", [])
    }
    return {
        "from": sender_address(headers.get("From", "")),
        "subject": headers.get("Subject", "") or "(no subject)",
        "date": headers.get("Date", ""),
        "snippet": data.get("snippet", ""),
    }


def sender_address(header: str) -> str:
    return next(
        (addr.lower() for _, addr in email.utils.getaddresses([header]) if addr), ""
    )


def mark_read(message_id: str) -> None:
    gws(
        "users", "messages", "modify",
        "--params", json.dumps({"userId": "me", "id": message_id}),
        "--json", json.dumps({"removeLabelIds": ["UNREAD"]}),
    )  # fmt: skip


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("processed", {})


def save_state(path: Path, processed: dict[str, str]) -> None:
    newest = dict(sorted(processed.items(), key=lambda pair: pair[1])[-KEEP_IDS:])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"processed": newest}, indent=1))


def poll(args: argparse.Namespace, processed: dict[str, str]) -> list[dict]:
    """New allowed mail, marked read. A message that fails is skipped and stays unread,
    so the next poll retries it; failing the whole poll would drop the ones already
    marked read, because the daemon ignores the output of a producer that exits 1."""
    items = []
    for message_id in unread_ids(args.query, args.limit):
        if message_id in processed:
            continue
        try:
            meta = metadata(message_id)
            if meta["from"] not in args.senders:
                continue
            if not args.dry_run:
                mark_read(message_id)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            print(
                f"mail_poll: skipped {message_id}, retrying next poll: {exc}",
                file=sys.stderr,
            )
            continue
        processed[message_id] = datetime.now(UTC).isoformat(timespec="seconds")
        items.append({"key": message_id, **meta})
    return items


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--senders",
        required=True,
        type=lambda raw: {s.strip().lower() for s in raw.split(",") if s.strip()},
        help="comma-separated addresses; mail from anyone else is ignored",
    )
    parser.add_argument("--query", default="is:unread", help="Gmail search query")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument(
        "--seed", action="store_true", help="record current unread mail, print nothing"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print, but mark nothing read"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        processed = load_state(args.state)
        if args.seed:
            now = datetime.now(UTC).isoformat(timespec="seconds")
            for message_id in unread_ids(args.query, 500):
                processed.setdefault(message_id, now)
            save_state(args.state, processed)
            print(f"seeded {len(processed)} ids into {args.state}", file=sys.stderr)
            return 0
        items = poll(args, processed)
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        # Nothing was marked read yet when the listing or the state file fails.
        print(f"mail_poll: {exc}", file=sys.stderr)
        return 1
    if items and not args.dry_run:
        try:
            save_state(args.state, processed)
        except OSError as exc:
            # The messages are already marked read; the unread flag alone stops a repeat.
            print(f"mail_poll: could not save {args.state}: {exc}", file=sys.stderr)
    for item in items:
        print(json.dumps(item, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
