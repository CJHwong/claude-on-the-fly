"""Cron-driven producer. Fires entries on a schedule and puts work in the job queue.

This daemon runs shell and nothing else. It never calls an agent: work that needs
one becomes a `Job`, and the jobs worker runs it. That split is the whole design —
the daemon decides *what* to work on, the worker decides *how long* and *how many
at once*.

An entry is one of three shapes:

- `prompt` / `prompt_file` alone: one job per fire, keyed to the entry, with no
  item context and a fresh session each time.
- `command` plus a prompt: the command's stdout enumerates work items as JSON,
  one object per line, and each becomes its own keyed job whose session resumes
  across fires. This is how tracker polling is expressed, with no tracker code.
- `command` alone: a subprocess run for its side effects. No job, no agent — the
  daemon runs it here and logs the outcome, because putting a shell script through
  an at-least-once queue would re-run its side effects after a crash.

Whether an item gets *more* work is decided by the next fire's query, never by
state held here. An item that stops being emitted stops being worked, which is
what makes cancellation, reconciliation, and terminal-state tracking unnecessary
rather than merely absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import yaml
from croniter import croniter
from liquid import Environment, StrictUndefined
from liquid.exceptions import LiquidError, LiquidSyntaxError

from claude_on_the_fly import logs
from claude_on_the_fly.jobs.core import Job, JobQueue
from claude_on_the_fly.jobs.key_state import (
    DEFAULT_MAX_FIRES,
    KeyStateStore,
    fingerprint,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
LOG_DIR = DATA_DIR / "logs"
DEFAULT_CONFIG = DATA_DIR / "cron.yaml"
DEFAULT_TIMEOUT = 1800
MAX_TIMEOUT = 86400
DEFAULT_MAX_CONCURRENT = 1
NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
# A producer command only has to print a work list, so it gets a fixed short
# limit rather than the entry's `timeout` — that one bounds the *agent* run each
# emitted item turns into, and is measured in tens of minutes.
PRODUCER_TIMEOUT_S = 120
# Longest producer stdout we will parse, so a command that accidentally streams a
# log file cannot exhaust memory.
MAX_PRODUCER_BYTES = 4 * 1024 * 1024

_LIQUID_ENV = Environment(undefined=StrictUndefined)

EXAMPLE_YAML = """\
# Cron entries. Each fires on a schedule; output goes to
# logs/cron-<name>-<host>-<date>.log. The file auto-reloads when you save it.
#
# Every entry needs:
#   name     letters, digits, '-' and '_' only
#   cron     standard 5-field cron expression
# and at least one of:
#   prompt        text run through an agent (Liquid template)
#   prompt_file   path to that text instead, read fresh on every fire so you can
#                 edit it without touching this file. Relative to this file.
#   command       shell command. WITH a prompt it is a producer: each line of its
#                 stdout must be a JSON object, and each becomes its own job.
#                 WITHOUT a prompt it just runs, for its side effects.
# Optional:
#   timeout          seconds for the agent run (or for a bare command); default
#                    1800, max 86400
#   max_concurrent   how many of THIS entry's items may be outstanding at once;
#                    default 1. Only meaningful for a producer.
#   max_fires        fires against an unchanged item before it is parked;
#                    default 3, 0 disables
#
# A producer's JSON objects must each carry a "key" identifying the item, and
# should carry a field that MOVES when the work moves (Jira's `updated`) — the
# key parks after max_fires if nothing about it changes.
#
# Example:
#   entries:
#     - name: morning-digest
#       cron: "0 9 * * *"
#       prompt: "Summarise overnight PRs and post the highlights."
#
#     - name: jira
#       cron: "*/2 * * * *"
#       max_concurrent: 3
#       prompt_file: ./prompts/jira.md
#       command: |
#         acli jira workitem search --jql 'assignee = currentUser() AND
#           status not in (Done)' --json
#           | jq -c '.[] | {key, title: .fields.summary,
#                           status: .fields.status.name, updated: .fields.updated}'

entries: []  # add at least one entry — an empty list won't load
"""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CronEntry:
    name: str
    cron: str
    prompt: str | None = None
    prompt_file: Path | None = None
    command: str | None = None
    timeout: int = DEFAULT_TIMEOUT
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    max_fires: int = DEFAULT_MAX_FIRES

    @property
    def kind(self) -> Literal["prompt", "producer", "command"]:
        if self.command is None:
            return "prompt"
        return "command" if not self.has_prompt else "producer"

    @property
    def has_prompt(self) -> bool:
        return self.prompt is not None or self.prompt_file is not None

    def prompt_source(self) -> str:
        """The template text for this fire.

        Read from disk every time rather than cached on mtime: a fire happens at
        most once a minute, so one read costs nothing, and it means editing the
        file takes effect on the next fire with no reload machinery to get wrong.
        """
        if self.prompt_file is not None:
            return self.prompt_file.read_text(encoding="utf-8")
        return self.prompt or ""


@dataclass
class EntryState:
    entry: CronEntry
    next_fire: datetime


def _require_str(data: dict[str, object], field: str, where: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: {field!r} must be a non-empty string")
    return value


def _positive_int(data: dict[str, object], field: str, default: int, where: str) -> int:
    value = data.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where}: {field!r} must be a non-negative int")
    return value


def _validate_entry(
    raw: object, index: int, seen: set[str], config_dir: Path
) -> CronEntry:
    where = f"entries[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be a mapping")
    data = cast(dict[str, object], raw)

    name = _require_str(data, "name", where)
    if not all(c in NAME_CHARS for c in name):
        raise ValueError(f"{where}: name {name!r} must match [A-Za-z0-9_-]")
    if name in seen:
        raise ValueError(f"{where}: duplicate name {name!r}")
    seen.add(name)
    where = f"{where} ({name})"

    cron_expr = _require_str(data, "cron", where)
    try:
        croniter(cron_expr, datetime.now())
    except (ValueError, KeyError) as exc:
        raise ValueError(f"{where}: invalid cron {cron_expr!r}: {exc}") from exc

    has_prompt = "prompt" in data
    has_file = "prompt_file" in data
    has_command = "command" in data
    if has_prompt and has_file:
        raise ValueError(f"{where}: specify 'prompt' OR 'prompt_file', not both")
    if not (has_prompt or has_file or has_command):
        raise ValueError(f"{where}: needs 'prompt', 'prompt_file', or 'command'")

    prompt = _require_str(data, "prompt", where) if has_prompt else None
    prompt_file: Path | None = None
    if has_file:
        raw_path = _require_str(data, "prompt_file", where)
        prompt_file = Path(os.path.expanduser(raw_path))
        if not prompt_file.is_absolute():
            prompt_file = (config_dir / prompt_file).resolve()
        if not prompt_file.is_file():
            raise ValueError(f"{where}: prompt_file not found at {prompt_file}")

    command = _require_str(data, "command", where) if has_command else None

    timeout = _positive_int(data, "timeout", DEFAULT_TIMEOUT, where)
    if not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"{where}: 'timeout' must be 1..{MAX_TIMEOUT}")
    max_concurrent = _positive_int(
        data, "max_concurrent", DEFAULT_MAX_CONCURRENT, where
    )
    if max_concurrent < 1:
        raise ValueError(f"{where}: 'max_concurrent' must be at least 1")
    if max_concurrent > 1 and command is None:
        # Silently ignoring it would leave somebody waiting for parallelism that
        # the dedup rule makes impossible: without a producer there is only ever
        # one item, the entry itself.
        raise ValueError(
            f"{where}: 'max_concurrent' above 1 needs a 'command' to produce items"
        )
    max_fires = _positive_int(data, "max_fires", DEFAULT_MAX_FIRES, where)

    entry = CronEntry(
        name=name,
        cron=cron_expr,
        prompt=prompt,
        prompt_file=prompt_file,
        command=command,
        timeout=timeout,
        max_concurrent=max_concurrent,
        max_fires=max_fires,
    )
    _validate_template(entry, where)
    return entry


def _validate_template(entry: CronEntry, where: str) -> None:
    """Compile the entry's template now, so a typo fails at load instead of at 3am.

    A plain entry is also dry-rendered against an empty context, which catches the
    one mistake load-time compilation cannot: referring to `item` in an entry that
    has no producer to supply one. A producer's own item shape is not knowable
    here, so those get compilation only.
    """
    if not entry.has_prompt:
        return
    try:
        source = entry.prompt_source()
    except OSError as exc:
        raise ValueError(f"{where}: cannot read prompt_file: {exc}") from exc
    try:
        template = _LIQUID_ENV.from_string(source)
    except LiquidSyntaxError as exc:
        raise ValueError(f"{where}: prompt template does not compile: {exc}") from exc
    if entry.kind == "prompt":
        try:
            template.render()
        except LiquidError as exc:
            raise ValueError(
                f"{where}: prompt uses a variable this entry cannot supply "
                f"(add a 'command' to produce items): {exc}"
            ) from exc


def load_config(path: Path) -> list[CronEntry]:
    """Parse and validate a YAML config. Raises ValueError on any issue."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping with an 'entries' key")
    entries_raw = cast(dict[str, object], raw).get("entries")
    if not isinstance(entries_raw, list):
        raise ValueError("'entries' must be a list")
    if not entries_raw:
        raise ValueError("'entries' must contain at least one entry")
    seen: set[str] = set()
    config_dir = path.parent
    return [
        _validate_entry(item, index, seen, config_dir)
        for index, item in enumerate(entries_raw)
    ]


def next_fire(cron_expr: str, now: datetime) -> datetime:
    return croniter(cron_expr, now).get_next(datetime)


# ---------------------------------------------------------------------------
# Producer output
# ---------------------------------------------------------------------------


def parse_items(stdout: str, entry_name: str) -> list[dict[str, Any]]:
    """The JSON objects a producer printed, one per line.

    A bad line costs that line and nothing else: the rest of the fire proceeds.
    Silently dropping it would be worse than the parse error it came from, so each
    one is logged with the offending text.
    """
    items: list[dict[str, Any]] = []
    for lineno, line in enumerate(stdout.splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            logger.error(
                "cron %s: line %d is not JSON (%s): %.200s",
                entry_name,
                lineno,
                exc,
                text,
            )
            continue
        if not isinstance(parsed, dict):
            logger.error(
                "cron %s: line %d is %s, not a JSON object: %.200s. "
                "A producer emitting an array should pipe through `jq -c '.[]'`.",
                entry_name,
                lineno,
                type(parsed).__name__,
                text,
            )
            continue
        key = parsed.get("key")
        if not isinstance(key, str) or not key.strip():
            logger.error(
                "cron %s: line %d has no usable 'key', so it cannot be deduplicated "
                "or resumed; skipping: %.200s",
                entry_name,
                lineno,
                text,
            )
            continue
        items.append(parsed)
    return items


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log_path(entry_name: str) -> Path:
    """`logs/cron-<entry>-<host>-<date>.log`; `cron-<entry>` is the role."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return logs.log_file(f"cron-{entry_name}", directory=LOG_DIR)


def append_log(entry_name: str, block: str) -> None:
    try:
        with log_path(entry_name).open("a", encoding="utf-8") as handle:
            handle.write(block)
    except OSError as exc:
        # The daemon log still has the story; losing the per-entry copy is not
        # worth dropping a fire over.
        logger.warning("cron %s: could not write its log: %s", entry_name, exc)


def _log_header(entry_name: str, kind: str, detail: str) -> None:
    stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    append_log(entry_name, f"\n=== {stamp} fire ({kind}) ===\n{detail}\n")


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class CronDaemon:
    """Fires due entries and enqueues the work they produce."""

    def __init__(self, config_path: Path, queue: JobQueue, key_state: KeyStateStore):
        self._config_path = config_path
        self._queue = queue
        self._key_state = key_state
        self._state: dict[str, EntryState] = {}
        self._mtime = 0.0
        self._stop = asyncio.Event()
        self._command_tasks: set[asyncio.Task] = set()

    # --- config ---

    def reload(self) -> tuple[set[str], set[str], set[str]]:
        entries = load_config(self._config_path)
        now = datetime.now()
        fresh: dict[str, EntryState] = {}
        for entry in entries:
            previous = self._state.get(entry.name)
            if previous is not None and previous.entry.cron == entry.cron:
                # Keep the pending fire time so editing an unrelated field does
                # not silently reschedule the entry.
                fresh[entry.name] = EntryState(
                    entry=entry, next_fire=previous.next_fire
                )
            else:
                fresh[entry.name] = EntryState(
                    entry=entry, next_fire=next_fire(entry.cron, now)
                )
        added = set(fresh) - set(self._state)
        removed = set(self._state) - set(fresh)
        changed = {
            name
            for name in set(fresh) & set(self._state)
            if self._state[name].entry != fresh[name].entry
        }
        self._state = fresh
        self._mtime = self._config_path.stat().st_mtime
        return added, removed, changed

    def _maybe_reload(self) -> None:
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError as exc:
            logger.warning("cron: config stat failed: %s", exc)
            return
        if mtime == self._mtime:
            return
        try:
            added, removed, changed = self.reload()
        except ValueError as exc:
            logger.error("cron: config reload failed, keeping prior entries: %s", exc)
            return
        logger.info(
            "cron: reloaded (+%d -%d ~%d)", len(added), len(removed), len(changed)
        )

    # --- loop ---

    async def run(self) -> None:
        self.reload()
        self._print_summary()
        while not self._stop.is_set():
            await self._sleep_to_next_minute()
            if self._stop.is_set():
                break
            self._maybe_reload()
            now = datetime.now()
            for state in list(self._state.values()):
                if state.next_fire <= now:
                    state.next_fire = next_fire(state.entry.cron, now)
                    await self._fire(state.entry)

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._command_tasks):
            task.cancel()
        if self._command_tasks:
            await asyncio.gather(*self._command_tasks, return_exceptions=True)

    async def _sleep_to_next_minute(self) -> None:
        now = datetime.now()
        wait = 60 - now.second - now.microsecond / 1_000_000
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=wait)

    # --- firing ---

    async def _fire(self, entry: CronEntry) -> None:
        logger.info("cron: firing %s (%s)", entry.name, entry.kind)
        try:
            if entry.kind == "command":
                self._spawn_command(entry)
            elif entry.kind == "prompt":
                self._enqueue_plain(entry)
            else:
                await self._fire_producer(entry)
        except Exception:
            # One broken entry must not take the daemon down; the next fire of
            # every other entry is still due.
            logger.exception("cron %s: fire failed", entry.name)

    def _enqueue_plain(self, entry: CronEntry) -> None:
        """One job per fire, keyed to the entry so two cannot overlap."""
        key = entry.name
        if self._queue.count_unfinished(entry.name) > 0:
            logger.info(
                "cron %s: previous run still outstanding, skipping this fire",
                entry.name,
            )
            return
        try:
            prompt = _LIQUID_ENV.from_string(entry.prompt_source()).render()
        except (OSError, LiquidError) as exc:
            logger.error("cron %s: cannot render prompt: %s", entry.name, exc)
            return
        _log_header(entry.name, "prompt", f"> {prompt}")
        # No session_key: each fire of a scheduled prompt starts clean, which is
        # what it meant before keys existed and what a daily digest wants.
        self._enqueue(entry, key=key, session_key=None, prompt=prompt)

    async def _fire_producer(self, entry: CronEntry) -> None:
        assert entry.command is not None
        stdout, rc = await self._run_command(
            entry.command, timeout=PRODUCER_TIMEOUT_S, capture=True
        )
        if rc != 0:
            logger.error(
                "cron %s: producer exited %s; skipping this fire%s",
                entry.name,
                rc,
                f": {stdout.strip()[:400]}" if stdout.strip() else "",
            )
            return
        items = parse_items(stdout, entry.name)
        if not items:
            logger.debug("cron %s: producer emitted no items", entry.name)
            return
        self._admit(entry, items)

    def _admit(self, entry: CronEntry, items: Iterable[dict[str, Any]]) -> None:
        """Enqueue what this entry is allowed to take on right now.

        Three gates, cheapest first: already queued, over the entry's cap, or held
        back by the key's own history (backing off after a failure, or parked for
        making no progress). Each rejection is logged, because a silent skip is
        indistinguishable from a producer that found nothing.
        """
        outstanding = self._queue.count_unfinished(entry.name)
        template = _LIQUID_ENV.from_string(entry.prompt_source())
        render_failures = 0
        enqueued = 0

        for item in items:
            item_key = str(item["key"])
            key = f"{entry.name}/{item_key}"

            if self._queue.count_unfinished(entry.name, item_key) > 0:
                logger.debug("cron %s: %s already queued", entry.name, item_key)
                continue
            if outstanding >= entry.max_concurrent:
                logger.info(
                    "cron %s: at max_concurrent=%d, deferring %s to a later fire",
                    entry.name,
                    entry.max_concurrent,
                    item_key,
                )
                continue

            reason = self._key_state.should_skip(
                key, fingerprint(item), max_fires=entry.max_fires
            )
            if reason:
                logger.info("cron %s: skipping %s: %s", entry.name, item_key, reason)
                continue

            try:
                prompt = template.render(item=item)
            except LiquidError as exc:
                render_failures += 1
                logger.warning(
                    "cron %s: cannot render prompt for %s: %s",
                    entry.name,
                    item_key,
                    exc,
                )
                continue

            _log_header(entry.name, f"producer item {item_key}", f"> {prompt}")
            self._enqueue(entry, key=key, session_key=key, prompt=prompt)
            self._key_state.record_fire(key, fingerprint(item))
            outstanding += 1
            enqueued += 1

        if render_failures and enqueued == 0:
            # Every item failing the same way is a template that does not match
            # what this producer emits, which is a config bug rather than one
            # item's bad data.
            logger.error(
                "cron %s: no item could be rendered (%d failed) — the prompt "
                "references fields this producer does not emit",
                entry.name,
                render_failures,
            )

    def _enqueue(
        self, entry: CronEntry, *, key: str, session_key: str | None, prompt: str
    ) -> None:
        job = Job(
            id=f"{time.time_ns()}-{uuid4().hex[:8]}",
            prompt=prompt,
            origin={"kind": "cron", "entry": entry.name},
            key=key,
            session_key=session_key,
            timeout=float(entry.timeout),
            platform="cron",
        )
        self._queue.enqueue(job)
        logger.info("cron %s: queued %s as %s", entry.name, key, job.id)

    # --- side-effect commands ---

    def _spawn_command(self, entry: CronEntry) -> None:
        task = asyncio.create_task(self._run_side_effect(entry))
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)

    async def _run_side_effect(self, entry: CronEntry) -> None:
        assert entry.command is not None
        started = datetime.now()
        _log_header(entry.name, "command", f"$ {entry.command}")
        try:
            with log_path(entry.name).open("a", encoding="utf-8") as handle:
                proc = await asyncio.create_subprocess_shell(
                    entry.command, stdout=handle, stderr=asyncio.subprocess.STDOUT
                )
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout=entry.timeout)
                    note = ""
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    rc = proc.returncode
                    note = f" (timed out after {entry.timeout}s)"
        except asyncio.CancelledError:
            append_log(entry.name, "*** cancelled during shutdown ***\n")
            raise
        except OSError as exc:
            logger.exception("cron %s: command failed to start", entry.name)
            append_log(entry.name, f"=== could not start: {exc} ===\n")
            return
        elapsed = (datetime.now() - started).total_seconds()
        append_log(
            entry.name, f"=== done exit={rc} duration={elapsed:.1f}s{note} ===\n"
        )

    async def _run_command(
        self, command: str, *, timeout: float, capture: bool
    ) -> tuple[str, int | None]:
        """Run a shell command, optionally capturing stdout. Returns (stdout, rc)."""
        pipe = asyncio.subprocess.PIPE if capture else None
        proc = await asyncio.create_subprocess_shell(
            command, stdout=pipe, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return f"timed out after {timeout}s", None
        if err:
            # A producer's stderr is where its own diagnostics go, and swallowing
            # them is what makes "it just stopped finding tickets" unanswerable.
            logger.warning(
                "cron: producer stderr: %s",
                err.decode(errors="replace").strip()[:1000],
            )
        text = out.decode(errors="replace") if out else ""
        if len(text) > MAX_PRODUCER_BYTES:
            logger.error(
                "cron: producer printed %d bytes, truncating to %d",
                len(text),
                MAX_PRODUCER_BYTES,
            )
            text = text[:MAX_PRODUCER_BYTES]
        return text, proc.returncode

    # --- summary ---

    def _print_summary(self) -> None:
        print(
            f"Cron started — {len(self._state)} entries from {self._config_path}",
            file=sys.stderr,
        )
        rows = sorted(self._state.values(), key=lambda s: s.next_fire)
        if not rows:
            return
        name_width = max(len(s.entry.name) for s in rows)
        cron_width = max(len(s.entry.cron) for s in rows)
        for state in rows:
            print(
                f"  {state.entry.name:<{name_width}}  "
                f"{state.entry.cron:<{cron_width}}  "
                f"{state.entry.kind:<8}  next: {state.next_fire:%a %H:%M}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:  # pragma: no cover
    import argparse
    import signal

    from dotenv import load_dotenv

    from claude_on_the_fly.heartbeat import HeartbeatWriter
    from claude_on_the_fly.jobs.registry import make_queue
    from claude_on_the_fly.preflight import setup_daemon_logging

    parser = argparse.ArgumentParser(prog="claude-cron")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"YAML config path (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args()

    load_dotenv()
    setup_daemon_logging("cron")

    if not args.config.is_file():
        raise SystemExit(f"config not found: {args.config}")
    try:
        load_config(args.config)
    except ValueError as exc:
        raise SystemExit(f"config error: {exc}") from exc

    queue = make_queue()
    daemon = CronDaemon(
        config_path=args.config,
        queue=queue,
        key_state=KeyStateStore(DATA_DIR / "jobs"),
    )

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(
                    sig, lambda: asyncio.ensure_future(daemon.stop())
                )
        heartbeat = HeartbeatWriter("cron")
        beat = asyncio.create_task(heartbeat.run())
        try:
            await daemon.run()
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            with contextlib.suppress(FileNotFoundError):
                heartbeat.path.unlink()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
