"""Symphony orchestrator: poll the trackers, dispatch claimed tickets, drive
continuation turns.

Multi-source: each tick reconciles running workers' state per source (catching
mid-turn terminal/inactive transitions and stall timeouts), processes due
retries per source, then fetches and dispatches new candidates from each
source. Global `max_concurrent` is shared across sources; per-state caps live
on each tracker config.

Source-vs-tracker terminology:
- `trackers: dict[str, Tracker]` keys are source names (`"jira"`, `"github"`).
- `config.trackers: dict[str, TrackerCommonConfig]` mirrors that keying.
- `prompt_sources: dict[str, str]` holds the rendered prompt template per
  source so workers from different sources can use different prompts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from itertools import count
from pathlib import Path
from typing import Iterable

from claude_on_the_fly.agent import ClaudeUnavailableError
from claude_on_the_fly.heartbeat import HeartbeatWriter

from .agent_runner import TicketRunner, session_uuid_for
from .config import SymphonyConfig, TrackerCommonConfig, load_config
from claude_on_the_fly.events import (
    EVENT_CANCELLED,
    EVENT_DISPATCHED,
    EVENT_WORKER_DONE,
    EVENT_WORKER_FAILED,
    EventLog,
)
from .prompt import PromptStore
from .retry import RetryQueue
from .state import OrchestratorState, RunningEntry
from .tracker import Tracker, make_trackers
from .tracker.issue import Issue
from .workspace import (
    WORKSPACES_ROOT,
    ensure_workspace,
    read_workspace_identifier,
    remove_workspace,
)

logger = logging.getLogger(__name__)


def _eligible(
    issue: Issue,
    state: OrchestratorState,
    retry_queue: RetryQueue,
    tracker_cfg: TrackerCommonConfig,
) -> bool:
    if state.is_claimed(issue.key):
        return False
    if retry_queue.has(issue.key):
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
    tracker_cfg: TrackerCommonConfig,
) -> list[Issue]:
    return sorted(
        [i for i in fetched if _eligible(i, state, retry_queue, tracker_cfg)],
        key=_sort_key,
    )


async def _run_worker(
    issue: Issue,
    state: OrchestratorState,
    tracker: Tracker,
    tracker_cfg: TrackerCommonConfig,
    config: SymphonyConfig,
    prompt_source: str,
    retry_queue: RetryQueue,
    event_log: EventLog,
    starting_failure_attempt: int = 0,
) -> None:
    identifier = issue.identifier
    key = issue.key
    source = issue.source
    try:
        workspace = ensure_workspace(identifier, source=source)
    except Exception:
        logger.exception("[%s] worker: workspace prep failed", identifier)
        retry_queue.schedule_failure(
            issue.id,
            identifier,
            config.max_retry_backoff_ms,
            attempt=starting_failure_attempt + 1,
            error="workspace prep failed",
            source=source,
        )
        state.release(key)
        return

    entry = state.get_running(key)
    if entry is not None:
        entry.workspace = workspace
        entry.failure_attempt = starting_failure_attempt

    sid = session_uuid_for(identifier, source=source)
    runner = TicketRunner(
        issue=issue,
        workspace=workspace,
        config=config,
        tracker_cfg=tracker_cfg,
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
                    source=source,
                )
                return

            state.mark_turn_end(key)
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
                    source=source,
                )
                return

            # Apply the tracker's predicates against the refreshed issue so the
            # decision logic for "stop / park / continue" lives in the adapter.
            refreshed_summary = tracker.issue_to_summary(refreshed)
            if tracker.is_terminal(refreshed_summary, tracker_cfg):
                logger.info(
                    "[%s] terminal state %s; removing workspace",
                    identifier,
                    refreshed.state,
                )
                remove_workspace(workspace)
                event_log.append(
                    EVENT_WORKER_DONE,
                    source="symphony",
                    tracker=source,
                    identifier=identifier,
                    workspace=workspace,
                    session_uuid=sid,
                    state=refreshed.state,
                    reason="terminal",
                )
                return
            if not tracker.is_active(refreshed_summary, tracker_cfg):
                logger.info(
                    "[%s] inactive after turn (state=%s); leaving workspace",
                    identifier,
                    refreshed.state,
                )
                event_log.append(
                    EVENT_WORKER_DONE,
                    source="symphony",
                    tracker=source,
                    identifier=identifier,
                    workspace=workspace,
                    session_uuid=sid,
                    state=refreshed.state,
                    reason="inactive",
                )
                return

            runner.issue = refreshed
            state.update_running_state(key, refreshed.state)

        # Only reachable when max_turns is a finite positive value.
        logger.info(
            "[%s] max_turns=%d reached; scheduling continuation retry",
            identifier,
            config.max_turns,
        )
        retry_queue.schedule_continuation(issue.id, identifier, source=source)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "[%s] worker crashed; scheduling failure retry",
            identifier,
        )
        event_log.append(
            EVENT_WORKER_FAILED,
            source="symphony",
            tracker=source,
            identifier=identifier,
            workspace=workspace,
            session_uuid=sid,
            error=f"worker crashed: {exc}",
        )
        retry_queue.schedule_failure(
            issue.id,
            identifier,
            config.max_retry_backoff_ms,
            attempt=starting_failure_attempt + 1,
            error="worker crashed",
            source=source,
        )
    finally:
        state.release(key)


def _dispatch(
    issue: Issue,
    state: OrchestratorState,
    tracker: Tracker,
    tracker_cfg: TrackerCommonConfig,
    config: SymphonyConfig,
    prompt_source: str,
    retry_queue: RetryQueue,
    event_log: EventLog,
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
            tracker_cfg,
            config,
            prompt_source,
            retry_queue,
            event_log,
            starting_failure_attempt=starting_failure_attempt,
        ),
        name=f"symphony-worker-{issue.identifier}",
    )
    entry.task = task
    pending_tasks.add(task)
    task.add_done_callback(pending_tasks.discard)
    logger.info(
        "[%s] dispatched (source=%s, state=%s, prio=%s, labels=%s, failure_attempt=%d)",
        issue.identifier,
        issue.source,
        issue.state,
        issue.priority,
        list(issue.labels),
        starting_failure_attempt,
    )
    event_log.append(
        EVENT_DISPATCHED,
        source="symphony",
        tracker=issue.source,
        identifier=issue.identifier,
        state=issue.state,
        priority=issue.priority,
        failure_attempt=starting_failure_attempt,
    )


def _check_and_cancel_stall(
    entry: RunningEntry,
    config: SymphonyConfig,
    retry_queue: RetryQueue,
    event_log: EventLog,
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
    event_log.append(
        EVENT_CANCELLED,
        source="symphony",
        tracker=entry.source,
        identifier=entry.issue_identifier,
        workspace=entry.workspace,
        reason="stall",
        elapsed_ms=int(elapsed_ms),
    )
    retry_queue.schedule_failure(
        entry.issue_id,
        entry.issue_identifier,
        config.max_retry_backoff_ms,
        attempt=entry.failure_attempt + 1,
        error="stall timeout",
        source=entry.source,
    )
    return True


def _has_per_state_capacity(
    state: OrchestratorState,
    issue_state: str,
    tracker_cfg: TrackerCommonConfig,
    global_max_concurrent: int,
) -> bool:
    """True if a new worker for issue_state can fit under the per-state cap.

    The cap lives on the tracker config (per-source); fall back to global
    `max_concurrent` when the state isn't explicitly limited.
    """
    per_state_limit = tracker_cfg.max_concurrent_by_state.get(
        issue_state.lower(), global_max_concurrent
    )
    return state.running_by_state(issue_state) < per_state_limit


async def reconcile(
    state: OrchestratorState,
    trackers: dict[str, Tracker],
    config: SymphonyConfig,
    retry_queue: RetryQueue,
    event_log: EventLog,
) -> None:
    """SPEC §8.5: refresh state of running workers; cancel on terminal /
    inactive / stalled. Each source's running entries are reconciled against
    its own tracker."""
    running = state.all_running()
    if not running:
        return

    # Group by source so each tracker only fetches summaries for its own keys.
    by_source: dict[str, list[RunningEntry]] = {}
    for r in running:
        by_source.setdefault(r.source, []).append(r)

    # Skip any source whose tracker vanished from config mid-run (rare),
    # then fetch all surviving sources in parallel. Independent network calls
    # shouldn't serialize the tick.
    runnable: list[tuple[str, Tracker, TrackerCommonConfig, list[RunningEntry]]] = []
    for source, entries in by_source.items():
        tracker = trackers.get(source)
        tracker_cfg = config.trackers.get(source)
        if tracker is None or tracker_cfg is None:
            logger.warning(
                "reconcile[%s]: source not configured; skipping %d entrie(s)",
                source,
                len(entries),
            )
            continue
        runnable.append((source, tracker, tracker_cfg, entries))

    if not runnable:
        return

    fetch_results = await asyncio.gather(
        *(
            tracker.fetch_summaries_by_keys([e.issue_identifier for e in entries])
            for _source, tracker, _cfg, entries in runnable
        ),
        return_exceptions=True,
    )

    now = time.monotonic()
    for (source, tracker, tracker_cfg, entries), summaries in zip(
        runnable, fetch_results
    ):
        if isinstance(summaries, BaseException):
            logger.exception(
                "reconcile[%s]: state fetch failed (skipping; will retry next tick)",
                source,
                exc_info=summaries,
            )
            continue

        for entry in entries:
            if _check_and_cancel_stall(entry, config, retry_queue, event_log, now):
                continue

            summary = summaries.get(entry.issue_identifier)
            if summary is None:
                continue

            if tracker.is_terminal(summary, tracker_cfg):
                logger.info(
                    "[%s] became terminal mid-run (%s); cancelling worker and removing workspace",
                    entry.issue_identifier,
                    summary.state,
                )
                if entry.workspace is not None:
                    remove_workspace(entry.workspace)
                if entry.task is not None and not entry.task.done():
                    entry.task.cancel()
                event_log.append(
                    EVENT_CANCELLED,
                    source="symphony",
                    tracker=entry.source,
                    identifier=entry.issue_identifier,
                    workspace=entry.workspace,
                    reason="terminal",
                    state=summary.state,
                )
                continue

            if not tracker.is_active(summary, tracker_cfg):
                logger.info(
                    "[%s] inactive mid-run (state=%s); cancelling worker (workspace left)",
                    entry.issue_identifier,
                    summary.state,
                )
                if entry.task is not None and not entry.task.done():
                    entry.task.cancel()
                event_log.append(
                    EVENT_CANCELLED,
                    source="symphony",
                    tracker=entry.source,
                    identifier=entry.issue_identifier,
                    workspace=entry.workspace,
                    reason="inactive",
                    state=summary.state,
                )
                continue

            if summary.state != entry.issue_state:
                state.update_running_state(entry.key, summary.state)


async def startup_cleanup(
    root: Path,
    trackers: dict[str, Tracker],
    config: SymphonyConfig,
) -> None:
    """SPEC §8.6: per source, walk the workspace subdir and remove dirs whose
    tickets are terminal.

    Resolves each dir to the original (unsanitized) identifier via the
    `.identifier` sidecar — needed because some sources (github) sanitize
    `/` and `#` both to `_`, which can't be reversed from the dir name alone.
    Dirs without a sidecar (legacy or manually-created) fall back to the
    dir name, which works correctly for sources like jira where sanitization
    is a no-op.
    """
    # Collect per-source work upfront so the fetches can run in parallel.
    plans: list[tuple[str, Tracker, dict[Path, str]]] = []
    for source, tracker in trackers.items():
        source_root = root / source
        if not source_root.exists():
            continue
        dirs = [d for d in source_root.iterdir() if d.is_dir()]
        if not dirs:
            continue
        dir_to_ident: dict[Path, str] = {
            d: (read_workspace_identifier(d) or d.name) for d in dirs
        }
        plans.append((source, tracker, dir_to_ident))

    if not plans:
        return

    fetch_results = await asyncio.gather(
        *(
            tracker.fetch_summaries_by_keys(list(dir_to_ident.values()))
            for _source, tracker, dir_to_ident in plans
        ),
        return_exceptions=True,
    )

    for (source, _tracker, dir_to_ident), summaries in zip(plans, fetch_results):
        if isinstance(summaries, BaseException):
            logger.warning(
                "startup_cleanup[%s]: state fetch failed; skipping (%s)",
                source,
                summaries,
            )
            continue
        terminal = set(config.trackers[source].terminal_states)
        for d, ident in dir_to_ident.items():
            summary = summaries.get(ident)
            if summary and summary.state in terminal:
                logger.info(
                    "startup_cleanup[%s]: %s status=%s", source, d, summary.state
                )
                remove_workspace(d)


async def _process_due_retries(
    state: OrchestratorState,
    trackers: dict[str, Tracker],
    config: SymphonyConfig,
    prompt_sources: dict[str, str],
    retry_queue: RetryQueue,
    event_log: EventLog,
    pending_tasks: set[asyncio.Task[None]],
) -> None:
    due = retry_queue.due_now()
    if not due:
        return
    logger.debug("retry: %d due entries", len(due))

    # Cheap check: only dispatch entries that aren't already claimed.
    unclaimed = [e for e in due if not state.is_claimed(e.key)]
    if not unclaimed:
        return

    # Group by source for batched summary fetches.
    by_source: dict[str, list] = {}
    for e in unclaimed:
        by_source.setdefault(e.source, []).append(e)

    # Collect runnable plans, then fetch all sources concurrently.
    runnable: list[tuple[str, Tracker, TrackerCommonConfig, list]] = []
    for source, entries in by_source.items():
        tracker = trackers.get(source)
        tracker_cfg = config.trackers.get(source)
        if tracker is None or tracker_cfg is None:
            logger.warning(
                "retry[%s]: source not configured; dropping %d entrie(s)",
                source,
                len(entries),
            )
            continue
        runnable.append((source, tracker, tracker_cfg, entries))

    if not runnable:
        return

    fetch_results = await asyncio.gather(
        *(
            tracker.fetch_summaries_by_keys([e.identifier for e in entries])
            for _source, tracker, _cfg, entries in runnable
        ),
        return_exceptions=True,
    )

    for (source, tracker, tracker_cfg, entries), summaries in zip(
        runnable, fetch_results
    ):
        if isinstance(summaries, BaseException):
            logger.warning(
                "retry[%s]: batch state fetch failed; requeueing all due (1s): %s",
                source,
                summaries,
            )
            for entry in entries:
                retry_queue.requeue(
                    entry, delay_ms=1000, error="batch state fetch failed"
                )
            continue

        prompt_source = prompt_sources.get(source, "")
        for entry in entries:
            summary = summaries.get(entry.identifier)
            if summary is None:
                retry_queue.requeue(
                    entry, delay_ms=entry.attempt * 1000, error="not visible"
                )
                continue
            if tracker.is_terminal(summary, tracker_cfg):
                logger.info("[%s] retry: terminal state, dropping", entry.identifier)
                continue
            if not tracker.is_active(summary, tracker_cfg):
                logger.info(
                    "[%s] retry: inactive (state=%s), dropping",
                    entry.identifier,
                    summary.state,
                )
                continue
            if state.running_count() >= config.max_concurrent:
                logger.debug(
                    "[%s] retry: no global slot, requeueing (1s)", entry.identifier
                )
                retry_queue.requeue(entry, delay_ms=1000, error="no global slots")
                continue
            if not _has_per_state_capacity(
                state, summary.state, tracker_cfg, config.max_concurrent
            ):
                logger.debug(
                    "[%s] retry: per-state cap hit for %s, requeueing (1s)",
                    entry.identifier,
                    summary.state,
                )
                retry_queue.requeue(
                    entry,
                    delay_ms=1000,
                    error=f"no slots for {summary.state}",
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

            # Race window: state changed between summary check and fetch_one.
            fresh_summary = tracker.issue_to_summary(issue)
            if not tracker.is_active(fresh_summary, tracker_cfg):
                logger.info(
                    "[%s] retry: became inactive between summary and fetch, dropping",
                    entry.identifier,
                )
                continue

            _dispatch(
                issue,
                state,
                tracker,
                tracker_cfg,
                config,
                prompt_source,
                retry_queue,
                event_log,
                pending_tasks,
                starting_failure_attempt=entry.attempt,
            )


async def tick(
    state: OrchestratorState,
    config: SymphonyConfig,
    prompt_sources: dict[str, str],
    trackers: dict[str, Tracker],
    retry_queue: RetryQueue,
    event_log: EventLog,
    pending_tasks: set[asyncio.Task[None]],
) -> None:
    """One scheduling pass: reconcile -> process retries -> fetch and dispatch
    new candidates from every source. Global `max_concurrent` is shared
    across sources."""
    await reconcile(state, trackers, config, retry_queue, event_log)
    await _process_due_retries(
        state,
        trackers,
        config,
        prompt_sources,
        retry_queue,
        event_log,
        pending_tasks,
    )

    if state.running_count() >= config.max_concurrent:
        return

    # Fetch from each source in parallel — independent network calls
    # shouldn't serialize the tick. Then filter to eligible, merge, sort,
    # and dispatch under the global capacity ceiling.
    sources = list(trackers.items())
    fetch_results = await asyncio.gather(
        *(
            tracker.fetch_candidates(config.trackers[source])
            for source, tracker in sources
        ),
        return_exceptions=True,
    )

    candidates: list[tuple[Issue, str]] = []  # (issue, source)
    for (source, _tracker), fetched in zip(sources, fetch_results):
        if isinstance(fetched, BaseException):
            logger.exception(
                "tick[%s]: fetch_candidates failed; skipping dispatch",
                source,
                exc_info=fetched,
            )
            continue
        tracker_cfg = config.trackers[source]
        for issue in _select_candidates(fetched, state, retry_queue, tracker_cfg):
            candidates.append((issue, source))

    if not candidates:
        return

    candidates.sort(key=lambda pair: _sort_key(pair[0]))

    logger.debug(
        "tick: %d eligible candidates (across %d source(s)), capacity=%d",
        len(candidates),
        len(trackers),
        config.max_concurrent - state.running_count(),
    )

    for issue, source in candidates:
        if state.running_count() >= config.max_concurrent:
            break
        tracker = trackers[source]
        tracker_cfg = config.trackers[source]
        if not _has_per_state_capacity(
            state, issue.state, tracker_cfg, config.max_concurrent
        ):
            continue
        _dispatch(
            issue,
            state,
            tracker,
            tracker_cfg,
            config,
            prompt_sources.get(source, ""),
            retry_queue,
            event_log,
            pending_tasks,
            starting_failure_attempt=0,
        )


def _redact_token(token: str) -> str:
    if not token:
        return "<unset>"
    if len(token) <= 4:
        return "***"
    return f"{token[:2]}***{token[-2:]}"


def _log_config_summary(config: SymphonyConfig) -> None:
    """Dump the resolved config to the log at startup. Redacts the api token."""
    from .config import JiraTrackerConfig

    logger.info("symphony config:")
    logger.info("  trackers: %d configured", len(config.trackers))
    for source, t in config.trackers.items():
        logger.info("  [%s] kind            = %s", source, t.kind)
        if isinstance(t, JiraTrackerConfig):
            logger.info("  [%s] base_url        = %s", source, t.base_url)
            logger.info("  [%s] email           = %s", source, t.email)
            logger.info("  [%s] project_key     = %s", source, t.project_key)
            if t.jql_extra:
                logger.info("  [%s] jql_extra       = %s", source, t.jql_extra)
            logger.info(
                "  [%s] api_token       = %s", source, _redact_token(t.api_token)
            )
        logger.info("  [%s] active_states   = %s", source, list(t.active_states))
        logger.info("  [%s] terminal_states = %s", source, list(t.terminal_states))
        logger.info("  [%s] gate_label      = %s", source, t.gate_label or "<none>")
        logger.info("  [%s] prompt_path     = %s", source, t.prompt_path)
        logger.info(
            "  [%s] max_concurrent_by_state = %s",
            source,
            dict(t.max_concurrent_by_state) or "<none>",
        )
    logger.info("  polling_ms          = %d", config.polling_ms)
    logger.info("  max_concurrent      = %d", config.max_concurrent)
    logger.info("  max_turns           = %d", config.max_turns)
    logger.info("  turn_timeout_ms     = %d", config.turn_timeout_ms)
    logger.info("  stall_timeout_ms    = %d", config.stall_timeout_ms)
    logger.info("  max_retry_backoff_ms = %d", config.max_retry_backoff_ms)


def _heartbeat_extra(
    state: OrchestratorState,
    pending_tasks: set[asyncio.Task[None]],
    retry_queue: RetryQueue,
) -> dict:
    """Snapshot symphony state for the TUI to render. Called every heartbeat tick."""
    now = time.monotonic()
    running_tickets = [
        {
            "identifier": e.issue_identifier,
            "source": e.source,
            "state": e.issue_state,
            "uptime_s": int(now - e.started_at),
            "last_turn_end_age_s": (
                int(now - e.last_turn_end_at)
                if e.last_turn_end_at is not None
                else None
            ),
            "failure_attempt": e.failure_attempt,
        }
        for e in state.all_running()
    ]
    return {
        "running": state.running_count(),
        "pending_workers": len(pending_tasks),
        "retry_queue": len(retry_queue.all_pending()),
        "running_tickets": running_tickets,
    }


async def run_loop(config_path, stop_event: asyncio.Event) -> None:
    """Main daemon loop. Hot-reloads prompts; ticks at configured cadence."""
    config = load_config(config_path)
    config.validate()
    _log_config_summary(config)

    # One PromptStore per source; mtime-based hot-reload kicks in on
    # `maybe_reload()` each tick.
    prompt_stores: dict[str, PromptStore] = {
        source: PromptStore(tracker_cfg.prompt_path)
        for source, tracker_cfg in config.trackers.items()
    }
    prompt_sources: dict[str, str] = {
        source: store.load() for source, store in prompt_stores.items()
    }

    trackers = make_trackers(config)
    state = OrchestratorState()
    event_log = EventLog()
    retry_queue = RetryQueue(event_log=event_log)
    pending_tasks: set[asyncio.Task[None]] = set()

    heartbeat = HeartbeatWriter(
        "symphony",
        extra_provider=lambda: _heartbeat_extra(state, pending_tasks, retry_queue),
    )
    heartbeat_task = asyncio.create_task(heartbeat.run())

    logger.info(
        "symphony: started (poll every %dms, sources=%s)",
        config.polling_ms,
        sorted(trackers),
    )

    try:
        await startup_cleanup(WORKSPACES_ROOT, trackers, config)

        while not stop_event.is_set():
            for source, store in prompt_stores.items():
                prompt_sources[source] = store.maybe_reload()

            try:
                await tick(
                    state,
                    config,
                    prompt_sources,
                    trackers,
                    retry_queue,
                    event_log,
                    pending_tasks,
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
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        try:
            heartbeat.path.unlink()
        except FileNotFoundError:
            pass
        for tracker in trackers.values():
            await tracker.aclose()
        logger.info("symphony: shut down")
