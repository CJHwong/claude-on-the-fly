"""Normalized Issue model. Tracker-agnostic shape; Jira-specific construction in from_jira()."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

_EMPTY_EXTRA: Mapping[str, Any] = MappingProxyType({})


def make_key(source: str, raw_id: str) -> str:
    """Compose `<source>:<raw_id>` for state / retry tracking. Defined at
    module level so callers can use it without an Issue instance handy."""
    return f"{source}:{raw_id}"


@dataclass(frozen=True)
class BlockerRef:
    key: str | None
    state: str | None


@dataclass(frozen=True)
class IssueSummary:
    """Reconciliation-time snapshot of a ticket: state + adapter-specific fields.

    Used by the batched `fetch_summaries_by_keys` call. Each adapter populates
    `extra` with whatever its `is_terminal`/`is_active` predicates need:
    Jira fills `{"labels": (...)}`; GitHub fills
    `{"review_requested_by_me": bool}`. Orchestrator never reads `extra` —
    it hands the summary back to the tracker's predicate.
    """

    state: str
    extra: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_EXTRA)


@dataclass(frozen=True)
class Issue:
    id: str  # tracker-internal numeric id (Jira: "10042"; GitHub: PR node id)
    identifier: str  # human key (Jira: "PROJ-1133"; GitHub: "owner/repo#123")
    title: str
    state: str
    description_raw: dict | None  # raw ADF JSON; agent parses (Jira). None for GitHub.
    priority: int | None  # 1=highest, lower=preferred per SPEC §8.2
    labels: tuple[str, ...]  # lowercased per SPEC §4.2
    blocked_by: tuple[BlockerRef, ...]
    parent_key: str | None
    url: str
    created_at: str | None
    updated_at: str | None
    type: str = (
        ""  # tracker-native type name (Jira: "Story", "Bug", "DevEx", "Sub-task")
    )
    source: str = "jira"  # tracker kind that produced this issue
    body_text: str | None = (
        None  # plaintext/markdown body for prompts that don't want raw ADF
    )
    # Adapter-specific structured fields the predicates may need (e.g.
    # GitHub stashes `review_requested_by_me` here so `issue_to_summary`
    # can project it back into an `IssueSummary` without an extra fetch).
    extra: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_EXTRA)

    @property
    def key(self) -> str:
        """Composite (source, id) key used by OrchestratorState and RetryQueue.

        Two trackers can independently mint the same raw `id`, so the
        in-memory state always keys by source-prefixed composite.
        """
        return make_key(self.source, self.id)

    @classmethod
    def from_jira(cls, payload: dict, base_url: str) -> Issue:
        f = payload.get("fields", {}) or {}
        status = f.get("status") or {}
        priority = f.get("priority") or {}
        parent = f.get("parent") or {}
        issuetype = f.get("issuetype") or {}
        priority_id = priority.get("id")
        try:
            prio = int(priority_id) if priority_id is not None else None
        except (TypeError, ValueError):
            prio = None

        blockers: list[BlockerRef] = []
        for link in f.get("issuelinks") or []:
            link_type = (link.get("type") or {}).get("inward")
            if link_type != "is blocked by":
                continue
            # An inward "is blocked by" link must point at an inwardIssue. If only
            # outwardIssue is set, this issue blocks the other one, not vice versa.
            inward = link.get("inwardIssue")
            if not isinstance(inward, dict) or not inward.get("key"):
                continue
            blockers.append(
                BlockerRef(
                    key=inward.get("key"),
                    state=((inward.get("fields") or {}).get("status") or {}).get(
                        "name"
                    ),
                )
            )

        labels = tuple(str(label).lower() for label in (f.get("labels") or []) if label)

        key = payload.get("key") or ""
        return cls(
            id=str(payload.get("id") or ""),
            identifier=key,
            title=str(f.get("summary") or ""),
            state=str(status.get("name") or ""),
            description_raw=f.get("description")
            if isinstance(f.get("description"), dict)
            else None,
            priority=prio,
            labels=labels,
            blocked_by=tuple(blockers),
            parent_key=parent.get("key"),
            url=f"{base_url.rstrip('/')}/browse/{key}" if key else "",
            created_at=f.get("created"),
            updated_at=f.get("updated"),
            type=str(issuetype.get("name") or ""),
            source="jira",
        )
