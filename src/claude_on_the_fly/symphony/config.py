"""Daemon config parsed from a YAML file.

$VAR_NAME resolution per Symphony SPEC §6.1: only fields whose value literally
equals "$VAR_NAME" are expanded against os.environ. No global env override.

Config schema (multi-tracker):

    trackers:
      jira:
        kind: jira
        base_url: ...
        active_states: [...]
        terminal_states: [...]
        gate_label: stevedore
        prompt: ./symphony-prompt-jira.md
        max_concurrent_by_state: {...}
      github:
        kind: github
        active_states: ["open"]
        terminal_states: ["closed", "merged"]
        prompt: ./symphony-prompt-github.md

    polling_ms: 30000
    max_concurrent: 1
    ...

Backward compat: the legacy singular `tracker:` form is auto-wrapped into a
single-entry `trackers:` map. Top-level `prompt`, `gate_label`, and
`max_concurrent_by_state` are hoisted into the wrapped tracker.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_VAR_RE = re.compile(r"^\$([A-Z][A-Z0-9_]*)$")

DEFAULT_PROMPT_NAME = "symphony-prompt.md"
DEFAULT_PROMPT_BY_KIND: dict[str, str] = {
    "jira": "symphony-prompt.md",
    "github": "symphony-prompt-github.md",
}


def resolve_env(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    m = _VAR_RE.match(value.strip())
    if not m:
        return value
    return os.environ.get(m.group(1), "")


def expand_path(value: Any, *, base: Path | None = None) -> Path | None:
    if value is None or value == "":
        return None
    resolved = resolve_env(value)
    if not isinstance(resolved, str) or not resolved:
        return None
    p = Path(resolved).expanduser()
    if not p.is_absolute() and base is not None:
        p = (base / p).resolve()
    else:
        p = p.resolve()
    return p


def _parse_per_state_concurrency(raw: Any) -> dict[str, int]:
    """Normalize the `max_concurrent_by_state:` mapping: lowercase keys, drop
    invalid values. Caller decides what "invalid" means at the surrounding
    config level (we don't raise — that's `from_dict`'s job)."""
    if not isinstance(raw, dict):
        raise ValueError("max_concurrent_by_state must be a mapping")
    out: dict[str, int] = {}
    for state_name, limit in raw.items():
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            continue
        if limit_int < 1:
            continue
        out[str(state_name).strip().lower()] = limit_int
    return out


def _default_prompt_path(kind: str, *, base: Path | None = None) -> Path:
    """Default prompt path when none is set in YAML.

    Prefer the YAML file's directory (base) when known; fall back to the
    standard data dir under home. Naming follows the per-kind convention,
    falling back to the legacy unsuffixed name for jira (backward compat)."""
    name = DEFAULT_PROMPT_BY_KIND.get(kind, DEFAULT_PROMPT_NAME)
    if base is not None:
        return (base / name).resolve()
    return Path.home() / ".claude-on-the-fly" / name


@dataclass(frozen=True, kw_only=True)
class TrackerCommonConfig:
    """Shared fields every tracker config carries. Subclassed per adapter."""

    kind: str
    active_states: tuple[str, ...] = ("To Do", "In Progress")
    terminal_states: tuple[str, ...] = ("Done", "Closed", "Cancelled")
    gate_label: str | None = None
    prompt_path: Path = field(
        default_factory=lambda: Path.home() / ".claude-on-the-fly" / DEFAULT_PROMPT_NAME
    )
    max_concurrent_by_state: dict[str, int] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.prompt_path.exists():
            raise ValueError(f"prompt file not found: {self.prompt_path}")


@dataclass(frozen=True, kw_only=True)
class JiraTrackerConfig(TrackerCommonConfig):
    base_url: str
    email: str
    api_token: str
    project_key: str
    jql_extra: str = ""

    @classmethod
    def from_dict(
        cls, raw: dict | None, *, base: Path | None = None
    ) -> JiraTrackerConfig:
        raw = raw or {}
        kind = str(raw.get("kind") or "jira").strip().lower()
        prompt_path = expand_path(raw.get("prompt"), base=base) or _default_prompt_path(
            kind, base=base
        )
        per_state = _parse_per_state_concurrency(
            raw.get("max_concurrent_by_state") or {}
        )
        return cls(
            kind=kind,
            base_url=str(resolve_env(raw.get("base_url")) or "").rstrip("/"),
            email=str(resolve_env(raw.get("email")) or ""),
            api_token=str(resolve_env(raw.get("api_token")) or ""),
            project_key=str(raw.get("project_key") or "").strip(),
            jql_extra=str(raw.get("jql_extra") or "").strip(),
            active_states=tuple(raw.get("active_states") or ("To Do", "In Progress")),
            terminal_states=tuple(
                raw.get("terminal_states") or ("Done", "Closed", "Cancelled")
            ),
            gate_label=(str(raw["gate_label"]) if raw.get("gate_label") else None),
            prompt_path=prompt_path,
            max_concurrent_by_state=per_state,
        )

    def validate(self) -> None:
        # Field-required errors first so they surface clearly even when the
        # prompt file (default location) doesn't exist on this machine.
        if not self.base_url:
            raise ValueError("tracker.base_url is required")
        if not self.email:
            raise ValueError(
                "tracker.email is required (set JIRA_EMAIL or use $JIRA_EMAIL)"
            )
        if not self.api_token:
            raise ValueError(
                "tracker.api_token is required (set JIRA_API_TOKEN or use $JIRA_API_TOKEN)"
            )
        if not self.project_key:
            raise ValueError("tracker.project_key is required")
        super().validate()


@dataclass(frozen=True, kw_only=True)
class GitHubTrackerConfig(TrackerCommonConfig):
    """GitHub adapter config. No fields beyond the common set: `gh` CLI
    handles auth, and assignment-as-gate means no gate label.

    Overrides `active_states` / `terminal_states` defaults to GitHub PR
    states so direct instantiation (without going through `from_dict`)
    still produces sensible behavior in tests.
    """

    active_states: tuple[str, ...] = ("open",)
    terminal_states: tuple[str, ...] = ("closed", "merged")
    search_query: str = "is:pr is:open -is:draft user-review-requested:@me"

    @classmethod
    def from_dict(
        cls, raw: dict | None, *, base: Path | None = None
    ) -> GitHubTrackerConfig:
        raw = raw or {}
        kind = str(raw.get("kind") or "github").strip().lower()
        prompt_path = expand_path(raw.get("prompt"), base=base) or _default_prompt_path(
            kind, base=base
        )
        per_state = _parse_per_state_concurrency(
            raw.get("max_concurrent_by_state") or {}
        )
        search_query = str(raw.get("search_query") or "").strip()
        return cls(
            kind=kind,
            active_states=tuple(raw.get("active_states") or ("open",)),
            terminal_states=tuple(raw.get("terminal_states") or ("closed", "merged")),
            gate_label=(str(raw["gate_label"]) if raw.get("gate_label") else None),
            prompt_path=prompt_path,
            max_concurrent_by_state=per_state,
            search_query=search_query
            if search_query
            else "is:pr is:open -is:draft user-review-requested:@me",
        )


# Backward-compat alias: existing code imports `TrackerConfig` and calls
# `.from_dict()` expecting Jira-shaped fields. New code should reach for
# `TrackerCommonConfig` (the abstract base) or the specific subclass.
TrackerConfig = JiraTrackerConfig


def make_tracker_config(
    raw: dict | None, *, base: Path | None = None
) -> TrackerCommonConfig:
    """Dispatch on `kind` to the right tracker config subclass."""
    raw = raw or {}
    kind = str(raw.get("kind") or "jira").strip().lower()
    if kind == "jira":
        return JiraTrackerConfig.from_dict(raw, base=base)
    if kind == "github":
        return GitHubTrackerConfig.from_dict(raw, base=base)
    raise ValueError(f"tracker.kind={kind!r} unsupported")


@dataclass(frozen=True)
class SymphonyConfig:
    trackers: dict[str, TrackerCommonConfig]
    polling_ms: int = 30000
    max_concurrent: int = 1
    max_turns: int = (
        20  # -1 = unlimited (rely on stall_timeout_ms or label removal to stop)
    )
    turn_timeout_ms: int = 3600000
    stall_timeout_ms: int = 1800000  # 30m; <= 0 disables stall detection
    max_retry_backoff_ms: int = 300000  # 5m cap on failure backoff

    @property
    def tracker(self) -> TrackerCommonConfig:
        """Convenience accessor for the first-inserted tracker — handy in
        single-source setups and tests. Multi-source callers iterate
        `self.trackers` directly."""
        return next(iter(self.trackers.values()))

    @classmethod
    def from_dict(cls, raw: dict, *, base: Path) -> SymphonyConfig:
        if not isinstance(raw, dict):
            raise ValueError("config must decode to a YAML mapping")

        trackers_raw = raw.get("trackers")
        if not trackers_raw:
            # Backward compat: wrap legacy singular `tracker:` shape into a
            # single-entry trackers map. Hoist top-level prompt/gate_label/
            # max_concurrent_by_state into the wrapped tracker.
            singular = dict(raw.get("tracker") or {})
            if "prompt" not in singular and raw.get("prompt") is not None:
                singular["prompt"] = raw["prompt"]
            if "gate_label" not in singular and raw.get("gate_label") is not None:
                singular["gate_label"] = raw["gate_label"]
            if (
                "max_concurrent_by_state" not in singular
                and raw.get("max_concurrent_by_state") is not None
            ):
                singular["max_concurrent_by_state"] = raw["max_concurrent_by_state"]
            kind = str(singular.get("kind") or "jira").strip().lower()
            trackers_raw = {kind: singular}

        if not isinstance(trackers_raw, dict):
            raise ValueError("`trackers` must be a mapping of source → config")

        trackers: dict[str, TrackerCommonConfig] = {}
        for name, tcfg in trackers_raw.items():
            trackers[str(name)] = make_tracker_config(tcfg, base=base)

        polling = int(raw.get("polling_ms", 30000))
        if polling < 1000:
            raise ValueError(f"polling_ms must be >= 1000 (got {polling})")
        max_concurrent = int(raw.get("max_concurrent", 1))
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1 (got {max_concurrent})")
        max_turns = int(raw.get("max_turns", 20))
        if max_turns == 0 or max_turns < -1:
            raise ValueError(
                f"max_turns must be >= 1 or -1 for unlimited (got {max_turns})"
            )
        max_retry_backoff_ms = int(raw.get("max_retry_backoff_ms", 300000))
        if max_retry_backoff_ms < 1000:
            raise ValueError(
                f"max_retry_backoff_ms must be >= 1000 (got {max_retry_backoff_ms})"
            )

        return cls(
            trackers=trackers,
            polling_ms=polling,
            max_concurrent=max_concurrent,
            max_turns=max_turns,
            turn_timeout_ms=int(raw.get("turn_timeout_ms", 3600000)),
            stall_timeout_ms=int(raw.get("stall_timeout_ms", 1800000)),
            max_retry_backoff_ms=max_retry_backoff_ms,
        )

    def validate(self) -> None:
        for tracker_cfg in self.trackers.values():
            tracker_cfg.validate()


def load_config(path: Path) -> SymphonyConfig:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: YAML parse error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: config must decode to a mapping, got {type(raw).__name__}"
        )
    return SymphonyConfig.from_dict(raw, base=path.parent)
