"""Symphony orchestrator: poll Jira, dispatch claimed tickets, drive continuation turns."""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from claude_on_the_fly.agent import ClaudeUnavailableError

from .agent_runner import TicketRunner, session_uuid_for
from .config import SymphonyConfig, TrackerConfig, load_config
from .prompt import PromptStore
from .state import OrchestratorState
from .tracker.issue import Issue
from .tracker.jira import JiraTracker
from .workspace import ensure_workspace, remove_workspace

logger = logging.getLogger(__name__)


def _eligible(
    issue: Issue, state: OrchestratorState, tracker_cfg: TrackerConfig
) -> bool:
    if state.is_claimed(issue.id):
        return False
    if state.is_exhausted(issue.id):
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
    fetched: Iterable[Issue], state: OrchestratorState, tracker_cfg: TrackerConfig
) -> list[Issue]:
    return sorted(
        [i for i in fetched if _eligible(i, state, tracker_cfg)], key=_sort_key
    )


async def _run_worker(
    issue: Issue,
    state: OrchestratorState,
    tracker: JiraTracker,
    config: SymphonyConfig,
    prompt_source: str,
) -> None:
    identifier = issue.identifier
    try:
        workspace = ensure_workspace(issue, config.worktree_root)
    except Exception:
        logger.exception("[%s] worker: workspace prep failed", identifier)
        state.release(issue.id)
        return

    sid = session_uuid_for(identifier)
    runner = TicketRunner(
        issue=issue,
        workspace=workspace,
        config=config,
        prompt_source=prompt_source,
        session_uuid=sid,
    )
    logger.info(
        "[%s] worker started: workspace=%s session=%s",
        identifier,
        workspace.path,
        sid,
    )

    try:
        for attempt in range(config.max_turns):
            try:
                response = await runner.run_turn(attempt)
            except ClaudeUnavailableError as exc:
                logger.warning(
                    "[%s] claude unavailable (attempt %d): %s; parking until daemon restart",
                    identifier,
                    attempt,
                    exc,
                )
                state.mark_exhausted(issue.id)
                return
            logger.info(
                "[%s] turn %d done | %s | %s",
                identifier,
                attempt,
                response.format_stats(),
                response.format_tools() or "-",
            )

            try:
                refreshed = await tracker.fetch_one(identifier)
            except Exception:
                logger.exception(
                    "[%s] failed to refresh issue after turn; releasing", identifier
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
                    "[%s] state %s is neither active nor terminal; leaving workspace",
                    identifier,
                    current_state,
                )
                return

            runner.issue = refreshed
            state.update_running_state(issue.id, refreshed.state)

        logger.warning(
            "[%s] max_turns=%d reached; parking until daemon restart",
            identifier,
            config.max_turns,
        )
        state.mark_exhausted(issue.id)
    except Exception:
        logger.exception(
            "[%s] worker crashed; parking until daemon restart", identifier
        )
        state.mark_exhausted(issue.id)
    finally:
        state.release(issue.id)


async def tick(
    state: OrchestratorState,
    config: SymphonyConfig,
    prompt_source: str,
    tracker: JiraTracker,
    pending_tasks: set[asyncio.Task[None]],
) -> None:
    """One scheduling pass: poll, sort, dispatch up to capacity."""
    if state.running_count() >= config.max_concurrent:
        return

    try:
        fetched = await tracker.fetch_candidates(config.tracker)
    except Exception:
        logger.exception("tick: fetch_candidates failed; skipping dispatch")
        return

    candidates = _select_candidates(fetched, state, config.tracker)
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
        try:
            entry = state.claim(issue)
        except RuntimeError:
            continue
        task = asyncio.create_task(
            _run_worker(issue, state, tracker, config, prompt_source),
            name=f"symphony-worker-{issue.identifier}",
        )
        entry.task = task
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)
        logger.info(
            "[%s] dispatched (state=%s, prio=%s, labels=%s)",
            issue.identifier,
            issue.state,
            issue.priority,
            list(issue.labels),
        )


async def run_loop(config_path, stop_event: asyncio.Event) -> None:
    """Main daemon loop. Hot-reloads prompt on mtime change; ticks at configured cadence."""
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
    pending_tasks: set[asyncio.Task[None]] = set()

    logger.info(
        "symphony: started (poll every %dms, prompt=%s)",
        config.polling_ms,
        config.prompt_path,
    )

    try:
        while not stop_event.is_set():
            prompt_source = prompt_store.maybe_reload()

            try:
                await tick(state, config, prompt_source, tracker, pending_tasks)
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
