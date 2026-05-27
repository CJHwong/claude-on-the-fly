"""Cron-driven scheduler frontend. Reads jobs from a YAML file and fires them
on a cron schedule. Prompt jobs go through the shared Orchestrator (fresh
session per fire); script jobs run as subprocesses. All output is appended to
per-job log files. Config auto-reloads on mtime change."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, cast

import yaml
from croniter import croniter

from claude_on_the_fly.agent import Response
from claude_on_the_fly.protocol import Frontend

if TYPE_CHECKING:
    from claude_on_the_fly.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".claude-on-the-fly"
LOG_DIR = DATA_DIR / "logs"
DEFAULT_CONFIG = DATA_DIR / "schedule.yaml"
DEFAULT_TIMEOUT = 1800
MAX_TIMEOUT = 86400
NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

# Commented reference config. Seeded into a missing schedule.yaml and reused as
# the header when documenting an existing one. Lives here, next to JobSpec and
# load_config, so the example and the schema can't drift apart.
EXAMPLE_YAML = """\
# Scheduler jobs. Each fires on a cron schedule; output is appended to
# logs/schedule-<name>.log. The file auto-reloads when you save it.
#
# Every job needs:
#   name     letters, digits, '-' and '_' only
#   cron     standard 5-field cron expression
#   prompt | script   exactly one:
#     prompt  text run through the agent (a fresh session per fire)
#     script  absolute path to an executable run as a subprocess
# Optional:
#   args     list of string args (script jobs only)
#   timeout  seconds; default 1800, max 86400
#
# Example:
#   jobs:
#     - name: morning-digest
#       cron: "0 9 * * *"
#       prompt: "Summarise overnight PRs and post the highlights."
#       timeout: 1800
#     - name: cleanup
#       cron: "*/30 * * * *"
#       script: /absolute/path/to/cleanup.sh
#       args: ["--verbose"]

jobs: []  # add at least one job — an empty list won't load
"""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobSpec:
    name: str
    cron: str
    prompt: str | None = None
    script: Path | None = None
    args: tuple[str, ...] = ()
    timeout: int = DEFAULT_TIMEOUT

    @property
    def kind(self) -> Literal["prompt", "script"]:
        return "prompt" if self.prompt is not None else "script"

    @property
    def chat_id(self) -> int:
        # Stable int per name. Survives reload and restart. Matches the
        # sha256-based pattern used by slack.py and gmail.py for chat_ids.
        return int(hashlib.sha256(self.name.encode()).hexdigest()[:16], 16)


@dataclass
class JobState:
    spec: JobSpec
    next_fire: datetime


def _validate_job(raw: object, index: int, seen: set[str]) -> JobSpec:
    prefix = f"jobs[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{prefix}: must be a mapping")
    data = cast(dict[str, object], raw)

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{prefix}: 'name' is required and must be a string")
    if not all(c in NAME_CHARS for c in name):
        raise ValueError(f"{prefix}: name {name!r} must match [A-Za-z0-9_-]")
    if name in seen:
        raise ValueError(f"{prefix}: duplicate name {name!r}")
    seen.add(name)

    cron_expr = data.get("cron")
    if not isinstance(cron_expr, str) or not cron_expr:
        raise ValueError(f"{prefix} ({name}): 'cron' required, must be a string")
    try:
        croniter(cron_expr, datetime.now())
    except (ValueError, KeyError) as exc:
        raise ValueError(f"{prefix} ({name}): invalid cron {cron_expr!r}: {exc}")

    has_prompt = "prompt" in data
    has_script = "script" in data
    if has_prompt and has_script:
        raise ValueError(f"{prefix} ({name}): specify 'prompt' OR 'script', not both")
    if not has_prompt and not has_script:
        raise ValueError(f"{prefix} ({name}): must specify 'prompt' or 'script'")

    prompt: str | None = None
    script: Path | None = None
    args: tuple[str, ...] = ()

    if has_prompt:
        p = data["prompt"]
        if not isinstance(p, str) or not p.strip():
            raise ValueError(f"{prefix} ({name}): 'prompt' must be a non-empty string")
        prompt = p
    else:
        s = data["script"]
        if not isinstance(s, str):
            raise ValueError(f"{prefix} ({name}): 'script' must be a string path")
        script = Path(os.path.expanduser(s))
        if not script.is_file():
            raise ValueError(f"{prefix} ({name}): script not found at {script}")
        raw_args = data.get("args", [])
        if not isinstance(raw_args, list) or not all(
            isinstance(a, str) for a in raw_args
        ):
            raise ValueError(f"{prefix} ({name}): 'args' must be a list of strings")
        args = tuple(str(a) for a in raw_args)

    timeout = data.get("timeout", DEFAULT_TIMEOUT)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or timeout <= 0
        or timeout > MAX_TIMEOUT
    ):
        raise ValueError(
            f"{prefix} ({name}): 'timeout' must be a positive int <= {MAX_TIMEOUT}"
        )

    return JobSpec(
        name=name,
        cron=cron_expr,
        prompt=prompt,
        script=script,
        args=args,
        timeout=timeout,
    )


def load_config(path: Path) -> list[JobSpec]:
    """Parse and validate a YAML config. Raises ValueError on any issue."""
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping with a 'jobs' key")
    jobs_raw = cast(dict[str, object], raw).get("jobs")
    if not isinstance(jobs_raw, list):
        raise ValueError("'jobs' must be a list")
    if not jobs_raw:
        raise ValueError("'jobs' must contain at least one entry")
    seen: set[str] = set()
    return [_validate_job(j, i, seen) for i, j in enumerate(jobs_raw)]


def next_fire(cron_expr: str, now: datetime) -> datetime:
    return croniter(cron_expr, now).get_next(datetime)


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------


def _log_path(name: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"schedule-{name}.log"


def _append(name: str, block: str) -> None:
    with _log_path(name).open("a") as f:
        f.write(block)


def _log_fire_header(name: str, kind: str, detail: str) -> None:
    """Write the opening block for one fire: timestamp, kind, and prompt/cmd."""
    ts = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    _append(name, f"\n=== {ts} fire ({kind}) ===\n{detail}\n")


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------


class SchedulerFrontend(Frontend):
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._on_message: Callable[[int, str], Awaitable[None]] | None = None
        self._orch: Orchestrator | None = None
        self._state: dict[str, JobState] = {}
        self._chat_to_name: dict[int, str] = {}
        self._mtime: float = 0.0
        self._stop = asyncio.Event()
        self._script_tasks: set[asyncio.Task] = set()

    # --- Reload ---

    def _reload(self) -> tuple[set[str], set[str], set[str]]:
        """Re-parse the config and merge into state. Returns (added, removed, modified)."""
        specs = load_config(self._config_path)
        now = datetime.now()
        new_state: dict[str, JobState] = {}
        for spec in specs:
            prev = self._state.get(spec.name)
            if prev and prev.spec == spec:
                new_state[spec.name] = prev
            elif prev and prev.spec.cron == spec.cron:
                new_state[spec.name] = JobState(spec=spec, next_fire=prev.next_fire)
            else:
                new_state[spec.name] = JobState(
                    spec=spec, next_fire=next_fire(spec.cron, now)
                )

        added = set(new_state) - set(self._state)
        removed = set(self._state) - set(new_state)
        modified = {
            n
            for n in set(new_state) & set(self._state)
            if self._state[n].spec != new_state[n].spec
        }

        self._state = new_state
        self._chat_to_name = {s.spec.chat_id: name for name, s in new_state.items()}
        self._mtime = self._config_path.stat().st_mtime
        return added, removed, modified

    def _maybe_reload(self) -> None:
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError as exc:
            logger.warning("config stat failed: %s", exc)
            return
        if mtime == self._mtime:
            return
        try:
            added, removed, modified = self._reload()
        except (ValueError, yaml.YAMLError) as exc:
            logger.error("config reload failed, keeping prior state: %s", exc)
            return
        logger.info("reloaded: +%d -%d ~%d", len(added), len(removed), len(modified))

    # --- Main loop ---

    async def start(self, on_message: Callable[[int, str], Awaitable[None]]) -> None:
        self._on_message = on_message
        self._reload()
        self._print_summary()
        await self._loop()

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._script_tasks):
            task.cancel()
        if self._script_tasks:
            await asyncio.gather(*self._script_tasks, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep_to_next_minute()
            if self._stop.is_set():
                break
            self._maybe_reload()
            now = datetime.now()
            for state in list(self._state.values()):
                if state.next_fire <= now:
                    await self._fire(state.spec)
                    state.next_fire = next_fire(state.spec.cron, now)

    async def _sleep_to_next_minute(self) -> None:
        now = datetime.now()
        wait_s = 60 - now.second - now.microsecond / 1_000_000
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            pass

    # --- Dispatch ---

    async def _fire(self, spec: JobSpec) -> None:
        logger.info("firing %s (%s)", spec.name, spec.kind)
        if spec.kind == "prompt":
            await self._fire_prompt(spec)
        else:
            self._spawn_script(spec)

    async def _fire_prompt(self, spec: JobSpec) -> None:
        if self._on_message is None or self._orch is None or spec.prompt is None:
            return
        self._orch.reset_session(spec.chat_id)
        _log_fire_header(spec.name, "prompt", f"> {spec.prompt}")
        await self._on_message(spec.chat_id, spec.prompt)

    def _spawn_script(self, spec: JobSpec) -> None:
        task = asyncio.create_task(self._run_script(spec))
        self._script_tasks.add(task)
        task.add_done_callback(self._script_tasks.discard)

    async def _run_script(self, spec: JobSpec) -> None:
        assert spec.script is not None
        start = datetime.now()
        cmd_display = " ".join([str(spec.script), *spec.args])
        _log_fire_header(spec.name, "script", f"$ {cmd_display}")

        proc: asyncio.subprocess.Process | None = None
        rc: int | None = None
        note = ""
        try:
            with _log_path(spec.name).open("a") as f:
                proc = await asyncio.create_subprocess_exec(
                    str(spec.script),
                    *spec.args,
                    stdout=f,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout=spec.timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    rc = proc.returncode
                    note = f"timed out after {spec.timeout}s"
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            _append(spec.name, "*** cancelled during shutdown ***\n")
            raise
        except Exception as exc:
            logger.exception("%s: script failed", spec.name)
            note = f"error: {exc}"

        duration = (datetime.now() - start).total_seconds()
        trailer = f" ({note})" if note else ""
        _append(
            spec.name,
            f"=== done exit={rc} duration={duration:.1f}s{trailer} ===\n",
        )

    # --- Summary ---

    def _print_summary(self) -> None:
        print(
            f"Scheduler started — {len(self._state)} jobs from {self._config_path}",
            file=sys.stderr,
        )
        rows = sorted(self._state.values(), key=lambda s: s.next_fire)
        if not rows:
            return
        name_w = max(len(s.spec.name) for s in rows)
        cron_w = max(len(s.spec.cron) for s in rows)
        for s in rows:
            print(
                f"  {s.spec.name:<{name_w}}  {s.spec.cron:<{cron_w}}  "
                f"{s.spec.kind:<6}  next: {s.next_fire:%a %H:%M}",
                file=sys.stderr,
            )

    # --- Frontend protocol ---

    def set_orchestrator(self, orchestrator: object) -> None:
        from claude_on_the_fly.orchestrator import Orchestrator as _Orch

        if not isinstance(orchestrator, _Orch):
            raise TypeError(f"Expected Orchestrator, got {type(orchestrator)}")
        self._orch = orchestrator

    async def send(self, chat_id: int, response: Response) -> None:
        name = self._chat_to_name.get(chat_id, f"chat-{chat_id}")
        body = response.body.rstrip()
        stats = response.format_stats() if response.has_stats else ""
        block = body + "\n"
        if stats:
            block += f"[{stats}]\n"
        block += "=== done ===\n"
        _append(name, block)

    async def send_typing(self, chat_id: int) -> None:
        return None

    async def notify_queued(self, chat_id: int, position: int) -> None:
        logger.debug("queued chat=%d pos=%d", chat_id, position)

    def workspace_name(self, chat_id: int) -> str:
        name = self._chat_to_name.get(chat_id, f"chat-{chat_id}")
        return f"schedule/{name}"

    def sender_name(self, chat_id: int) -> str:
        return "scheduler"

    def channel_context(self, chat_id: int) -> str:
        name = self._chat_to_name.get(chat_id)
        if name and name in self._state:
            return f"cron:{self._state[name].spec.cron}"
        return "cron"

    def timeout_for(self, chat_id: int) -> float | None:
        name = self._chat_to_name.get(chat_id)
        if name and name in self._state:
            return float(self._state[name].spec.timeout)
        return None

    def describe(self) -> dict[str, str]:
        return {"config_path": str(self._config_path)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover
    import argparse

    from dotenv import load_dotenv

    from claude_on_the_fly.orchestrator import run
    from claude_on_the_fly.preflight import _setup_logging, check_claude_cli

    parser = argparse.ArgumentParser(prog="claude-schedule")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"YAML config path (default: {DEFAULT_CONFIG})",
    )
    ns = parser.parse_args()

    load_dotenv()
    _setup_logging()

    if not ns.config.is_file():
        raise SystemExit(f"config not found: {ns.config}")

    try:
        specs = load_config(ns.config)
    except ValueError as exc:
        raise SystemExit(f"config error: {exc}")

    if any(s.kind == "prompt" for s in specs):
        check_claude_cli()

    frontend = SchedulerFrontend(config_path=ns.config)
    asyncio.run(run(frontend, platform="schedule"))


if __name__ == "__main__":  # pragma: no cover
    main()
