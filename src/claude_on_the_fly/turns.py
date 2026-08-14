"""Durable record of chat turns that have not been answered yet.

A chat turn lives in an in-memory queue, so a stop of any kind used to destroy
it: the person who asked got silence, and nothing replayed it. This is the write
side of the fix. One file per frontend, rewritten atomically, holding exactly
what it takes to pick a turn back up.

Written when the turn is *accepted*, not at shutdown. That is the whole point:
a shutdown-time write covers a clean SIGTERM and nothing else, while a record
that already exists survives SIGKILL, a `--force` past the supervisor's grace,
an OOM kill, and a panic.

Two phases. Both resume; the difference is what the resumed turn is told:

- `QUEUED` — accepted, never handed to an agent. Nothing has run, so it is
  replayed as it stands.
- `DISPATCHED` — an agent was started, and may already have written files, posted
  messages, or pushed commits. It is still replayed, because a person who asked
  for something wants it done rather than handed back, but the replayed prompt
  says it is a resume so the agent can check what is already there before
  repeating anything (`orchestrator.RESUME_TEMPLATE`).

A turn that has been replayed to its limit is the one exception, and it is where
the phases stop mattering: running it again is the likeliest reason the daemon
keeps going down, so it is offered back instead.

There is deliberately no third "started but has not acted yet" phase. Both
backends only build their tool-event relay when interim progress is enabled
(`agent._exec`, `backends/codex.run`), so such a phase would need new hooks in
both, and it would not change what happens now that both phases resume.

The agent never reads or writes this. It lives under `state/`, which the sandbox
profiles deny to the jail in both directions -- a turn record the agent could
write would be a prompt it could schedule for the next start.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

QUEUED = "queued"
DISPATCHED = "dispatched"

# How old a pending turn may be and still be worth acting on. A restart takes
# seconds and an upgrade a minute; a machine that was off overnight should not
# resurrect yesterday's half-finished question at breakfast.
DEFAULT_TTL_S = 30 * 60

# How many times one turn may be replayed before it is parked. A turn that kills
# the daemon would otherwise be replayed at every start, for ever. Parking is
# `key_state`'s answer to the same problem.
MAX_REPLAYS = 2


@dataclass(frozen=True)
class PendingTurn:
    """One accepted turn that has not been answered.

    `route` is opaque frontend routing context, the same contract as a `Job`'s
    origin: nothing here reads it. It exists because a chat id is not always
    enough to reach the conversation again -- Slack's is a hash of
    (channel, thread_ts), so the reply has nowhere to go unless the pair is
    carried with the turn.

    `session` is the frontend's session discriminator at the time the turn was
    accepted. Without it a replay resumes a *different* conversation and a
    different workspace than the person was in.
    """

    chat_id: int
    text: str
    route: dict[str, Any] = field(default_factory=dict)
    session: str | None = None
    compact: bool = False
    phase: str = QUEUED
    turn_id: str = ""
    recorded_at: float = 0.0
    replays: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "chat_id": self.chat_id,
            "text": self.text,
            "route": self.route,
            "session": self.session,
            "compact": self.compact,
            "phase": self.phase,
            "recorded_at": self.recorded_at,
            "replays": self.replays,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingTurn | None:
        """Rebuild one entry, or None if it cannot be trusted.

        A half-written or hand-edited entry is dropped rather than degraded: the
        text is what gets replayed as somebody's message, so a partial record is
        not something to guess at.

        This is also the only place a disk record becomes an object, which is
        why the two gates in `take()` are made sound here rather than there. A
        negative `replays` and a non-finite `recorded_at` both walk straight past
        a comparison, so the values are clamped where they are read.

        `text` is deliberately not length-capped. The daemon's own write path is
        the message a person sent, and a cap here would silently drop a long
        paste; nothing about it bounds memory either, since `_read` has already
        read the whole file. Anyone who can plant a 5 MB entry can plant a
        thousand small ones.
        """
        try:
            chat_id = data["chat_id"]
            text = data["text"]
            turn_id = data["turn_id"]
        except (KeyError, TypeError):
            return None
        # `bool` subclasses `int`, so `chat_id: true` passed the int check and
        # became chat_id=True, which compares equal to 1: a stranger's reply
        # aimed at whichever conversation that frontend numbers 1.
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            return None
        if not isinstance(text, str):
            return None
        if not isinstance(turn_id, str) or not turn_id:
            return None
        recorded_at = _as_float(data.get("recorded_at"))
        if recorded_at is None:
            return None
        route = data.get("route")
        session = data.get("session")
        phase = data.get("phase")
        replays = data.get("replays")
        return cls(
            chat_id=chat_id,
            text=text,
            route=route if isinstance(route, dict) else {},
            session=session if isinstance(session, str) else None,
            compact=bool(data.get("compact")),
            # An unrecognised phase reads as dispatched, so a record we cannot
            # classify is resumed with the "you may have already done some of
            # this" note rather than as a clean first attempt.
            phase=phase if phase in (QUEUED, DISPATCHED) else DISPATCHED,
            turn_id=turn_id,
            recorded_at=recorded_at,
            # Clamped, not trusted. `take()` parks a turn at `replays >=
            # MAX_REPLAYS`, so a counter of -1 needs about 10^18 restarts to get
            # there: the turn that keeps killing the daemon is replayed at every
            # start instead of being handed back. bool is excluded for the reason
            # chat_id is.
            replays=max(0, replays)
            if isinstance(replays, int) and not isinstance(replays, bool)
            else 0,
        )


def _as_float(value: Any) -> float | None:
    """A recorded timestamp; 0.0 if there is none, None if it is not a number.

    The TTL gate is `moment - entry.recorded_at > ttl_s`. Every comparison
    against NaN is False, so a NaN outlives its TTL for ever, and an infinity
    does the same in one direction. Neither is a timestamp, so the entry is
    dropped rather than repaired -- guessing at the age of a message somebody is
    about to be answered for is the thing this class refuses to do.

    A missing or non-numeric value stays 0.0, which reads as "not recorded" and
    is exempt from the TTL. That is the existing degrade-to-defaults contract for
    every other scalar here, and the daemon's own writer always sets the field.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else None


def new_turn_id() -> str:
    """A unique id for one accepted turn. Time-sortable, like a job id."""
    return f"{time.time_ns()}-{uuid4().hex[:8]}"


class TurnJournal:
    """The pending-turn file for one frontend.

    Every mutation rewrites the whole file through a temp file and an atomic
    replace, the idiom `key_state` and the supervisor's pid writes already use.
    The file holds at most the turns one chat frontend has outstanding, so
    rewriting it whole costs less than the bookkeeping to avoid doing so.

    No method raises on an I/O or parse failure. A journal that cannot be
    written must not take down the turn it was describing, and one that cannot be
    read is treated as empty: losing the safety net is bad, refusing to serve is
    worse.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    # -- writing ---------------------------------------------------------

    def record(self, turn: PendingTurn) -> None:
        """Add or replace one entry. Called before the turn can run."""
        entries = {t.turn_id: t for t in self._read()}
        entries[turn.turn_id] = turn
        self._write(list(entries.values()))

    def mark_dispatched(self, turn_id: str) -> None:
        """Note that an agent was started for this turn, so a resume of it has to
        assume work may already have happened. Silent if the entry is gone."""
        entries = self._read()
        found = False
        updated = []
        for entry in entries:
            if entry.turn_id == turn_id and entry.phase != DISPATCHED:
                updated.append(replace(entry, phase=DISPATCHED))
                found = True
            else:
                updated.append(entry)
        if found:
            self._write(updated)

    def forget(self, turn_id: str) -> None:
        """Drop one entry, once its reply has been posted."""
        entries = self._read()
        remaining = [entry for entry in entries if entry.turn_id != turn_id]
        if len(remaining) != len(entries):
            self._write(remaining)

    # -- reading ---------------------------------------------------------

    def take(
        self, *, ttl_s: float = DEFAULT_TTL_S, now: float | None = None
    ) -> tuple[list[PendingTurn], list[PendingTurn]]:
        """Empty the journal and return (to replay, to nudge about).

        Emptying *before* anything runs is what stops a turn that kills the
        daemon from being replayed at every start; the replay counter it carries
        forward is what parks it if it manages to be recorded again. Same
        reasoning as cron renaming its trigger file before it reads it.

        Both phases replay. The phase is not what decides that any more -- it
        travels so the caller can tell a resumed turn that it may be repeating
        itself (`orchestrator.RESUME_TEMPLATE`) -- and the only thing held back is
        a turn that has already been replayed to its limit, where running it again
        is the likeliest reason the daemon keeps going down. Expired entries are
        returned in neither list: nothing is owed to a question from another day.
        """
        entries = self._read()
        if not entries:
            return [], []
        self._write([])
        moment = time.time() if now is None else now
        replay: list[PendingTurn] = []
        nudge: list[PendingTurn] = []
        for entry in entries:
            if entry.recorded_at and moment - entry.recorded_at > ttl_s:
                logger.info(
                    "turns: dropping %s for chat_id=%s, older than %.0fs",
                    entry.turn_id,
                    entry.chat_id,
                    ttl_s,
                )
                continue
            if entry.replays >= MAX_REPLAYS:
                logger.warning(
                    "turns: parking %s for chat_id=%s after %d replays",
                    entry.turn_id,
                    entry.chat_id,
                    entry.replays,
                )
                nudge.append(entry)
            else:
                replay.append(replace(entry, replays=entry.replays + 1))
        replay.sort(key=lambda t: t.turn_id)
        nudge.sort(key=lambda t: t.turn_id)
        return replay, nudge

    def _read(self) -> list[PendingTurn]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "turns: cannot read %s (%s), treating as empty", self._path, exc
            )
            return []
        if not isinstance(raw, list):
            logger.warning("turns: %s is not a list, treating as empty", self._path)
            return []
        rebuilt = [
            PendingTurn.from_dict(item) for item in raw if isinstance(item, dict)
        ]
        return [turn for turn in rebuilt if turn is not None]

    def _write(self, entries: list[PendingTurn]) -> None:
        try:
            payload = json.dumps([entry.to_dict() for entry in entries], indent=2)
        except (TypeError, ValueError):
            # A frontend's `route_for` returned something that will not serialize.
            # That is the frontend's bug, and the turn it describes must still be
            # served: this journal is a safety net, not a gate on the message path.
            logger.exception("turns: cannot serialize the journal for %s", self._path)
            return
        # A fixed temp name, kept on purpose. One daemon owns one frontend's
        # journal, so there is no second writer to collide with, and a fixed name
        # is reclaimed by the next write after a SIGKILL -- a pid-suffixed one
        # would leave a file behind for every kill, in a directory nothing sweeps.
        tmp = self._path.with_suffix(".json.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # 0600, not `write_text`'s 0o644 (measured). This file holds the
            # verbatim text of every unanswered turn, so it gets the same
            # treatment as the other daemon-owned record of a conversation
            # (`codex_state.write_thread_id`). The mode argument covers the
            # create; the chmod covers a permissive umask and a temp file left
            # by an older build.
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            with os.fdopen(os.open(tmp, flags, 0o600), "wb") as handle:
                handle.write(payload.encode("utf-8"))
            os.chmod(tmp, 0o600)
            tmp.replace(self._path)
        except OSError:
            # The turn matters more than the record of it. Logged rather than
            # swallowed: losing the journal means a stop stops being recoverable,
            # which the operator should be able to find out about.
            logger.exception("turns: could not write %s", self._path)
