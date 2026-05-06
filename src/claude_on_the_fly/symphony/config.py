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

DEFAULT_WORKTREE_ROOT = Path.home() / "code" / "symphony-wt"
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
    max_turns: int = 20
    turn_timeout_ms: int = 3600000
    worktree_root: Path = field(default_factory=lambda: DEFAULT_WORKTREE_ROOT)
    prompt_path: Path = field(
        default_factory=lambda: Path.home() / ".claude-on-the-fly" / DEFAULT_PROMPT_NAME
    )
    exit_label: str | None = None

    @classmethod
    def from_dict(cls, raw: dict, *, base: Path) -> SymphonyConfig:
        if not isinstance(raw, dict):
            raise ValueError("config must decode to a YAML mapping")

        worktree_root = (
            expand_path(raw.get("worktree_root"), base=base) or DEFAULT_WORKTREE_ROOT
        )
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
        if max_turns < 1:
            raise ValueError(f"max_turns must be >= 1 (got {max_turns})")

        return cls(
            tracker=TrackerConfig.from_dict(raw.get("tracker")),
            polling_ms=polling,
            max_concurrent=max_concurrent,
            max_turns=max_turns,
            turn_timeout_ms=int(raw.get("turn_timeout_ms", 3600000)),
            worktree_root=worktree_root,
            prompt_path=prompt_path,
            exit_label=(str(raw["exit_label"]) if raw.get("exit_label") else None),
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
