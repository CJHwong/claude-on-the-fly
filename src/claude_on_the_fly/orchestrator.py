"""Session management, message queuing, and agent execution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from claude_on_the_fly import (
    agent,
    broker,
    commands,
    cotf_approve,
    egress,
    logs,
    permissions,
    sandbox,
    settings,
)
from claude_on_the_fly import approvals as approvals_mod
from claude_on_the_fly.agent import (
    DATA_DIR,
    ClaudeUnavailableError,
    Response,
    current_backend_key,
)
from claude_on_the_fly.approvals import ApprovalBroker
from claude_on_the_fly.events import (
    EVENT_DISPATCHED,
    EVENT_WORKER_DONE,
    EVENT_WORKER_FAILED,
    EventLog,
)
from claude_on_the_fly.heartbeat import HeartbeatWriter
from claude_on_the_fly.jobs.orphans import ProcessLedger
from claude_on_the_fly.protocol import Frontend

logger = logging.getLogger(__name__)

# Percentage of the model's context window above which the next inbound message
# is preceded by a compaction. Unset or 0 disables it, which is the default:
# compaction is a full-context pass, so firing it unasked costs real money.
#
# Read per-instance rather than at import: `load_dotenv()` runs after this module is
# imported, and the config file can change under a running daemon either way. Only pty
# mode can supply the reading it compares against
# (see `Response.context_tokens`), so in native mode this is inert however it is
# set — the manual trigger is the whole feature there.
AUTO_COMPACT_PCT_VAR = "COTF_AUTO_COMPACT_PCT"


@dataclass(frozen=True)
class Turn:
    """One item of queued work for a chat.

    A compaction is queued as a turn rather than run inline so it inherits the
    whole per-turn lifecycle — the reaction, the live status ticker, `$stop`, and
    the event-log entry — and so it takes its place in FIFO order ahead of the
    message that triggered it.
    """

    text: str
    compact: bool = False


class SessionEgress:
    """One CONNECT proxy, with its own grant store, per session.

    Per-session rather than one daemon-wide proxy for two reasons that are really
    the same reason:

    - **Attribution.** A CONNECT carries a hostname and nothing else, so a shared
      proxy cannot tell which of several concurrently running chats made it. The
      port is the only available label, so giving each session its own port is
      what lets the approval prompt land in the conversation that caused it.
    - **Grant scope.** A grant lives on an ApprovalBroker's store. Share the
      broker and approving a host in one chat silently authorizes it for every
      other chat and for cron. One store per session confines it.

    A session is (chat_id, session_uuid), so `/new` earns a fresh proxy and drops
    the previous session's grants rather than inheriting them.
    """

    def __init__(self, frontend: Frontend) -> None:
        self._frontend = frontend
        self._proxies: dict[int, tuple[str, egress.EgressProxy]] = {}

    async def env_for(self, chat_id: int, session: str) -> dict[str, str]:
        """Proxy env for this session, starting a proxy the first time."""
        existing = self._proxies.get(chat_id)
        if existing is not None and existing[0] == session:
            return existing[1].proxy_env()
        if existing is not None:
            # Session changed under this chat: the old grants died with it.
            await existing[1].stop()
            logger.info("egress: chat %s session changed, grants dropped", chat_id)
        # Both the proxy and its grant store carry the chat label, so every gate
        # decision in the log names the conversation it belongs to. Without it two
        # concurrent chats reaching the same host are indistinguishable, and the
        # per-session confinement this class exists for cannot be verified.
        label = f"chat {chat_id}"
        approvals = ApprovalBroker(
            approvals_mod.gate_from_frontend(self._frontend, chat_id),
            policy=approvals_mod.ApprovalPolicy(never_ask=egress.never_ask_subjects()),
            label=label,
        )
        proxy = egress.EgressProxy(approvals, label=label)
        await proxy.start()
        self._proxies[chat_id] = (session, proxy)
        logger.info(
            "egress: chat %s -> 127.0.0.1:%d (own grant store, session %s)",
            chat_id,
            proxy.port,
            session[:8],
        )
        return proxy.proxy_env()

    async def close_all(self) -> None:
        """Revoke every session's egress at once."""
        for _session, proxy in list(self._proxies.values()):
            await proxy.stop()
        self._proxies.clear()


class SessionPermissions:
    """One approval service, with its own grant store, per session.

    Mirrors SessionEgress, for the same two reasons: a grant must not leak into
    another chat, and the prompt has to land in the conversation that caused it.
    Separate from SessionEgress rather than folded into it because a deployment can
    run either without the other -- egress gating is COTF_SANDBOX, tool approvals
    are `permissions.mode`, and neither implies the other.
    """

    def __init__(self, frontend: Frontend) -> None:
        self._frontend = frontend
        self._services: dict[int, tuple[str, permissions.PermissionService]] = {}
        # chat_id -> the service's request total as of the end of the last turn.
        # The guard needs a per-turn delta, not a lifetime count: a session that
        # asked once and then lost its gate would otherwise pass every later check
        # on the strength of that one early request.
        self._asked_before_turn: dict[int, int] = {}

    async def env_for(
        self, chat_id: int, session: str, workspace: Path
    ) -> dict[str, str]:
        """Approval env for this session, starting a service the first time."""
        resolved = permissions.configured()
        if not resolved.enabled:
            return {}
        existing = self._services.get(chat_id)
        if existing is not None and existing[0] == session:
            existing[1].update_timing(
                ttl_seconds=resolved.ttl_seconds,
                timeout_seconds=resolved.timeout_seconds,
            )
            return self._env(existing[1])
        if existing is not None:
            await existing[1].stop()
            # The replacement service starts its count at zero, so a stale
            # baseline here would make the first turn's delta negative.
            self._asked_before_turn.pop(chat_id, None)
            logger.info("permissions: chat %s session changed, grants dropped", chat_id)
        label = f"chat {chat_id}"
        service = permissions.PermissionService(
            broker=ApprovalBroker(
                approvals_mod.gate_from_frontend(self._frontend, chat_id),
                policies={"tool": approvals_mod.tool_policy()},
                timeout_seconds=resolved.timeout_seconds,
                label=label,
            ),
            workspace=workspace,
            ttl_seconds=resolved.ttl_seconds,
            label=label,
            tmux_session=permissions.tmux_session_name(chat_id, session),
            notify=self._notifier(chat_id),
        )
        await service.start()
        self._services[chat_id] = (session, service)
        logger.info(
            "permissions: chat %s -> 127.0.0.1:%d (own grant store, session %s, "
            "pane %s)",
            chat_id,
            service.port,
            session[:8],
            service.tmux_session,
        )
        return self._env(service)

    def _notifier(self, chat_id: int) -> Callable[[str], Awaitable[None]]:
        """How a permission service reaches the conversation it belongs to.

        A plain message rather than an approval card: these are reports of a gate
        that could not function, not questions, and offering buttons for something
        already decided would only invite a tap that does nothing.
        """

        async def send(text: str) -> None:
            await self._frontend.send(chat_id, Response(body=text))

        return send

    @staticmethod
    def _env(service: permissions.PermissionService) -> dict[str, str]:
        """What a spawned agent needs to reach this service.

        The pane name is published too, because claude-pty picks its own otherwise
        and the daemon has to know where an approval keystroke goes.
        """
        return {
            cotf_approve.ENDPOINT_ENV: service.base_url + permissions.DECIDE_PATH,
            cotf_approve.NOTIFY_ENV: service.base_url + permissions.NOTIFY_PATH,
            cotf_approve.REQUEST_TIMEOUT_ENV: str(service.broker.timeout_seconds + 5),
            permissions.TMUX_SESSION_ENV: service.tmux_session,
            **permissions.pty_env(),
        }

    def check_turn(self, chat_id: int, response: Response, backend: str) -> None:
        """Report a turn that used tools without the gate ever being asked.

        codex runs the command when its hook is untrusted or crashes, so that
        failure is invisible from the outside: the operator sees an ordinary turn
        and assumes it was supervised. Comparing the turn's own tool count against
        what the service was asked is the only place the two facts meet.

        Compares a per-turn delta rather than the service's lifetime total. With the
        total, a session that asked about one thing early and then lost its gate
        would pass every subsequent check forever on the strength of that one
        request -- which is the failure mode most worth catching, since a gate that
        never worked at all is far more likely to be noticed.
        """
        entry = self._services.get(chat_id)
        if entry is None:
            return
        total = entry[1].requests_seen
        asked_this_turn = total - self._asked_before_turn.get(chat_id, 0)
        self._asked_before_turn[chat_id] = total
        permissions.warn_if_ungated(
            sum(response.tool_counts.values()),
            asked_this_turn,
            backend=backend,
        )

    async def close_all(self) -> None:
        for _session, service in list(self._services.values()):
            await service.stop()
        self._services.clear()


class Orchestrator:
    def __init__(
        self,
        frontend: Frontend,
        platform: str,
        event_log: EventLog | None = None,
        egress_manager: SessionEgress | None = None,
        permissions_manager: SessionPermissions | None = None,
        command_broker: commands.CommandBroker | None = None,
    ) -> None:
        # None when sandboxing is off: no proxy, no per-session env, and the
        # spawn sites behave exactly as they did before any of this existed.
        self._egress = egress_manager
        # None when approvals are off, which keeps the spawn env untouched.
        self._permissions = permissions_manager
        # Daemon-wide service, but each turn receives a token bound to its own
        # workspace before the backend process is spawned.
        self._commands = command_broker
        self._frontend = frontend
        self._platform = platform
        self._running: dict[int, asyncio.Task] = {}
        # Session discriminator per chat: cron bumps an int via
        # reset_session; telegram /new pins a string token via set_session_token.
        # Either feeds session_uuid's `{chat_id}-{value}` tag.
        self._session_counters: dict[int, int | str] = {}
        self._queues: dict[int, asyncio.Queue[Turn]] = {}
        # Last turn's prompt size and window per chat, for the auto-compact gate.
        # Only pty mode populates it; elsewhere the gate never has a reading and
        # so never fires.
        self._context: dict[int, tuple[int, int]] = {}
        # None means use the live setting. Tests and embedders may assign an
        # explicit threshold through the compatibility property below.
        self._auto_compact_pct_override: int | None = None
        self._event_log = event_log if event_log is not None else EventLog()
        # chat_id -> {identifier, started_at_monotonic, session_uuid}.
        # Populated at dispatch, cleared on completion. Drives the heartbeat
        # `running_jobs` slot consumed by the TUI's Active AI jobs pane.
        self._in_flight: dict[int, dict] = {}
        # Restart-required config fields already reported. Compared as a set rather
        # than a flag so a second edit is reported too, and reverting one clears it.
        self._restarts_reported: tuple[str, ...] = ()

    def session_uuid(self, chat_id: int) -> str:
        counter = self._session_counters.get(chat_id, 0)
        tag = f"{chat_id}" if counter == 0 else f"{chat_id}-{counter}"
        return str(uuid5(NAMESPACE_URL, tag))

    def reset_session(self, chat_id: int) -> None:
        # Cron uses an int counter here; telegram pins a str token via
        # set_session_token. Only the int form is bumped (different chat_id
        # spaces), so coerce defensively to keep the +1 well-typed.
        current = self._session_counters.get(chat_id, 0)
        self._session_counters[chat_id] = (
            current if isinstance(current, int) else 0
        ) + 1
        self._forget_context(chat_id)

    def set_session_token(self, chat_id: int, token: str) -> None:
        """Pin the session discriminator to a token the frontend minted, so the
        session UUID matches the frontend's workspace suffix (telegram's /new
        uses a unique timestamp token). The tag formatting in session_uuid
        accepts a string just as it does cron's integer counter, which
        reset_session still bumps."""
        self._session_counters[chat_id] = token
        self._forget_context(chat_id)

    def _forget_context(self, chat_id: int) -> None:
        """Drop this chat's context reading because its session changed.

        The reading is keyed by chat, but it describes a *session* — and both
        callers above repoint a chat at a fresh one. Cron does this
        before every fire, so without this a big reading from the last fire would
        survive into the next and queue a compaction against a session that has
        nothing in it yet.
        """
        self._context.pop(chat_id, None)

    def is_busy(self, chat_id: int) -> bool:
        return chat_id in self._running and not self._running[chat_id].done()

    def queue_size(self, chat_id: int) -> int:
        queue = self._queues.get(chat_id)
        return queue.qsize() if queue else 0

    async def abort(self, chat_id: int) -> bool:
        """Stop the in-flight turn for a chat and drop anything queued behind it.

        Cancelling the drain task raises CancelledError into the awaited
        agent.run; the backend's exec finally reaps the whole process tree
        (spawned with start_new_session), so the agent CLI and its tool
        subprocesses die together instead of orphaning. Returns whether a turn
        was actually running.
        """
        queue = self._queues.get(chat_id)
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        task = self._running.get(chat_id)
        if task is None or task.done():
            return False
        logger.info("abort: chat_id=%s cancelling in-flight turn", chat_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def on_message(self, chat_id: int, text: str) -> None:
        logger.debug("on_message: chat_id=%s text=%s", chat_id, logs.redact(text))
        if self._due_for_compaction(chat_id):
            # Ahead of the message, not during the idle window before it. An idle
            # thread may never be spoken to again, and compacting one that isn't
            # pays a full-context pass for nothing. Waiting until someone
            # actually comes back costs them this turn's latency and saves every
            # turn after it.
            logger.info("on_message: chat_id=%s auto-compacting first", chat_id)
            await self.on_compact(chat_id)
        await self._enqueue(chat_id, Turn(text))

    async def on_compact(self, chat_id: int) -> None:
        """Queue a compaction for this chat. Runs in FIFO order like any turn."""
        await self._enqueue(chat_id, Turn("", compact=True))

    async def _enqueue(self, chat_id: int, turn: Turn) -> None:
        if chat_id not in self._queues:
            self._queues[chat_id] = asyncio.Queue()
        self._queues[chat_id].put_nowait(turn)
        if self.is_busy(chat_id):
            queued = self._queues[chat_id].qsize()
            logger.debug("enqueue: chat_id=%s busy, queued=%s", chat_id, queued)
            await self._frontend.notify_queued(chat_id, queued)
        else:
            logger.debug("enqueue: chat_id=%s starting drain", chat_id)
            self._running[chat_id] = asyncio.create_task(self._drain(chat_id))

    def _due_for_compaction(self, chat_id: int) -> bool:
        """Whether this chat's last turn left the context over the threshold.

        Consumes the reading, so two messages arriving back to back queue one
        compaction rather than two — the second would find nothing to compact and
        bill a full-context pass to be told so.
        """
        threshold = self._auto_compact_pct
        if not threshold:
            return False
        reading = self._context.get(chat_id)
        if reading is None:
            return False
        tokens, window = reading
        if window <= 0:
            return False
        pct = tokens * 100 / window
        if pct < threshold:
            return False
        self._context.pop(chat_id, None)
        logger.info(
            "auto-compact: chat_id=%s context %.0f%% (%s/%s) >= %s%%",
            chat_id,
            pct,
            tokens,
            window,
            threshold,
        )
        return True

    @property
    def _auto_compact_pct(self) -> int:
        if self._auto_compact_pct_override is not None:
            return self._auto_compact_pct_override
        return _auto_compact_pct()

    @_auto_compact_pct.setter
    def _auto_compact_pct(self, value: int) -> None:
        self._auto_compact_pct_override = value

    async def _drain(self, chat_id: int) -> None:
        queue = self._queues[chat_id]
        try:
            while True:
                try:
                    turn = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self._process(chat_id, turn)
        finally:
            if self._running.get(chat_id) is asyncio.current_task():
                self._running.pop(chat_id, None)

    async def _typing_loop(self, chat_id: int) -> None:
        while True:
            await self._frontend.send_typing(chat_id)
            await asyncio.sleep(4)

    async def _report_config_restarts(self, chat_id: int) -> None:
        """Name any config edit that this turn will not honour.

        Checked per turn, and reported into the conversation the operator is
        already in, because that is where they will be right after saving the file:
        most of `config.yaml` takes effect on the next read, so the only edits
        worth interrupting for are the ones where saving is *not* enough.

        Reported once per distinct set. `settings.check_reload` compares against
        the startup baseline and so keeps returning the same answer until a
        restart, which sending every turn would turn into noise nobody reads.

        Frontend failures are swallowed on purpose. A missed notice is a worse log
        line; an exception here would kill the drain task with turns still queued.
        """
        changed = settings.check_reload()
        if changed == self._restarts_reported:
            return
        self._restarts_reported = changed
        if not changed:
            return
        logger.warning(
            "settings: %s changed in %s and needs a daemon restart to take effect",
            ", ".join(changed),
            settings.operator_settings(),
        )
        body = (
            f"Config change to {', '.join(changed)} needs a restart. The rest of "
            f"{settings.FILENAME} is picked up on its own, but this part is read "
            "once at startup, so this turn and the ones after it still run the old "
            "value."
        )
        try:
            await self._frontend.send(chat_id, Response(body=body))
        except Exception:
            logger.exception("settings: could not report the restart-required change")

    async def _process(self, chat_id: int, turn: Turn) -> None:
        text = turn.text
        await self._report_config_restarts(chat_id)
        workspace = DATA_DIR / "workspaces" / self._frontend.workspace_name(chat_id)
        workspace.mkdir(parents=True, exist_ok=True)
        if self._platform in agent.ATTACHMENT_PLATFORMS:
            (workspace / agent.OUTBOX_DIRNAME).mkdir(exist_ok=True)
        agent.ensure_persona(workspace)
        session = self.session_uuid(chat_id)
        identifier = self._frontend.workspace_name(chat_id)
        logger.debug(
            "process: chat_id=%s workspace=%s session=%s", chat_id, workspace, session
        )

        self._event_log.append(
            EVENT_DISPATCHED,
            source=self._platform,
            backend=current_backend_key(),
            identifier=identifier,
            workspace=workspace,
            session_uuid=session,
        )
        self._in_flight[chat_id] = {
            "identifier": identifier,
            "started_at_monotonic": time.monotonic(),
            "session_uuid": session,
        }

        # Startup notification performs frontend I/O and is therefore a
        # cancellation point. Keep it inside the lifecycle try/finally so an
        # abort while it is in progress still clears the in-flight slot and
        # any partially-applied frontend status/reaction.
        typing_task: asyncio.Task | None = None
        env_token = None
        command_token: str | None = None
        try:
            # Point this turn's agent at its own egress proxy. Set here rather
            # than passed down because the spawn is several frames below, inside
            # a backend; asyncio copied this task's context at creation, so the
            # value reaches that spawn and no other session's.
            #
            # Inside the try because starting a proxy can fail (a bound port, an
            # exhausted fd table). Outside it, that failure escaped _process
            # entirely: the in-flight slot set just above leaked, notify_complete
            # never ran, and the whole drain task died with turns still queued.
            session_overrides: dict[str, str] = {}
            if self._egress is not None:
                session_overrides.update(await self._egress.env_for(chat_id, session))
            if self._permissions is not None:
                session_overrides.update(
                    await self._permissions.env_for(chat_id, session, workspace)
                )
            if self._commands is not None:
                command_env = self._commands.agent_env(workspace)
                command_token = command_env[commands.TOKEN_ENV]
                session_overrides.update(command_env)
            if session_overrides:
                env_token = sandbox.session_env(session_overrides)
            await self._frontend.notify_start(chat_id)
            typing_task = asyncio.create_task(self._typing_loop(chat_id))
            if turn.compact:
                response = await self._run_compaction(
                    chat_id, workspace, session, self._frontend.timeout_for(chat_id)
                )
            else:
                identity = getattr(self._frontend, "sender_identity", None)
                response = await agent.run(
                    workspace,
                    session,
                    text,
                    self._platform,
                    user_name=(
                        identity(chat_id)
                        if callable(identity)
                        else self._frontend.sender_name(chat_id)
                    ),
                    channel_context=self._frontend.channel_context(chat_id),
                    timeout=self._frontend.timeout_for(chat_id),
                )
            if self._permissions is not None:
                # After the turn, because the tool count only exists once it is
                # over. Reporting late beats not reporting: an ungated turn is
                # exactly what an operator who enabled approvals must never
                # discover by accident.
                self._permissions.check_turn(
                    chat_id, response, settings.get("AGENT_BACKEND", "claude")
                )
            logger.debug(
                "process: chat_id=%s response cost=%.4f tokens_in=%s tokens_out=%s",
                chat_id,
                response.cost,
                response.tokens_in,
                response.tokens_out,
            )
            # pty reports it off the statusline; native (non-ollama) adds it up
            # in `_native_context_fields`; ollama withholds it. A turn that
            # doesn't report sets nothing rather than zeroing the reading, so a
            # mode switch mid-thread can't make a large context look small.
            if response.context_tokens and response.context_window_size:
                self._context[chat_id] = (
                    response.context_tokens,
                    response.context_window_size,
                )
            if self._platform in agent.ATTACHMENT_PLATFORMS:
                response.attachments = agent.collect_outbox(workspace)
            delivered = await self._frontend.send(chat_id, response)
            if delivered:
                agent.archive_outbox(workspace, delivered)
            self._event_log.append(
                EVENT_WORKER_DONE,
                source=self._platform,
                backend=current_backend_key(),
                identifier=identifier,
                workspace=workspace,
                session_uuid=session,
                cost=response.cost,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
            )
        except asyncio.CancelledError:
            logger.info("process: chat_id=%s aborted", chat_id)
            raise
        except ClaudeUnavailableError as exc:
            logger.warning("Claude unavailable for chat %s: %s", chat_id, exc)
            await self._frontend.send(
                chat_id, Response(body=f"Claude unavailable: {exc}")
            )
            self._event_log.append(
                EVENT_WORKER_FAILED,
                source=self._platform,
                backend=current_backend_key(),
                identifier=identifier,
                workspace=workspace,
                session_uuid=session,
                error=str(exc),
                reason="unavailable",
            )
        except Exception as exc:
            logger.exception("Agent error for chat %s", chat_id)
            await self._frontend.send(chat_id, Response(body=f"Error: {exc}"))
            self._event_log.append(
                EVENT_WORKER_FAILED,
                source=self._platform,
                backend=current_backend_key(),
                identifier=identifier,
                workspace=workspace,
                session_uuid=session,
                error=str(exc),
            )
        finally:
            self._in_flight.pop(chat_id, None)
            if typing_task is not None:
                typing_task.cancel()
            if env_token is not None:
                sandbox.reset_session_env(env_token)
            if command_token is not None and self._commands is not None:
                self._commands.revoke_token(command_token)
            await self._frontend.notify_complete(chat_id)

    async def _run_compaction(
        self, chat_id: int, workspace: Path, session: str, timeout: float | None
    ) -> Response:
        """Compact the session and render the outcome as this turn's reply."""
        # Whatever this chat was holding describes the pre-compaction prompt and
        # is stale the moment the compaction lands. Dropping it (rather than
        # guessing the new size) is what stops a manual `$compact` from being
        # followed straight away by an automatic one on the next message.
        self._context.pop(chat_id, None)
        outcome = await agent.compact(workspace, session, timeout=timeout)
        if outcome is None:
            return Response(body="This backend can't compact a conversation.")
        return Response(body=outcome.summary(), compaction=outcome)

    def heartbeat_extra(self) -> dict:
        """Snapshot in-flight chat jobs for the TUI's Active AI jobs pane.

        Shape mirrors the jobs worker's rows so the dashboard can merge
        across sources with a single normalizer.
        """
        now = time.monotonic()
        running_jobs = [
            {
                "identifier": j["identifier"],
                "chat_id": chat_id,
                "uptime_s": int(now - j["started_at_monotonic"]),
                "session_uuid": j["session_uuid"],
            }
            for chat_id, j in self._in_flight.items()
        ]
        return {"running_jobs": running_jobs}

    async def shutdown(self) -> None:
        for task in self._running.values():
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)


def _auto_compact_pct() -> int:
    """Resolve the auto-compact threshold. 0 (off) for unset or unusable values.

    A junk value disables the feature rather than taking the daemon down: this
    is a cost optimization, not something worth refusing to start over. The
    value is logged so a typo doesn't look like a silently working setting.
    """
    raw = settings.get(AUTO_COMPACT_PCT_VAR).strip()
    if not raw:
        return 0
    try:
        pct = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number, auto-compact off", AUTO_COMPACT_PCT_VAR, raw
        )
        return 0
    if not 1 <= pct <= 100:
        logger.warning(
            "%s=%d is outside 1-100, auto-compact off", AUTO_COMPACT_PCT_VAR, pct
        )
        return 0
    return pct


def _redact_token(token: str) -> str:
    """Mask a secret for log output."""
    if not token:
        return "<unset>"
    if len(token) <= 4:
        return "***"
    return f"{token[:2]}***{token[-2:]}"


def _log_settings_summary(platform: str, frontend: Frontend) -> None:
    """Dump the resolved runtime settings at startup.

    Pulls shared bits (log level, data dir, agent backend) from the resolved
    settings, then
    appends frontend-specific fields via Frontend.describe(). Secrets are
    expected to be redacted by the frontend before being returned.
    """
    import os

    backend = settings.get("AGENT_BACKEND", "claude").lower()
    mode_var = f"{backend.upper()}_MODE"
    mode = settings.get(mode_var, "native").lower()

    logger.info("%s settings:", platform)
    logger.info("  platform        = %s", platform)
    logger.info("  log_level       = %s", os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.info("  data_dir        = %s", DATA_DIR)
    logger.info("  agent_backend   = %s", backend)
    logger.info("  %-15s = %s", mode_var.lower(), mode)
    if mode == "ollama":
        logger.info("  ollama_model    = %s", settings.get("OLLAMA_MODEL", "<unset>"))

    for label, value in frontend.describe().items():
        logger.info("  %-15s = %s", label, value)


async def _start_sandbox(
    frontend: Frontend,
) -> tuple[broker.Broker | None, SessionEgress | None, commands.CommandBroker | None]:
    """Bring up the credential broker, per-session egress, and command broker.

    All three None when sandboxing is off, which is what makes the spawn sites
    behave exactly as they did before any of this existed.

    Partial failure tears down what already started and re-raises. Without that,
    a command broker that cannot bind left the credential broker listening with
    every route's key loaded in memory and ANTHROPIC_BASE_URL published, for a
    daemon that was on its way to exiting.
    """
    # Approval artifacts are independent of the sandbox. Generate them before
    # the sandbox-off return so a fresh ask-mode deployment has both backends'
    # prompt/hook wiring available.
    permissions.check()
    if permissions.configured().enabled:
        permissions.write_shim()
        permissions.write_mcp_config()
        permissions.write_pty_settings()
    if not sandbox.enabled():
        return None, None, None
    broker_instance = None
    command_broker = None
    try:
        # The broker is shared: its routes are operator config, not something a
        # session earns, so one credential-holding proxy for the daemon is right.
        # Egress is the opposite -- see SessionEgress for why it is per-session.
        broker_instance = await broker.start_default_broker(
            approvals=ApprovalBroker(
                approvals_mod.gate_from_frontend(frontend),
                policy=approvals_mod.ApprovalPolicy(
                    never_ask=egress.never_ask_subjects(),
                ),
            )
        )
        # The broker is daemon-wide, but its bearer token is per turn so the
        # request can be bound to the originating workspace. Publish only the
        # harmless endpoint here; the session token is layered by _process().
        command_broker = commands.CommandBroker(sandbox.shim_dir())
        await command_broker.start()
        # The endpoint is harmless to publish daemon-wide, but the bearer token
        # must be issued per turn and bound to that turn's workspace. Do not leave
        # the private base token in the daemon environment for sandbox.agent_env
        # to forward accidentally.
        command_env = command_broker.agent_env()
        os.environ.update(
            {
                key: value
                for key, value in command_env.items()
                if key != commands.TOKEN_ENV
            }
        )
        os.environ.pop(commands.TOKEN_ENV, None)
    except Exception:
        logger.exception("sandbox: startup failed, revoking what already started")
        if command_broker is not None:
            await command_broker.stop()
        if broker_instance is not None:
            await broker_instance.stop()
        raise
    logger.info(
        "sandbox: mode=%s broker=%s egress=per-session commands=%s content_log=%s",
        sandbox.mode(),
        "on" if broker_instance else "none",
        ",".join(command_broker.shimmed) or "none",
        "on" if logs.log_content() else "redacted",
    )
    # macOS cannot report a seatbelt denial, so the agent's own blocked reads are
    # permanently invisible. Probing the denies here is the substitute: it records,
    # per run, that the boundary was actually in force rather than inferring it
    # from an absence of errors.
    await sandbox.verify_denials()
    return broker_instance, SessionEgress(frontend), command_broker


async def run(frontend: Frontend, platform: str) -> None:
    """Start the orchestrator with the given frontend. Blocks until SIGINT/SIGTERM."""
    from claude_on_the_fly import logs

    logs.configure(platform)
    (DATA_DIR / "memory" / "users").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)

    _log_settings_summary(platform, frontend)

    # Before anything reads the policy: seed the operator's file if it is missing,
    # name any problem with it now, and record what the restart-required fields
    # were, so a later edit to one can be reported rather than silently ignored.
    #
    # Unconditional, where this used to sit inside the sandbox branch. The file
    # holds more than sandbox policy now, and `permissions:` in particular is read
    # with the sandbox off -- so gating the seeding and the validation on
    # `sandbox.enabled()` meant the one deployment shape that most needs the
    # diagnostics was the shape that never got them.
    settings.check_operator_settings()

    # Agent CLIs are separate process groups. Recover groups left by a forced
    # daemon stop before accepting new work, and record every live group so the
    # supervisor can reap it even if this process is SIGKILLed.
    process_ledger = ProcessLedger(DATA_DIR / "state" / f"{platform}.pids")
    process_ledger.sweep()
    agent.add_process_listener(process_ledger.on_process)

    try:
        # When sandboxing is enabled, the broker holds the real API keys and the
        # agent reaches them only through loopback. start_default_broker publishes
        # base-urls into os.environ that sandbox.agent_env forwards to the agent.
        #
        # The egress proxy covers everything the broker cannot: it gates ordinary
        # HTTPS by destination host and, via the approval broker, can ask the
        # operator to grant an unknown one mid-run instead of failing the task.
        broker_instance, session_egress, command_broker = await _start_sandbox(frontend)

        # Approvals are independent of COTF_SANDBOX, so this is built whenever the
        # config asks for it rather than only inside the sandbox branch.
        session_permissions = (
            SessionPermissions(frontend) if permissions.configured().enabled else None
        )
        orch = Orchestrator(
            frontend,
            platform,
            egress_manager=session_egress,
            permissions_manager=session_permissions,
            command_broker=command_broker,
        )
        frontend.set_orchestrator(orch)

        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        heartbeat = HeartbeatWriter(platform, extra_provider=orch.heartbeat_extra)
        heartbeat_task = asyncio.create_task(heartbeat.run())

        frontend_task = asyncio.create_task(frontend.start(orch.on_message))
        logger.info("Running (%s). Ctrl+C to stop.", platform)

        await stop.wait()

        logger.info("Shutting down...")
        heartbeat_task.cancel()
        frontend_task.cancel()
        await asyncio.gather(heartbeat_task, frontend_task, return_exceptions=True)
        await orch.shutdown()
        await frontend.stop()
        # Stopping these revokes every route out of the sandbox at once.
        if session_egress is not None:
            await session_egress.close_all()
        if session_permissions is not None:
            await session_permissions.close_all()
        if command_broker is not None:
            await command_broker.stop()
        if broker_instance is not None:
            await broker_instance.stop()
        with contextlib.suppress(FileNotFoundError):
            heartbeat.path.unlink()
    finally:
        # Startup or frontend failures can happen before the normal shutdown
        # sequence. Never leave a durable process listener attached to the module.
        agent.remove_process_listener(process_ledger.on_process)
