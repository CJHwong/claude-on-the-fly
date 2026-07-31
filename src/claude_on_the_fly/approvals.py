"""Runtime permission grants: ask the operator, then widen policy in place.

A deny-by-default egress policy has one practical problem: the allowlist is
never complete on day one, and every gap surfaces as a failed run. This module
turns a denial into a question. The requester (the broker, the CONNECT proxy)
blocks on `ApprovalGate.request`; the operator answers on whatever frontend is
attached; an approval writes a scoped, expiring grant that the requester
consults from then on.

Nothing here trusts the agent. Requests are built from what the *proxy*
observed (the host it tried to reach, the method it used), never from text the
agent authored, because an agent under injection would otherwise write its own
justification. See docs/agent/broker.md for the threat model.

Three limits keep the ask channel from becoming the weak point:

  never-ask   Some subjects are refused without asking. An operator tapping
              "approve" on a phone is the softest link in the chain, so the
              things an injection payload wants most are not offered at all.
  rate limit  A burst of requests is one signal, not N independent decisions.
              Past the threshold the gate denies without asking.
  expiry      Every grant carries a TTL and dies with the process. Persisting
              one is a separate, deliberate act that this module does not do.

Decisions are logged at INFO through the standard logging path, which is the
grant ledger: logs.py already owns naming, rollover, and retention.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

# Default seconds an operator has to answer before the gate gives up and
# denies. Bounded because the caller is holding a live HTTP connection open;
# both the Anthropic SDK and codex impose their own request timeouts, so a
# wait longer than this produces a client-side error rather than a grant.
DEFAULT_TIMEOUT_SECONDS = 90.0

# Default lifetime of an approved grant. Session-scoped by intent: the store
# lives in the daemon process, so a restart clears everything regardless.
DEFAULT_TTL_SECONDS = 3600.0

# Requests allowed inside _RATE_WINDOW_SECONDS before the gate stops asking.
# A hijacked agent probing for an open host generates a burst; a human doing
# real work generates a trickle.
_RATE_LIMIT = 10
_RATE_WINDOW_SECONDS = 600.0

# The same ceiling is wrong for tool permissions. Ten questions per ten minutes
# suits egress, where a burst of distinct hosts is the signal itself. A supervised
# turn legitimately asks far more often than that -- a first pass over an
# unfamiliar repo produced prompts for chmod, find, sudo, curl and git init inside
# one turn -- and hitting the ceiling auto-denies the rest silently, which reads
# to the operator as the agent giving up for no reason. Budgets are per kind so
# neither shape can spend the other's.
TOOL_RATE_LIMIT = 60
TOOL_RATE_WINDOW_SECONDS = 600.0

# How long a denial suppresses the same question. Agents retry hard: a live run
# with codex produced 50 prompts for one denied host, because a "no" was not
# remembered at all and each retry asked again. Short enough that the operator
# can still change their mind later in the run, long enough to absorb a retry
# storm. Distinct from the rate limit, which bounds *distinct* subjects.
_DENY_COOLDOWN_SECONDS = 120.0


@dataclass(frozen=True)
class ApprovalRequest:
    """One permission question, built from observed facts only.

    :param kind: Requester-defined category, e.g. "host" or "route-scope".
        Used for grant-store keying and for the operator-facing label.
    :param subject: The exact thing being requested, e.g. "api.github.com:443".
        This is the grant key, so it must be the full precise scope.
    :param detail: Mechanical context for the operator: what tried to do what.
        Never agent-authored prose.
    :param ttl_seconds: How long an approval lasts.
    """

    kind: str
    subject: str
    detail: str
    ttl_seconds: float = DEFAULT_TTL_SECONDS

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.subject}"


class ApprovalGate(Protocol):
    """Asks a human whether to grant a request. Must fail closed."""

    async def request(self, req: ApprovalRequest) -> bool:
        """Return True to grant. Any error, timeout, or absent operator is False."""
        ...


class DenyAllGate:
    """The safe default: never asks, never grants.

    Used when no frontend is attached or no operator chat is configured, so an
    unconfigured deployment behaves exactly like the pre-approval build.
    """

    async def request(self, req: ApprovalRequest) -> bool:
        logger.info("approval: deny %s (no approval channel configured)", req.key)
        return False


class GrantStore:
    """Live, expiring grants consulted by the broker and the CONNECT proxy.

    Keyed by `ApprovalRequest.key`. Expiry is checked on read rather than swept
    on a timer: reads are frequent and cheap, and a lazily-expired entry can
    never be observed as valid.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._expiry: dict[str, float] = {}

    def grant(self, key: str, ttl_seconds: float) -> None:
        self._expiry[key] = self._clock() + ttl_seconds

    def allows(self, key: str) -> bool:
        expires_at = self._expiry.get(key)
        if expires_at is None:
            return False
        if self._clock() >= expires_at:
            del self._expiry[key]
            return False
        return True

    def active(self) -> list[str]:
        """Unexpired grant keys, for the startup summary and heartbeat."""
        return [key for key in list(self._expiry) if self.allows(key)]


@dataclass
class ApprovalPolicy:
    """What the gate refuses to even ask about, and how often it will ask.

    :param never_ask: Subjects that are denied without reaching the operator.
        Matched by exact subject or, when an entry ends in "*", by prefix.
    :param rate_limit: Requests permitted per window before auto-deny.
    :param window_seconds: The rate-limit window.
    """

    never_ask: frozenset[str] = frozenset()
    rate_limit: int = _RATE_LIMIT
    window_seconds: float = _RATE_WINDOW_SECONDS
    deny_cooldown_seconds: float = _DENY_COOLDOWN_SECONDS

    def refuses(self, subject: str) -> bool:
        for entry in self.never_ask:
            if entry.endswith("*") and subject.startswith(entry[:-1]):
                return True
            if entry == subject:
                return True
        return False


def tool_policy() -> ApprovalPolicy:
    """The policy for `kind="tool"` requests: the same shape, a looser ceiling.

    Nothing is never-asked here. The egress never-ask tier exists because a
    metadata endpoint is a subject no legitimate task needs and an operator could
    be talked into approving; a tool call has no equivalent set, and inventing one
    would be a denylist pretending to be a boundary.
    """
    return ApprovalPolicy(
        rate_limit=TOOL_RATE_LIMIT,
        window_seconds=TOOL_RATE_WINDOW_SECONDS,
    )


class ApprovalBroker:
    """Wraps a gate with the grant store, the never-ask tier, and rate limiting.

    This is what requesters depend on. `check` is the whole contract: it returns
    True if the subject is already granted or the operator grants it now, and
    False for everything else including every failure path.

    Concurrent requests for the same subject collapse onto one question: the
    first caller asks, the rest await the same answer. Without this, an agent
    retrying a blocked host three times would spam the operator with three
    identical prompts.
    """

    def __init__(
        self,
        gate: ApprovalGate,
        *,
        policy: ApprovalPolicy | None = None,
        policies: dict[str, ApprovalPolicy] | None = None,
        store: GrantStore | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        label: str = "",
    ) -> None:
        self._gate = gate
        # `policy` stays the default for every kind, so the egress caller that
        # predates this is unchanged. `policies` overrides it per kind, which is
        # what keeps a turn's worth of tool prompts from spending the budget that
        # exists to blunt host probing.
        self._policy = policy or ApprovalPolicy()
        self._policies = dict(policies or {})
        self._store = store or GrantStore(clock=clock)
        self._timeout = timeout_seconds
        self._clock = clock
        # Brokers are per-session, so a grant in one chat is invisible to
        # another. Without this the log cannot show which session's store a
        # decision landed in, and the confinement is unverifiable.
        self._label = label
        # kind -> when each recent question was asked. Separate deques rather than
        # one shared, for the same reason the policies are separate.
        self._recent: dict[str, deque[float]] = {}
        self._in_flight: dict[str, asyncio.Future[bool]] = {}
        # key -> when the operator's "no" stops suppressing the question.
        self._denied_until: dict[str, float] = {}

    @property
    def store(self) -> GrantStore:
        return self._store

    @property
    def _tag(self) -> str:
        """Log prefix identifying which session's grant store this is."""
        return f"approval[{self._label}]" if self._label else "approval"

    def allows(self, key: str) -> bool:
        """True if `key` is already granted. No question is asked."""
        return self._store.allows(key)

    async def check(self, req: ApprovalRequest) -> bool:
        """Grant `req` from the store, or ask the operator once and cache."""
        if self._store.allows(req.key):
            # Reusing a grant used to be silent, which left the busiest outcome
            # in a long run with no record at all: after the first approval every
            # later request produced nothing here.
            logger.debug("%s: %s allowed by standing grant", self._tag, req.key)
            return True
        policy = self.policy_for(req.kind)
        if policy.refuses(req.subject):
            logger.warning(
                "%s: refuse %s without asking (never-ask policy)", self._tag, req.key
            )
            return False
        if self._in_deny_cooldown(req.key):
            return False
        if self._rate_limited(req.kind):
            logger.warning(
                "%s: deny %s (rate limit: >%d %s requests in %.0fs, active grants %s)",
                self._tag,
                req.key,
                policy.rate_limit,
                req.kind,
                policy.window_seconds,
                self._store.active(),
            )
            return False
        return await self._ask_once(req)

    def policy_for(self, kind: str) -> ApprovalPolicy:
        """The policy governing `kind`, falling back to the shared default."""
        return self._policies.get(kind, self._policy)

    async def _ask_once(self, req: ApprovalRequest) -> bool:
        """Ask, collapsing concurrent duplicates onto a single question."""
        existing = self._in_flight.get(req.key)
        if existing is not None:
            logger.info(
                "%s: %s already pending, joining that request", self._tag, req.key
            )
            return await asyncio.shield(existing)

        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._in_flight[req.key] = future
        granted = False
        try:
            granted = await self._invoke_gate(req)
        finally:
            self._in_flight.pop(req.key, None)
            # Resolved in the finally, not after the await, so the joiners always
            # get an answer. When the first asker's turn is aborted mid-question
            # this raises CancelledError, and settling the future only on the
            # success path left every joined caller awaiting it forever — holding
            # a live CONNECT open, since `check` has no timeout of its own.
            if not future.done():
                future.set_result(granted)
        return granted

    async def _invoke_gate(self, req: ApprovalRequest) -> bool:
        """Run the gate under a timeout. Every failure mode denies."""
        self._recent.setdefault(req.kind, deque()).append(self._clock())
        logger.info(
            "%s: asking operator about %s via %s (%s)",
            self._tag,
            req.key,
            type(self._gate).__name__,
            req.detail,
        )
        try:
            granted = await asyncio.wait_for(
                self._gate.request(req), timeout=self._timeout
            )
        except TimeoutError:
            logger.warning(
                "%s: deny %s (no answer in %.0fs)", self._tag, req.key, self._timeout
            )
            return False
        except Exception:
            # A frontend failure must not become an accidental grant.
            logger.exception("%s: deny %s (gate raised)", self._tag, req.key)
            return False
        if not granted:
            self._denied_until[req.key] = (
                self._clock() + self._policy.deny_cooldown_seconds
            )
            logger.info(
                "%s: operator denied %s (not asking again for %.0fs)",
                self._tag,
                req.key,
                self._policy.deny_cooldown_seconds,
            )
            return False
        self._store.grant(req.key, req.ttl_seconds)
        logger.warning(
            "%s: operator GRANTED %s for %.0fs", self._tag, req.key, req.ttl_seconds
        )
        return True

    def _in_deny_cooldown(self, key: str) -> bool:
        """True if the operator recently said no to this exact subject.

        Suppresses the retry storm an agent generates after a denial without
        making the "no" permanent: once the window passes the question can be
        asked again, so the operator is free to change their mind mid-run.
        """
        until = self._denied_until.get(key)
        if until is None:
            return False
        if self._clock() >= until:
            del self._denied_until[key]
            return False
        logger.info(
            "%s: deny %s (operator already declined, %.0fs of cooldown left)",
            self._tag,
            key,
            until - self._clock(),
        )
        return True

    def _rate_limited(self, kind: str) -> bool:
        policy = self.policy_for(kind)
        recent = self._recent.setdefault(kind, deque())
        cutoff = self._clock() - policy.window_seconds
        while recent and recent[0] < cutoff:
            recent.popleft()
        return len(recent) >= policy.rate_limit


@dataclass
class RecordingGate:
    """Test double: answers from a fixed script and records what it was asked."""

    answers: dict[str, bool] = field(default_factory=dict)
    default: bool = False
    seen: list[ApprovalRequest] = field(default_factory=list)

    async def request(self, req: ApprovalRequest) -> bool:
        self.seen.append(req)
        return self.answers.get(req.subject, self.default)


def gate_from_frontend(frontend: object, chat_id: int | None = None) -> ApprovalGate:
    """Adapt a frontend's `ask_approval` into an ApprovalGate.

    `chat_id` binds this gate to one session so the prompt lands in the
    conversation that caused it. Pass None for work with no conversation behind
    it (cron, the job queue); the frontend then falls back to its configured
    operator destination.

    Note the split this preserves: *where* the prompt appears is a routing
    question, but *who may answer* stays an authorization question the frontend
    enforces separately. Posting into a shared channel is therefore safe; every
    frontend re-checks the clicker against its allowed senders.
    """
    asker = getattr(frontend, "ask_approval", None)
    if asker is None:
        logger.warning(
            "frontend %s has no ask_approval; approvals disabled",
            type(frontend).__name__,
        )
        return DenyAllGate()
    return _FrontendGate(asker, chat_id)


@dataclass
class _FrontendGate:
    """Adapts `Frontend.ask_approval` to the ApprovalGate protocol."""

    asker: Callable[..., Awaitable[bool]]
    chat_id: int | None = None

    async def request(self, req: ApprovalRequest) -> bool:
        return await self.asker(req, self.chat_id)
