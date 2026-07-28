"""Symphony orchestrator: poll the trackers, dispatch claimed tickets, drive
continuation turns.

Multi-source: each tick reconciles running workers' state per source (catching
mid-turn terminal/inactive transitions and stall timeouts), processes due
retries per source, then fetches and dispatches new candidates from each
source. Concurrency is per-tracker (`max_concurrent`); no global cap.

Source-vs-tracker terminology:
- `trackers: dict[str, Tracker]` keys are source names (the tracker's key in
  the config; defaults to the kind, `"jira"` / `"github"`).
- `config.trackers: dict[str, TrackerCommonConfig]` mirrors that keying.
- prompt resolution: one `InstructionResolver` per source resolves each
  issue's instruction file (default stem or per-repo override) at dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable, Mapping
from itertools import count
from pathlib import Path

from claude_on_the_fly.agent import ClaudeUnavailableError, current_backend_key
from claude_on_the_fly.events import (
    EVENT_CANCELLED,
    EVENT_DISPATCHED,
    EVENT_WORKER_DONE,
    EVENT_WORKER_FAILED,
    EventLog,
)
from claude_on_the_fly.heartbeat import HeartbeatWriter

from .agent_runner import TicketRunner, session_uuid_for
from .config import SymphonyConfig, TrackerCommonConfig, load_config
from .cursor import CursorStore, is_claimable
from .prompt import InstructionResolver
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


def _prompt_source_for(
    prompt_sources: Mapping[str, object], source: str, identifier: str
) -> str:
    """Return the prompt template string for a specific issue.

    `prompt_sources[source]` is either:
    - an `InstructionResolver` (production): resolves the issue's instruction
      stem (per-repo override or the tracker default) and hot-reloads the file.
    - a `str` (test-only): the literal template source.
    """
    entry = prompt_sources.get(source)
    if entry is None:
        return ""
    if isinstance(entry, InstructionResolver):
        return entry.resolve_for(identifier)
    if isinstance(entry, str):
        return entry
    raise TypeError(
        f"prompt_sources[{source!r}] has unexpected type {type(entry).__name__}"
    )


def _eligible(
    issue: Issue,
    state: OrchestratorState,
    retry_queue: RetryQueue,
    tracker_cfg: TrackerCommonConfig,
    cursor_store: CursorStore | None = None,
) -> bool:
    if state.is_claimed(issue.key):
        return False
    if retry_queue.has(issue.key):
        return False
    if not issue.id or not issue.identifier or not issue.title or not issue.state:
        return False
    # No active/terminal-state checks: the candidate fetch is authoritative
    # (Jira's `jql`, GitHub's `search_query` + reviewed-at-head filter). If a
    # ticket came back as a candidate, it qualifies.
    # Cursor gating (replaces gate_label for Jira). For trackers without a
    # CursorStore (e.g. GitHub uses review-removal as its done signal), this
    # check is skipped.
    if cursor_store is not None:
        cursor = cursor_store.load(issue.identifier)
        if not is_claimable(ticket_updated=issue.updated_at, cursor=cursor):
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
    cursor_store: CursorStore | None = None,
) -> list[Issue]:
    return sorted(
        [
            i
            for i in fetched
            if _eligible(i, state, retry_queue, tracker_cfg, cursor_store)
        ],
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
    cursor_store: CursorStore | None = None,
) -> None:
    identifier = issue.identifier
    key = issue.key
    source = issue.source
    outcome = "unknown"
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

    sid = session_uuid_for(identifier, source=source, backend_key=current_backend_key())
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
        no_progress = 0  # consecutive turns that completed with zero tool use
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

            # No-progress guard: a turn that completes without invoking any
            # tool produced nothing actionable (e.g. an empty/synthetic backend
            # reply). The wall-clock stall_timeout can't catch this because each
            # turn resets the idle timer. Bail after N in a row instead of
            # churning to max_turns.
            if config.max_no_progress_turns > 0:
                no_progress = 0 if response.has_tools else no_progress + 1
                if no_progress >= config.max_no_progress_turns:
                    outcome = "no_progress"
                    logger.warning(
                        "[%s] %d consecutive turns with no tool use (model=%s); "
                        "agent making no progress, stopping",
                        identifier,
                        no_progress,
                        response.model or "?",
                    )
                    event_log.append(
                        EVENT_WORKER_FAILED,
                        source="symphony",
                        tracker=source,
                        backend=current_backend_key(),
                        identifier=identifier,
                        workspace=workspace,
                        session_uuid=sid,
                        error=f"no progress: {no_progress} turns with no tool use",
                    )
                    retry_queue.schedule_failure(
                        issue.id,
                        identifier,
                        config.max_retry_backoff_ms,
                        attempt=starting_failure_attempt + 1,
                        error="no tool use",
                        source=source,
                    )
                    return

            # Decide stop / park / continue via the same summary path the
            # daemon reconciler uses, so worker and daemon never disagree.
            # For Jira this re-runs the candidate JQL membership (needs cfg);
            # for GitHub it re-checks PR state + reviewed-at-head.
            try:
                summaries = await tracker.fetch_summaries_by_keys(
                    [identifier], tracker_cfg
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[%s] failed to refresh state after turn; scheduling failure retry",
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

            summary = summaries.get(identifier)
            if summary is None:
                # Per the tracker Protocol a missing key is a TRANSIENT fetch
                # gap (e.g. a `gh pr view` blip), NOT terminal — do not remove
                # the workspace. Reschedule and let the next attempt re-fetch.
                logger.warning(
                    "[%s] state missing after turn (transient); scheduling retry",
                    identifier,
                )
                retry_queue.schedule_failure(
                    issue.id,
                    identifier,
                    config.max_retry_backoff_ms,
                    attempt=starting_failure_attempt + 1,
                    error="state missing post-turn",
                    source=source,
                )
                return
            if tracker.is_terminal(summary, tracker_cfg):
                outcome = "terminal"
                logger.info(
                    "[%s] terminal/gone after turn; removing workspace", identifier
                )
                remove_workspace(workspace)
                event_log.append(
                    EVENT_WORKER_DONE,
                    source="symphony",
                    tracker=source,
                    backend=current_backend_key(),
                    identifier=identifier,
                    workspace=workspace,
                    session_uuid=sid,
                    state=summary.state,
                    reason="terminal",
                )
                return
            if not tracker.is_active(summary, tracker_cfg):
                outcome = "inactive"
                logger.info(
                    "[%s] inactive after turn (state=%s); leaving workspace",
                    identifier,
                    summary.state,
                )
                event_log.append(
                    EVENT_WORKER_DONE,
                    source="symphony",
                    tracker=source,
                    backend=current_backend_key(),
                    identifier=identifier,
                    workspace=workspace,
                    session_uuid=sid,
                    state=summary.state,
                    reason="inactive",
                )
                return

            # Continuing: refresh the full issue for the next turn's prompt.
            try:
                refreshed = await tracker.fetch_one(identifier)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[%s] failed to refresh issue for next turn; scheduling retry",
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
            runner.issue = refreshed
            state.update_running_state(key, refreshed.state)

        # Only reachable when max_turns is a finite positive value.
        outcome = "max_turns"
        logger.info(
            "[%s] max_turns=%d reached; scheduling continuation retry",
            identifier,
            config.max_turns,
        )
        retry_queue.schedule_continuation(issue.id, identifier, source=source)
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:
        outcome = "crash"
        logger.exception(
            "[%s] worker crashed; scheduling failure retry",
            identifier,
        )
        event_log.append(
            EVENT_WORKER_FAILED,
            source="symphony",
            tracker=source,
            backend=current_backend_key(),
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
        # Persist the cursor on every completion path. Skipped when no
        # store is configured for this tracker (e.g. GitHub, which uses
        # review-removal as its done signal).
        if cursor_store is not None:
            try:
                cursor_store.record_run_end(
                    identifier,
                    outcome=outcome,
                    ticket_updated=runner.issue.updated_at,
                )
            except Exception:
                logger.exception(
                    "[%s] cursor record_run_end failed (outcome=%s)",
                    identifier,
                    outcome,
                )


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
    cursor_store: CursorStore | None = None,
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
            cursor_store=cursor_store,
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
        backend=current_backend_key(),
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
        backend=current_backend_key(),
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
    source: str,
    issue_state: str,
    tracker_cfg: TrackerCommonConfig,
) -> bool:
    """True if a new worker for issue_state can fit under the per-state cap.

    The cap lives on the tracker config (per-source). When the state is not
    explicitly limited, fall back to the tracker's own `max_concurrent`.
    The per-state count is scoped to this tracker only — sibling trackers
    do not contribute to the cap.
    """
    per_state_limit = tracker_cfg.max_concurrent_by_state.get(
        issue_state.lower(), tracker_cfg.max_concurrent
    )
    return state.running_by_source_and_state(source, issue_state) < per_state_limit


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
            tracker.fetch_summaries_by_keys([e.issue_identifier for e in entries], cfg)
            for _source, tracker, cfg, entries in runnable
        ),
        return_exceptions=True,
    )

    now = time.monotonic()
    for (source, tracker, tracker_cfg, entries), summaries in zip(
        runnable, fetch_results, strict=True
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
                    backend=current_backend_key(),
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
                    backend=current_backend_key(),
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
            tracker.fetch_summaries_by_keys(
                list(dir_to_ident.values()), config.trackers[source]
            )
            for source, tracker, dir_to_ident in plans
        ),
        return_exceptions=True,
    )

    for (source, tracker, dir_to_ident), summaries in zip(
        plans, fetch_results, strict=True
    ):
        if isinstance(summaries, BaseException):
            logger.warning(
                "startup_cleanup[%s]: state fetch failed; skipping (%s)",
                source,
                summaries,
            )
            continue
        tracker_cfg = config.trackers[source]
        for d, ident in dir_to_ident.items():
            summary = summaries.get(ident)
            # GC a leftover workspace when its ticket is done (terminal) or
            # no longer active (Jira: left the JQL; GitHub: closed/merged or
            # already reviewed-at-head). Missing summary → leave it; a
            # transient fetch gap shouldn't delete a live ticket's scratch.
            if summary is None:
                continue
            if tracker.is_terminal(summary, tracker_cfg) or not tracker.is_active(
                summary, tracker_cfg
            ):
                logger.info(
                    "startup_cleanup[%s]: %s status=%s", source, d, summary.state
                )
                remove_workspace(d)


async def _process_due_retries(
    state: OrchestratorState,
    trackers: dict[str, Tracker],
    config: SymphonyConfig,
    # InstructionResolver in production, plain str in tests — see
    # _prompt_source_for. Mapping (covariant) so dict[str, InstructionResolver]
    # and dict[str, str] both satisfy it.
    prompt_sources: Mapping[str, object],
    retry_queue: RetryQueue,
    event_log: EventLog,
    pending_tasks: set[asyncio.Task[None]],
    cursor_stores: dict[str, CursorStore] | None = None,
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
            tracker.fetch_summaries_by_keys([e.identifier for e in entries], cfg)
            for _source, tracker, cfg, entries in runnable
        ),
        return_exceptions=True,
    )

    for (source, tracker, tracker_cfg, entries), summaries in zip(
        runnable, fetch_results, strict=True
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
            if state.running_by_source(source) >= tracker_cfg.max_concurrent:
                logger.debug(
                    "[%s] retry: tracker %s at capacity, requeueing (1s)",
                    entry.identifier,
                    source,
                )
                retry_queue.requeue(
                    entry,
                    delay_ms=1000,
                    error=f"no {source} slots",
                )
                continue
            if not _has_per_state_capacity(state, source, summary.state, tracker_cfg):
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

            # The is_active gate above already used the authoritative summary
            # from fetch_summaries_by_keys (which sets the kind-specific signal,
            # e.g. Jira's matches_jql). Don't re-derive activeness from
            # issue_to_summary here — for Jira that projection can't know
            # matches_jql and would drop every retry. Any state drift in the
            # sub-second window before dispatch is caught by the worker's first
            # post-turn reconcile.
            _dispatch(
                issue,
                state,
                tracker,
                tracker_cfg,
                config,
                _prompt_source_for(prompt_sources, source, issue.identifier),
                retry_queue,
                event_log,
                pending_tasks,
                starting_failure_attempt=entry.attempt,
                cursor_store=(cursor_stores or {}).get(source),
            )


async def tick(
    state: OrchestratorState,
    config: SymphonyConfig,
    # InstructionResolver in production, plain str in tests (see
    # _prompt_source_for); Mapping is covariant so both dict shapes satisfy it.
    prompt_sources: Mapping[str, object],
    trackers: dict[str, Tracker],
    retry_queue: RetryQueue,
    event_log: EventLog,
    pending_tasks: set[asyncio.Task[None]],
    cursor_stores: dict[str, CursorStore] | None = None,
) -> None:
    """One scheduling pass: reconcile -> process retries -> fetch and dispatch
    new candidates from every source. Per-tracker concurrency caps."""
    await reconcile(state, trackers, config, retry_queue, event_log)
    await _process_due_retries(
        state,
        trackers,
        config,
        prompt_sources,
        retry_queue,
        event_log,
        pending_tasks,
        cursor_stores=cursor_stores,
    )

    # Per-tracker capacity check — skip the global short-circuit. Each
    # tracker has its own budget and the fan-out below also enforces.
    if all(
        state.running_by_source(source) >= cfg.max_concurrent
        for source, cfg in config.trackers.items()
        if cfg.enabled
    ):
        return

    # Fetch from each source in parallel — independent network calls
    # shouldn't serialize the tick. Skip trackers already at capacity to
    # save a poll.
    sources = [
        (source, tracker)
        for source, tracker in trackers.items()
        if state.running_by_source(source) < config.trackers[source].max_concurrent
    ]
    fetch_results = await asyncio.gather(
        *(
            tracker.fetch_candidates(config.trackers[source])
            for source, tracker in sources
        ),
        return_exceptions=True,
    )

    candidates: list[tuple[Issue, str]] = []  # (issue, source)
    for (source, _tracker), fetched in zip(sources, fetch_results, strict=True):
        if isinstance(fetched, BaseException):
            logger.exception(
                "tick[%s]: fetch_candidates failed; skipping dispatch",
                source,
                exc_info=fetched,
            )
            continue
        tracker_cfg = config.trackers[source]
        cursor_store = (cursor_stores or {}).get(source)
        for issue in _select_candidates(
            fetched, state, retry_queue, tracker_cfg, cursor_store
        ):
            candidates.append((issue, source))

    if not candidates:
        return

    candidates.sort(key=lambda pair: _sort_key(pair[0]))

    total_capacity = sum(
        max(0, cfg.max_concurrent - state.running_by_source(source))
        for source, cfg in config.trackers.items()
        if cfg.enabled
    )
    logger.debug(
        "tick: %d eligible candidates (across %d source(s)), capacity=%d",
        len(candidates),
        len(trackers),
        total_capacity,
    )

    for issue, source in candidates:
        tracker = trackers[source]
        tracker_cfg = config.trackers[source]
        if state.running_by_source(source) >= tracker_cfg.max_concurrent:
            continue
        if not _has_per_state_capacity(state, source, issue.state, tracker_cfg):
            continue
        _dispatch(
            issue,
            state,
            tracker,
            tracker_cfg,
            config,
            _prompt_source_for(prompt_sources, source, issue.identifier),
            retry_queue,
            event_log,
            pending_tasks,
            starting_failure_attempt=0,
            cursor_store=(cursor_stores or {}).get(source),
        )


def _log_config_summary(config: SymphonyConfig) -> None:
    """Dump the resolved config to the log at startup. No secrets to redact —
    Jira auth lives in `acli`, GitHub auth lives in `gh`."""
    from .config import JiraTrackerConfig

    logger.info("symphony config:")
    logger.info("  trackers: %d configured", len(config.trackers))
    from .config import GitHubTrackerConfig

    for source, t in config.trackers.items():
        logger.info("  [%s] kind            = %s", source, t.kind)
        if isinstance(t, JiraTrackerConfig):
            logger.info("  [%s] base_url        = %s", source, t.base_url)
            logger.info("  [%s] project_key     = %s", source, t.project_key)
            logger.info("  [%s] jql             = %s", source, t.jql or "<none>")
            logger.info("  [%s] auth            = acli", source)
        if isinstance(t, GitHubTrackerConfig):
            logger.info("  [%s] search_query    = %s", source, t.search_query)
            logger.info("  [%s] auth            = gh", source)
        logger.info("  [%s] max_concurrent  = %d", source, t.max_concurrent)
        logger.info("  [%s] instruction     = %s", source, t.instruction)
        logger.info(
            "  [%s] max_concurrent_by_state = %s",
            source,
            dict(t.max_concurrent_by_state) or "<none>",
        )
    logger.info("  polling_ms          = %d", config.polling_ms)
    logger.info("  max_turns           = %d", config.max_turns)
    logger.info("  turn_timeout_ms     = %d", config.turn_timeout_ms)
    logger.info("  stall_timeout_ms    = %d", config.stall_timeout_ms)
    logger.info("  max_no_progress_turns = %d", config.max_no_progress_turns)
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


def _instructions_root() -> Path:
    """Local instructions dir: ~/.claude-on-the-fly/symphony/<kind>/<name>.md."""
    from .. import agent as _agent_mod

    return _agent_mod.DATA_DIR / "symphony"


def _build_prompt_resolvers(
    config: SymphonyConfig, *, local_root: Path
) -> dict[str, InstructionResolver]:
    """One InstructionResolver per tracker. Resolves each issue's instruction
    file from its default stem (or a per-repo override) at dispatch time."""
    resolvers: dict[str, InstructionResolver] = {}
    for source, tracker_cfg in config.trackers.items():
        resolvers[source] = InstructionResolver(
            kind=tracker_cfg.kind,
            default_instruction=tracker_cfg.instruction,
            instruction_by_repo=getattr(tracker_cfg, "instruction_by_repo", None),
            local_root=local_root,
        )
    return resolvers


def _refresh_prompt_stores(
    stores: dict[str, InstructionResolver],
    config: SymphonyConfig,
) -> None:
    """Rebuild the resolver map in place (instruction / per-repo map may have
    changed)."""
    rebuilt = _build_prompt_resolvers(config, local_root=_instructions_root())
    stores.clear()
    stores.update(rebuilt)


def _build_cursor_stores(
    config: SymphonyConfig, state_root: Path
) -> dict[str, CursorStore]:
    from .config import JiraTrackerConfig

    return {
        source: CursorStore(state_root, source)
        for source, tracker_cfg in config.trackers.items()
        if isinstance(tracker_cfg, JiraTrackerConfig)
    }


def _cancel_workers_for_sources(
    state: OrchestratorState, sources: set[str], *, reason: str
) -> int:
    """Cancel every running worker whose source is in `sources`. Returns count."""
    count_cancelled = 0
    for entry in state.all_running():
        if entry.source not in sources:
            continue
        if entry.task is not None and not entry.task.done():
            logger.info(
                "[%s] cancelling worker (%s, source=%s)",
                entry.issue_identifier,
                reason,
                entry.source,
            )
            entry.task.cancel()
            count_cancelled += 1
    return count_cancelled


def _trim_workers_to_budget(
    state: OrchestratorState, source: str, new_budget: int
) -> int:
    """Cancel newest workers for `source` until `running_by_source(source) <= new_budget`.

    Returns the number cancelled. Newest = highest `started_at`, on the
    theory that they have the least sunk work.
    """
    running = sorted(
        (e for e in state.all_running() if e.source == source),
        key=lambda e: e.started_at,
        reverse=True,
    )
    excess = max(0, len(running) - new_budget)
    cancelled = 0
    for entry in running[:excess]:
        if entry.task is not None and not entry.task.done():
            logger.info(
                "[%s] cancelling newest worker (concurrency reduced to %d)",
                entry.issue_identifier,
                new_budget,
            )
            entry.task.cancel()
            cancelled += 1
    return cancelled


def _maybe_reload_config(
    *,
    config_path: Path,
    config: SymphonyConfig,
    last_mtime: float | None,
    state: OrchestratorState,
    trackers: dict[str, Tracker],
    prompt_stores: dict[str, InstructionResolver],
    cursor_stores: dict[str, CursorStore],
    state_root: Path,
) -> tuple[SymphonyConfig, float | None]:
    """Check the config file's mtime; reload + apply changes when it bumps.

    Returns the (possibly updated) config and the new mtime. On any reload
    failure, logs and keeps the current config (last-known-good).
    """
    try:
        mtime = config_path.stat().st_mtime
    except FileNotFoundError:
        logger.warning(
            "config file vanished at %s; keeping last-known-good", config_path
        )
        return config, last_mtime
    if last_mtime is not None and mtime == last_mtime:
        return config, last_mtime
    if last_mtime is None:
        return config, mtime
    try:
        new_config = load_config(config_path)
        new_config.validate()
    except Exception:
        logger.exception(
            "config reload failed; keeping last-known-good (%s)", config_path
        )
        return config, mtime

    logger.info("config reload: %s", config_path)

    # Diff on the ENABLED source set, not raw config keys: flipping a tracker's
    # `enabled` flag must behave exactly like adding/removing its stanza —
    # toggling off cancels its in-flight workers and stops polling; toggling on
    # builds the tracker. `make_trackers` only builds enabled ones, so the live
    # `trackers` dict already tracks this set.
    old_sources = {s for s, c in config.trackers.items() if c.enabled}
    new_sources = {s for s, c in new_config.trackers.items() if c.enabled}
    removed = old_sources - new_sources
    added = new_sources - old_sources

    if removed:
        n = _cancel_workers_for_sources(state, removed, reason="tracker removed")
        for source in removed:
            trackers.pop(source, None)
            cursor_stores.pop(source, None)
        logger.info(
            "config reload: dropped tracker(s) %s (cancelled %d worker(s))",
            sorted(removed),
            n,
        )

    if added:
        from .config import JiraTrackerConfig
        from .tracker import make_tracker

        for source in added:
            tcfg = new_config.trackers[source]
            trackers[source] = make_tracker(tcfg)
            if isinstance(tcfg, JiraTrackerConfig):
                cursor_stores[source] = CursorStore(state_root, source)
        logger.info("config reload: added tracker(s) %s", sorted(added))

    # Concurrency reductions: cancel newest workers per source if the new
    # cap is lower than the current running count.
    for source in new_sources & old_sources:
        new_cap = new_config.trackers[source].max_concurrent
        running = state.running_by_source(source)
        if running > new_cap:
            n = _trim_workers_to_budget(state, source, new_cap)
            logger.info(
                "config reload: %s max_concurrent reduced %d→%d, cancelled %d",
                source,
                running,
                new_cap,
                n,
            )

    # Rebuild prompt stores in place — covers added/removed trackers and a
    # changed `instruction` selection.
    _refresh_prompt_stores(prompt_stores, new_config)

    return new_config, mtime


async def run_loop(config_path, stop_event: asyncio.Event) -> None:
    """Main daemon loop. Hot-reloads symphony.yaml + prompts; ticks at configured cadence."""
    config_path = Path(config_path)
    config = load_config(config_path)
    config.validate()
    _log_config_summary(config)

    prompt_stores: dict[str, InstructionResolver] = {}
    _refresh_prompt_stores(prompt_stores, config)

    from .. import agent as _agent_mod

    state_root = _agent_mod.DATA_DIR / "symphony" / "state"
    cursor_stores = _build_cursor_stores(config, state_root)

    try:
        last_config_mtime: float | None = config_path.stat().st_mtime
    except FileNotFoundError:
        last_config_mtime = None

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
            # Per-tick: check for config edits and apply structural changes
            # (added/removed trackers, concurrency reductions, instruction
            # changes). Prompt content reload is per-dispatch via mtime.
            config, last_config_mtime = _maybe_reload_config(
                config_path=config_path,
                config=config,
                last_mtime=last_config_mtime,
                state=state,
                trackers=trackers,
                prompt_stores=prompt_stores,
                cursor_stores=cursor_stores,
                state_root=state_root,
            )

            try:
                await tick(
                    state,
                    config,
                    prompt_stores,
                    trackers,
                    retry_queue,
                    event_log,
                    pending_tasks,
                    cursor_stores=cursor_stores,
                )
            except Exception:
                logger.exception("tick: unexpected failure (continuing)")

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=config.polling_ms / 1000,
                )

        logger.info(
            "symphony: stop signal received; awaiting %d worker(s)", len(pending_tasks)
        )
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        with contextlib.suppress(FileNotFoundError):
            heartbeat.path.unlink()
        for tracker in trackers.values():
            await tracker.aclose()
        logger.info("symphony: shut down")
