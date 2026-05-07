"""Symphony orchestrator: poll the tracker, dispatch claimed tickets, drive continuation turns.

Per tick: reconcile running workers' state (catching mid-turn terminal/inactive
transitions and stall timeouts), process due retries, then fetch and dispatch
new candidates. Startup cleans up scratch dirs whose tickets are already terminal.
"""

from __future__ import annotations

import asyncio
import logging
import time
from itertools import count
from pathlib import Path
from typing import Iterable

from claude_on_the_fly.agent import ClaudeUnavailableError

from .agent_runner import TicketRunner, session_uuid_for
from .config import SymphonyConfig, TrackerConfig, load_config
from .prompt import PromptStore
from .retry import RetryQueue
from .state import OrchestratorState, RunningEntry
from .tracker import Tracker, make_tracker
from .tracker.issue import Issue
from .workspace import WORKSPACES_ROOT, ensure_workspace, remove_workspace

logger = logging.getLogger(__name__)


def _eligible(
    issue: Issue,
    state: OrchestratorState,
    retry_queue: RetryQueue,
    tracker_cfg: TrackerConfig,
) -> bool:
    if state.is_claimed(issue.id):
        return False
    if retry_queue.has(issue.id):
        return False
    if not issue.id or not issue.identifier or not issue.title or not issue.state:
        return False
    if issue.state not in tracker_cfg.active_states:
        return False
    if issue.state in tracker_cfg.terminal_states:
        return False
    if issue.state.lower() == "to do":
        terminal = set(tracker_cfg.terminal_states)
        for blocker in issue.blocked_by:
            if blocker.state and blocker.state not in terminal:
                return False
    return True


def _sort_key(issue: Issue) -> tuple:
    """SPEC §8.2: priority asc (lower = sooner), created_at oldest first, identifier tiebreak."""
    prio = issue.priority if issue.priority is not None else 9999
    created = issue.created_at or "9999-99-99T99:99:99"
    return (prio, created, issue.identifier)


def _select_candidates(
    fetched: Iterable[Issue],
    state: OrchestratorState,
    retry_queue: RetryQueue,
    tracker_cfg: TrackerConfig,
) -> list[Issue]:
    return sorted(
        [i for i in fetched if _eligible(i, state, retry_queue, tracker_cfg)],
        key=_sort_key,
    )


async def _run_worker(
    issue: Issue,
    state: OrchestratorState,
    tracker: Tracker,
    config: SymphonyConfig,
    prompt_source: str,
    retry_queue: RetryQueue,
    starting_failure_attempt: int = 0,
) -> None:
    identifier = issue.identifier
    try:
        workspace = ensure_workspace(identifier)
    except Exception:
        logger.exception("[%s] worker: workspace prep failed", identifier)
        retry_queue.schedule_failure(
            issue.id,
            identifier,
            config.max_retry_backoff_ms,
            attempt=starting_failure_attempt + 1,
            error="workspace prep failed",
        )
        state.release(issue.id)
        return

    entry = state.get_running(issue.id)
    if entry is not None:
        entry.workspace = workspace
        entry.failure_attempt = starting_failure_attempt

    sid = session_uuid_for(identifier)
    runner = TicketRunner(
        issue=issue,
        workspace=workspace,
        config=config,
        prompt_source=prompt_source,
        session_uuid=sid,
    )
    logger.info(
        "[%s] worker started: workspace=%s session=%s (failure_attempt=%d)",
        identifier,
        workspace,
        sid,
        starting_failure_attempt,
    )

    try:
        turn_iter = count() if config.max_turns < 0 else range(config.max_turns)
        for attempt in turn_iter:
            try:
                response = await runner.run_turn(attempt)
            except asyncio.CancelledError:
                logger.info(
                    "[%s] worker cancelled mid-turn (reconcile-driven)", identifier
                )
                raise
            except ClaudeUnavailableError as exc:
                logger.warning(
                    "[%s] claude unavailable (turn %d): %s; scheduling failure retry",
                    identifier,
                    attempt,
                    exc,
                )
                retry_queue.schedule_failure(
                    issue.id,
                    identifier,
                    config.max_retry_backoff_ms,
                    attempt=starting_failure_attempt + 1,
                    error=str(exc),
                )
                return

            state.mark_turn_end(issue.id)
            logger.info(
                "[%s] turn %d done | sid=%s | %s | %s",
                identifier,
                attempt,
                sid[:13],
                response.format_stats(),
                response.format_tools() or "-",
            )

            try:
                refreshed = await tracker.fetch_one(identifier)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[%s] failed to refresh issue after turn; scheduling failure retry",
                    identifier,
                )
                retry_queue.schedule_failure(
                    issue.id,
                    identifier,
                    config.max_retry_backoff_ms,
                    attempt=starting_failure_attempt + 1,
                    error="post-turn refresh failed",
                )
                return

            current_state = refreshed.state
            if current_state in config.tracker.terminal_states:
                logger.info(
                    "[%s] terminal state %s; removing workspace",
                    identifier,
                    current_state,
                )
                remove_workspace(workspace)
                return
            if current_state not in config.tracker.active_states:
                logger.info(
                    "[%s] state %s neither active nor terminal; leaving workspace",
                    identifier,
                    current_state,
                )
                return

            runner.issue = refreshed
            state.update_running_state(issue.id, refreshed.state)

        # Only reachable when max_turns is a finite positive value.
        logger.info(
            "[%s] max_turns=%d reached; scheduling continuation retry",
            identifier,
            config.max_turns,
        )
        retry_queue.schedule_continuation(issue.id, identifier)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "[%s] worker crashed; scheduling failure retry",
            identifier,
        )
        retry_queue.schedule_failure(
            issue.id,
            identifier,
            config.max_retry_backoff_ms,
            attempt=starting_failure_attempt + 1,
            error="worker crashed",
        )
    finally:
        state.release(issue.id)


def _dispatch(
    issue: Issue,
    state: OrchestratorState,
    tracker: Tracker,
    config: SymphonyConfig,
    prompt_source: str,
    retry_queue: RetryQueue,
    pending_tasks: set[asyncio.Task[None]],
    starting_failure_attempt: int = 0,
) -> None:
    """Claim, spawn worker task, register lifecycle. Caller checks capacity first."""
    try:
        entry = state.claim(issue)
    except RuntimeError:
        return
    task = asyncio.create_task(
        _run_worker(
            issue,
            state,
            tracker,
            config,
            prompt_source,
            retry_queue,
            starting_failure_attempt=starting_failure_attempt,
        ),
        name=f"symphony-worker-{issue.identifier}",
    )
    entry.task = task
    pending_tasks.add(task)
    task.add_done_callback(pending_tasks.discard)
    logger.info(
        "[%s] dispatched (state=%s, prio=%s, labels=%s, failure_attempt=%d)",
        issue.identifier,
        issue.state,
        issue.priority,
        list(issue.labels),
        starting_failure_attempt,
    )


def _check_and_cancel_stall(
    entry: RunningEntry,
    config: SymphonyConfig,
    retry_queue: RetryQueue,
    now_monotonic: float,
) -> bool:
    """Cancel the worker and queue a failure retry if it has stalled. Returns True if stalled."""
    if config.stall_timeout_ms <= 0:
        return False
    ref = (
        entry.last_turn_end_at
        if entry.last_turn_end_at is not None
        else entry.started_at
    )
    elapsed_ms = (now_monotonic - ref) * 1000
    if elapsed_ms <= config.stall_timeout_ms:
        return False
    logger.warning(
        "[%s] stall detected (%.0fms since last progress); cancelling",
        entry.issue_identifier,
        elapsed_ms,
    )
    if entry.task is not None and not entry.task.done():
        entry.task.cancel()
    retry_queue.schedule_failure(
        entry.issue_id,
        entry.issue_identifier,
        config.max_retry_backoff_ms,
        attempt=entry.failure_attempt + 1,
        error="stall timeout",
    )
    return True


def _has_per_state_capacity(
    state: OrchestratorState, issue_state: str, config: SymphonyConfig
) -> bool:
    """True if a new worker for issue_state can fit under the per-state cap."""
    per_state_limit = config.max_concurrent_by_state.get(
        issue_state.lower(), config.max_concurrent
    )
    return state.running_by_state(issue_state) < per_state_limit


async def reconcile(
    state: OrchestratorState,
    tracker: Tracker,
    config: SymphonyConfig,
    retry_queue: RetryQueue,
) -> None:
    """SPEC §8.5: refresh state of running workers; cancel on terminal/inactive/stalled."""
    running = state.all_running()
    if not running:
        return

    keys = [r.issue_identifier for r in running]
    try:
        statuses = await tracker.fetch_states_by_keys(keys)
    except Exception:
        logger.exception(
            "reconcile: state fetch failed (skipping; will retry next tick)"
        )
        return

    now = time.monotonic()
    for entry in running:
        if _check_and_cancel_stall(entry, config, retry_queue, now):
            continue

        new_state = statuses.get(entry.issue_identifier)
        if new_state is None:
            continue

        if new_state in config.tracker.terminal_states:
            logger.info(
                "[%s] became terminal mid-run (%s); cancelling worker and removing workspace",
                entry.issue_identifier,
                new_state,
            )
            if entry.workspace is not None:
                remove_workspace(entry.workspace)
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
            continue

        if new_state not in config.tracker.active_states:
            logger.info(
                "[%s] became inactive mid-run (%s); cancelling worker (workspace left)",
                entry.issue_identifier,
                new_state,
            )
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
            continue

        if new_state != entry.issue_state:
            state.update_running_state(entry.issue_id, new_state)


async def startup_cleanup(
    root: Path,
    tracker: Tracker,
    tracker_cfg: TrackerConfig,
) -> None:
    """SPEC §8.6: walk the symphony workspaces root, remove dirs whose tickets are terminal."""
    if not root.exists():
        return
    dirs = [d for d in root.iterdir() if d.is_dir()]
    if not dirs:
        return
    keys = [d.name for d in dirs]
    try:
        statuses = await tracker.fetch_states_by_keys(keys)
    except Exception:
        logger.warning("startup_cleanup: state fetch failed; skipping")
        return

    terminal = set(tracker_cfg.terminal_states)
    for d in dirs:
        status = statuses.get(d.name)
        if status and status in terminal:
            logger.info("startup_cleanup: %s status=%s", d, status)
            remove_workspace(d)


async def _process_due_retries(
    state: OrchestratorState,
    tracker: Tracker,
    config: SymphonyConfig,
    prompt_source: str,
    retry_queue: RetryQueue,
    pending_tasks: set[asyncio.Task[None]],
) -> None:
    due = retry_queue.due_now()
    if not due:
        return
    logger.debug("retry: %d due entries", len(due))

    # Batch the cheap status check first; only fetch full Issue for entries we'd dispatch.
    unclaimed = [e for e in due if not state.is_claimed(e.issue_id)]
    if not unclaimed:
        return
    try:
        statuses = await tracker.fetch_states_by_keys([e.identifier for e in unclaimed])
    except Exception:
        logger.warning("retry: batch state fetch failed; requeueing all due (1s)")
        for entry in unclaimed:
            retry_queue.requeue(entry, delay_ms=1000, error="batch state fetch failed")
        return

    for entry in unclaimed:
        current_state = statuses.get(entry.identifier)
        if current_state is None:
            retry_queue.requeue(
                entry, delay_ms=entry.attempt * 1000, error="not visible"
            )
            continue
        if current_state in config.tracker.terminal_states:
            logger.info("[%s] retry: terminal state, dropping", entry.identifier)
            continue
        if current_state not in config.tracker.active_states:
            logger.info(
                "[%s] retry: state %s not active, dropping",
                entry.identifier,
                current_state,
            )
            continue
        if state.running_count() >= config.max_concurrent:
            logger.debug(
                "[%s] retry: no global slot, requeueing (1s)", entry.identifier
            )
            retry_queue.requeue(entry, delay_ms=1000, error="no global slots")
            continue
        if not _has_per_state_capacity(state, current_state, config):
            logger.debug(
                "[%s] retry: per-state cap hit for %s, requeueing (1s)",
                entry.identifier,
                current_state,
            )
            retry_queue.requeue(
                entry, delay_ms=1000, error=f"no slots for {current_state}"
            )
            continue

        try:
            issue = await tracker.fetch_one(entry.identifier)
        except Exception:
            logger.warning(
                "[%s] retry: fetch_one failed; requeueing",
                entry.identifier,
            )
            retry_queue.requeue(
                entry, delay_ms=entry.attempt * 1000, error="fetch_one failed"
            )
            continue

        _dispatch(
            issue,
            state,
            tracker,
            config,
            prompt_source,
            retry_queue,
            pending_tasks,
            starting_failure_attempt=entry.attempt,
        )


async def tick(
    state: OrchestratorState,
    config: SymphonyConfig,
    prompt_source: str,
    tracker: Tracker,
    retry_queue: RetryQueue,
    pending_tasks: set[asyncio.Task[None]],
) -> None:
    """One scheduling pass: reconcile -> process retries -> fetch and dispatch."""
    await reconcile(state, tracker, config, retry_queue)
    await _process_due_retries(
        state,
        tracker,
        config,
        prompt_source,
        retry_queue,
        pending_tasks,
    )

    if state.running_count() >= config.max_concurrent:
        return

    try:
        fetched = await tracker.fetch_candidates(config.tracker)
    except Exception:
        logger.exception("tick: fetch_candidates failed; skipping dispatch")
        return

    candidates = _select_candidates(fetched, state, retry_queue, config.tracker)
    if not candidates:
        return

    logger.debug(
        "tick: %d eligible candidates, capacity=%d",
        len(candidates),
        config.max_concurrent - state.running_count(),
    )

    for issue in candidates:
        if state.running_count() >= config.max_concurrent:
            break
        if not _has_per_state_capacity(state, issue.state, config):
            continue
        _dispatch(
            issue,
            state,
            tracker,
            config,
            prompt_source,
            retry_queue,
            pending_tasks,
            starting_failure_attempt=0,
        )


async def run_loop(config_path, stop_event: asyncio.Event) -> None:
    """Main daemon loop. Hot-reloads prompt; ticks at configured cadence."""
    config = load_config(config_path)
    config.validate()

    prompt_store = PromptStore(config.prompt_path)
    prompt_source = prompt_store.load()

    tracker = make_tracker(config.tracker)
    state = OrchestratorState()
    retry_queue = RetryQueue()
    pending_tasks: set[asyncio.Task[None]] = set()

    logger.info(
        "symphony: started (poll every %dms, prompt=%s)",
        config.polling_ms,
        config.prompt_path,
    )

    try:
        await startup_cleanup(WORKSPACES_ROOT, tracker, config.tracker)

        while not stop_event.is_set():
            prompt_source = prompt_store.maybe_reload()

            try:
                await tick(
                    state, config, prompt_source, tracker, retry_queue, pending_tasks
                )
            except Exception:
                logger.exception("tick: unexpected failure (continuing)")

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=config.polling_ms / 1000,
                )
            except asyncio.TimeoutError:
                pass

        logger.info(
            "symphony: stop signal received; awaiting %d worker(s)", len(pending_tasks)
        )
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
    finally:
        await tracker.aclose()
        logger.info("symphony: shut down")
