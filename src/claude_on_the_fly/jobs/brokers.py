"""The chat daemon's sandbox services, for work nobody can be asked about.

A chat turn gets a command broker token bound to its workspace, a credential
broker, an egress proxy, and on a Linux jail a relay that bridges those loopback
ports into its namespace. A job got none of them, so under a sandbox every
brokered tool failed, and under a Linux jail the job could not reach its model.

A job runs unattended, so there is nobody to answer a prompt. Every approval here
goes to `DenyAllGate`: a job touches the commands and hosts already allowed, and
anything that would have asked the operator is refused instead.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from claude_on_the_fly import approvals, broker, commands, egress, sandbox

logger = logging.getLogger(__name__)


def _never_asking(label: str = "") -> approvals.ApprovalBroker:
    return approvals.ApprovalBroker(
        approvals.DenyAllGate(),
        policy=approvals.ApprovalPolicy(never_ask=egress.never_ask_subjects()),
        label=label,
    )


class JobBrokers:
    """Daemon-wide brokers for the jobs worker, and each job's share of them.

    The command broker and the credential broker are shared, as in the chat
    daemon: their routes are operator config, not something a job earns. The
    command token and the egress proxy are per job, so a token dies with its job
    and a job's egress refusals are logged under its own label.

    The command broker writes the same shim directory the chat daemon does. That
    is safe because a shim carries no endpoint of its own: both daemons read the
    same `commands` section and write the same files, and each spawn finds its
    daemon's endpoint in its environment.
    """

    def __init__(self, tools: tuple[commands.ShimmedTool, ...] | None = None) -> None:
        self._tools = tools
        self._credentials: broker.Broker | None = None
        self._commands: commands.CommandBroker | None = None

    async def start(self) -> None:
        if not sandbox.enabled():
            return
        try:
            self._credentials = await broker.start_default_broker(
                approvals=_never_asking()
            )
            self._commands = commands.CommandBroker(sandbox.shim_dir(), self._tools)
            await self._commands.start()
        except Exception:
            logger.exception(
                "jobs: sandbox services failed to start, revoking what already started"
            )
            await self.stop()
            raise
        self._commands.publish_endpoint()
        logger.info(
            "jobs: sandbox services started (credential broker=%s, commands=%s, "
            "approvals denied without asking)",
            "on" if self._credentials else "none",
            ",".join(self._commands.shimmed) or "none",
        )

    async def stop(self) -> None:
        if self._commands is not None:
            await self._commands.stop()
            self._commands = None
            os.environ.pop(commands.ENDPOINT_ENV, None)
        if self._credentials is not None:
            await self._credentials.stop()
            self._credentials = None

    @contextlib.asynccontextmanager
    async def for_job(
        self, workspace: Path, key: str, model_env: Mapping[str, str] | None = None
    ) -> AsyncIterator[dict[str, str]]:
        """The env one job's agent needs, torn down when the job ends.

        `model_env` names the job's model server (`sandbox.model_endpoint_env`),
        so the relay bridges it along with the brokers.
        """
        command_broker = self._commands
        if command_broker is None:
            yield {}
            return
        overrides: dict[str, str] = {}
        proxy: egress.EgressProxy | None = None
        token: str | None = None
        relay: sandbox.SessionRelay | None = None
        try:
            if sandbox.egress_proxy_enabled():
                label = f"job {key}"
                proxy = egress.EgressProxy(
                    _never_asking(label), label=label, ask=sandbox.egress_asks()
                )
                await proxy.start()
                overrides.update(proxy.proxy_env())
            command_env = command_broker.agent_env(workspace)
            token = command_env[commands.TOKEN_ENV]
            overrides.update(command_env)
            overrides.update(model_env or {})
            # After the overrides are known: on a Linux jail the namespace holds
            # nothing until this bridges their ports in, the job's own egress
            # proxy among them. Inert everywhere else.
            relay = await sandbox.open_session_relay(overrides, key)
            yield overrides
        finally:
            if relay is not None:
                await relay.close()
            if token is not None:
                command_broker.revoke_token(token)
            if proxy is not None:
                await proxy.stop()
