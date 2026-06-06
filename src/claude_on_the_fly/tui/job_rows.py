"""Turn the EventLog's raw rows into renderable job summaries.

Shared by the history overlay (full audit trail) and the dashboard's chat
tab (live request feed scoped to the chat frontends). Pure functions only —
no Textual, no filesystem — so both screens render the same way and stay
testable without booting the app.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _event_source(e: dict) -> str:
    """Return the frontend source for an event row.

    Pre-unification symphony rows wrote the tracker name (jira / github) into
    `source` directly. Treat any such legacy row as a symphony event so the
    new filter vocabulary still surfaces it.
    """
    src = str(e.get("source") or "")
    if src in ("jira", "github"):
        return "symphony"
    return src


def _format_runtime(seconds: float | None) -> str:
    """Compact wall-clock duration: 12s, 1m23s, 1h05m. None → '—'."""
    if seconds is None or seconds < 1:
        return "—" if seconds is None else "0s"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _format_local_time(value: object) -> str:
    """UTC ISO-8601 timestamp → local HH:MM:SS in the system timezone. Falls
    back to the raw HH:MM:SS slice when the value can't be parsed."""
    epoch = _parse_ts(value)
    if epoch is None:
        text = str(value or "")
        return text[11:19] if len(text) >= 19 else text
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def _parse_ts(value: object) -> float | None:
    """ISO-8601 UTC timestamp → epoch seconds. None on parse failure."""
    if not isinstance(value, str) or not value:
        return None
    try:
        # EventLog writes ISO-8601 with a trailing 'Z'; fromisoformat needs
        # '+00:00' on Python < 3.11. 3.12 accepts both, but normalise so the
        # parse never regresses on an older interpreter.
        normalised = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalised).timestamp()
    except (ValueError, TypeError):
        return None


def _format_detail(e: dict) -> str:
    """Type-specific one-line summary of an event row."""
    t = e.get("type")
    source = _event_source(e)
    if t == "cancelled":
        reason = e.get("reason") or ""
        st = e.get("state") or ""
        return f"{reason} ({st})" if st else reason
    if t == "worker_done":
        # Symphony rows carry reason (terminal/inactive) + state; chat rows
        # carry cost + tokens. Render whichever is present.
        reason = e.get("reason") or ""
        st = e.get("state") or ""
        if reason:
            return f"{reason} ({st})" if st else reason
        cost = e.get("cost")
        if cost is not None:
            return f"cost=${cost:.4f}"
        return ""
    if t == "worker_failed":
        return str(e.get("error", ""))[:100]
    if t == "retry_scheduled":
        kind = e.get("kind") or "?"
        attempt = e.get("attempt")
        return f"{kind} attempt={attempt}" if attempt else kind
    if t == "dispatched":
        if source == "symphony":
            st = e.get("state") or ""
            attempt = e.get("failure_attempt")
            if attempt:
                return f"{st} (retry {attempt})"
            return st
        # Chat dispatches don't have a tracker state; identifier already
        # tells the user which conversation, so leave detail blank.
        return ""
    return ""


def _compute_runtimes(events_oldest_first: list[dict]) -> dict[int, float | None]:
    """For each event index (matching `events_oldest_first` order), return
    the seconds elapsed since the most recent `dispatched` for the same
    (identifier, source). Dispatched rows themselves carry 0.

    Runtime resets on every fresh `dispatched`, so a retry shows time since
    the retry started, not since the original first attempt. Events that
    arrive before any matching dispatch (legacy rows, log truncated) get
    None — rendered as the em-dash placeholder."""
    last_dispatch: dict[tuple[str, str], float] = {}
    out: dict[int, float | None] = {}
    for idx, e in enumerate(events_oldest_first):
        ident = str(e.get("identifier", "?"))
        src = _event_source(e)
        key = (ident, src)
        ts = _parse_ts(e.get("ts"))
        if e.get("type") == "dispatched":
            if ts is not None:
                last_dispatch[key] = ts
            out[idx] = 0.0
            continue
        anchor = last_dispatch.get(key)
        if anchor is None or ts is None:
            out[idx] = None
        else:
            out[idx] = max(0.0, ts - anchor)
    return out


@dataclass
class _JobAggregate:
    """Per-(identifier, source) accumulator used while scanning newest-first."""

    identifier: str
    source: str
    last_event: dict
    latest_event_ts: float | None
    runs: int = 0
    latest_dispatch_ts: float | None = None


def _aggregate_by_job(events_newest_first: list[dict]) -> list[dict]:
    """Collapse the event log into one record per (identifier, source).

    Each output dict is a synthesised "job summary" with the latest event's
    payload plus aggregate stats:

    - `runs`       : number of `dispatched` events seen for this job
    - `runtime`    : seconds elapsed between the latest dispatch and the
                     latest event (None when no dispatch exists in window)
    - `last_event` : the full latest event dict — drives time, type, detail
    - `backend`    : backend recorded on the latest event
    - `session_uuid`: latest event's session_uuid (or None for symphony rows,
                     since symphony events don't include it on every type)

    Newest-first input keeps the first occurrence of each key as the latest,
    so we don't have to compare timestamps explicitly.
    """
    seen_order: list[tuple[str, str]] = []
    rows: dict[tuple[str, str], _JobAggregate] = {}
    for e in events_newest_first:
        ident = str(e.get("identifier", "?"))
        src = _event_source(e)
        key = (ident, src)
        bucket = rows.get(key)
        if bucket is None:
            bucket = _JobAggregate(
                identifier=ident,
                source=src,
                last_event=e,
                latest_event_ts=_parse_ts(e.get("ts")),
            )
            rows[key] = bucket
            seen_order.append(key)
        if e.get("type") == "dispatched":
            bucket.runs += 1
            # Newest-first scan: the first dispatch we hit IS the latest.
            if bucket.latest_dispatch_ts is None:
                bucket.latest_dispatch_ts = _parse_ts(e.get("ts"))
    out: list[dict] = []
    for key in seen_order:
        bucket = rows[key]
        e = bucket.last_event
        dispatch_ts = bucket.latest_dispatch_ts
        latest_ts = bucket.latest_event_ts
        runtime: float | None
        if dispatch_ts is None or latest_ts is None:
            runtime = None
        else:
            runtime = max(0.0, latest_ts - dispatch_ts)
        out.append(
            {
                "identifier": bucket.identifier,
                "source": bucket.source,
                "runs": bucket.runs,
                "runtime": runtime,
                "last_event": e,
                "backend": e.get("backend"),
                "session_uuid": e.get("session_uuid"),
                "tracker": e.get("tracker") or e.get("source"),
            }
        )
    return out
