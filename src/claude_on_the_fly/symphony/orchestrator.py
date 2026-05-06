"""Symphony orchestrator: poll Jira, dispatch claimed tickets, drive continuation turns.

Phase 2 adds: reconciliation per tick (state refresh of running workers, stall
detection), retry queue with backoff, startup terminal-workspace cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from itertools import count
from typing import Iterable

from claude_on_the_fly.agent import ClaudeUnavailableError

from .agent_runner import TicketRunner, session_uuid_for
from .config import SymphonyConfig, TrackerConfig, load_config
from .prompt import PromptStore
from .retry import RetryQueue
from .state import OrchestratorState
from .tracker.issue import Issue
from .tracker.jira import JiraTracker
from .workspace import ensure_workspace, remove_workspace

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
    tracker: JiraTracker,
    config: SymphonyConfig,
    prompt_source: str,
    retry_queue: RetryQueue,
    starting_failure_attempt: int = 0,
) -> None:
    identifier = issue.identifier
    try:
        workspace = ensure_workspace(issue, config.worktree_root)
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
        workspace.path,
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
                "[%s] turn %d done | %s | %s",
                identifier,
                attempt,
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
    tracker: JiraTracker,
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


async def reconcile(
    state: OrchestratorState,
    tracker: JiraTracker,
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
        if config.stall_timeout_ms > 0:
            ref = (
                entry.last_turn_end_at
                if entry.last_turn_end_at is not None
                else entry.started_at
            )
            elapsed_ms = (now - ref) * 1000
            if elapsed_ms > config.stall_timeout_ms:
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
    worktree_root,
    tracker: JiraTracker,
    tracker_cfg: TrackerConfig,
) -> None:
    """SPEC §8.6: walk worktree_root, remove dirs whose tickets are terminal."""
    if not worktree_root.exists():
        return
    dirs = [d for d in worktree_root.iterdir() if d.is_dir()]
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
            logger.info(
                "startup_cleanup: removing %s (status=%s)",
                d,
                status,
            )
            shutil.rmtree(d, ignore_errors=True)


async def _process_due_retries(
    state: OrchestratorState,
    tracker: JiraTracker,
    config: SymphonyConfig,
    prompt_source: str,
    retry_queue: RetryQueue,
    pending_tasks: set[asyncio.Task[None]],
) -> None:
    due = retry_queue.due_now()
    if not due:
        return
    logger.debug("retry: %d due entries", len(due))
    for entry in due:
        if state.is_claimed(entry.issue_id):
            continue
        try:
            issue = await tracker.fetch_one(entry.identifier)
        except Exception:
            logger.warning(
                "[%s] retry: fetch failed; requeueing with same backoff",
                entry.identifier,
            )
            retry_queue.requeue(
                entry, delay_ms=entry.attempt * 1000, error="fetch failed"
            )
            continue

        if issue.state in config.tracker.terminal_states:
            logger.info("[%s] retry: terminal state, dropping", entry.identifier)
            continue
        if issue.state not in config.tracker.active_states:
            logger.info(
                "[%s] retry: state %s not active, dropping",
                entry.identifier,
                issue.state,
            )
            continue
        if state.running_count() >= config.max_concurrent:
            logger.debug(
                "[%s] retry: no slot, requeueing (1s)",
                entry.identifier,
            )
            retry_queue.requeue(entry, delay_ms=1000, error="no slots")
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
    tracker: JiraTracker,
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

    tracker = JiraTracker(
        base_url=config.tracker.base_url,
        email=config.tracker.email,
        api_token=config.tracker.api_token,
    )
    state = OrchestratorState()
    retry_queue = RetryQueue()
    pending_tasks: set[asyncio.Task[None]] = set()

    logger.info(
        "symphony: started (poll every %dms, prompt=%s)",
        config.polling_ms,
        config.prompt_path,
    )

    try:
        await startup_cleanup(config.worktree_root, tracker, config.tracker)

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
