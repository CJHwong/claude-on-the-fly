"""Daemon config parsed from a YAML file.

$VAR_NAME resolution per Symphony SPEC §6.1: only fields whose value literally
equals "$VAR_NAME" are expanded against os.environ. No global env override.
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


@dataclass(frozen=True)
class TrackerConfig:
    kind: str
    base_url: str
    email: str
    api_token: str
    project_key: str
    jql_extra: str = ""
    active_states: tuple[str, ...] = ("To Do", "In Progress")
    terminal_states: tuple[str, ...] = ("Done", "Closed", "Cancelled")

    @classmethod
    def from_dict(cls, raw: dict | None) -> TrackerConfig:
        raw = raw or {}
        return cls(
            kind=str(raw.get("kind") or "jira").strip().lower(),
            base_url=str(resolve_env(raw.get("base_url")) or "").rstrip("/"),
            email=str(resolve_env(raw.get("email")) or ""),
            api_token=str(resolve_env(raw.get("api_token")) or ""),
            project_key=str(raw.get("project_key") or "").strip(),
            jql_extra=str(raw.get("jql_extra") or "").strip(),
            active_states=tuple(raw.get("active_states") or ("To Do", "In Progress")),
            terminal_states=tuple(
                raw.get("terminal_states") or ("Done", "Closed", "Cancelled")
            ),
        )

    def validate(self) -> None:
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


@dataclass(frozen=True)
class SymphonyConfig:
    tracker: TrackerConfig
    polling_ms: int = 30000
    max_concurrent: int = 1
    # Per-state caps, in addition to the global max_concurrent. Keys are
    # tracker-state names (lowercased at load time); values are positive ints.
    # Example: {"rework": 1, "pending review": 2}.
    max_concurrent_by_state: dict[str, int] = field(default_factory=dict)
    max_turns: int = (
        20  # -1 = unlimited (rely on stall_timeout_ms or label removal to stop)
    )
    turn_timeout_ms: int = 3600000
    stall_timeout_ms: int = 1800000  # 30m; <= 0 disables stall detection
    max_retry_backoff_ms: int = 300000  # 5m cap on failure backoff
    prompt_path: Path = field(
        default_factory=lambda: Path.home() / ".claude-on-the-fly" / DEFAULT_PROMPT_NAME
    )
    gate_label: str | None = (
        None  # the label whose presence gates dispatch via JQL; agent removes it when done
    )

    @classmethod
    def from_dict(cls, raw: dict, *, base: Path) -> SymphonyConfig:
        if not isinstance(raw, dict):
            raise ValueError("config must decode to a YAML mapping")

        prompt_path = expand_path(raw.get("prompt"), base=base) or (
            base / DEFAULT_PROMPT_NAME
        )

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

        per_state_raw = raw.get("max_concurrent_by_state") or {}
        if not isinstance(per_state_raw, dict):
            raise ValueError("max_concurrent_by_state must be a mapping")
        per_state: dict[str, int] = {}
        for state_name, limit in per_state_raw.items():
            try:
                limit_int = int(limit)
            except (TypeError, ValueError):
                continue
            if limit_int < 1:
                continue
            per_state[str(state_name).strip().lower()] = limit_int

        return cls(
            tracker=TrackerConfig.from_dict(raw.get("tracker")),
            polling_ms=polling,
            max_concurrent=max_concurrent,
            max_concurrent_by_state=per_state,
            max_turns=max_turns,
            turn_timeout_ms=int(raw.get("turn_timeout_ms", 3600000)),
            stall_timeout_ms=int(raw.get("stall_timeout_ms", 1800000)),
            max_retry_backoff_ms=max_retry_backoff_ms,
            prompt_path=prompt_path,
            gate_label=(str(raw["gate_label"]) if raw.get("gate_label") else None),
        )

    def validate(self) -> None:
        self.tracker.validate()
        if not self.prompt_path.exists():
            raise ValueError(f"prompt file not found: {self.prompt_path}")


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
