"""Daemon config parsed from a YAML file.

$VAR_NAME resolution per Symphony SPEC §6.1: only fields whose value literally
equals "$VAR_NAME" are expanded against os.environ. No global env override.

Config schema (multi-tracker):

    trackers:
      jira:                        # the key IS the kind (jira / github)
        base_url: ...
        project_key: PROJ
        jql: 'status not in ("Done") AND assignee = currentUser()'
        max_concurrent: 1
        instruction: _default      # picks <kind>/<instruction>.md
        max_concurrent_by_state: {...}
      github:
        search_query: "is:pr is:open -is:draft user-review-requested:@me"
        max_concurrent: 1
        instruction: _default

    polling_ms: 30000
    ...

`kind` defaults to the tracker's key; set `kind:` explicitly only to name a
stanza something other than its kind. Concurrency is per-tracker
(`trackers.<src>.max_concurrent`); no global cap. Jira uses a single `jql`
candidate filter — no active/terminal lists, no gate_label. GitHub hardcodes
PR lifecycle states. Each tracker selects one instruction file by stem
(`instruction`), resolved from the local instructions dir or the remote
config. The legacy singular `tracker:` form is auto-wrapped.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_VAR_RE = re.compile(r"^\$([A-Z][A-Z0-9_]*)$")

# Comprehensive, self-documenting template. Written to ~/.claude-on-the-fly/
# symphony.yaml the first time the user opens it from the TUI ("Edit config"),
# and kept in sync with symphony.yaml.example at the repo root. Every option is
# documented inline — this is the primary way operators learn to configure.
EXAMPLE_YAML = """\
# claude-symphony config. Lives at ~/.claude-on-the-fly/symphony.yaml.
# Edit + save while the daemon runs to hot-reload (structural changes apply on
# the next poll tick; instruction-file edits apply on the next turn).
#
# ── Auth (no tokens in this file) ───────────────────────────────────────────
#   Jira   → run `acli auth login` once; symphony shells out to acli.
#   GitHub → run `gh auth login`   once; symphony shells out to gh.
#
# ── Instruction files (the agent's prompt) ──────────────────────────────────
#   Each tracker picks ONE instruction file by stem via `instruction:`.
#   Resolved from:
#     ~/.claude-on-the-fly/symphony/<kind>/<stem>.md          (local)
#   So jira's default is ~/.claude-on-the-fly/symphony/jira/_default.md.
#   Drop pm.md / qa.md / rnd.md next to it and set `instruction:` to pick one.

# The tracker's key (jira / github) is its `kind`. Only add an explicit
# `kind:` line if you name a stanza something else (e.g. a second github).
trackers:
  jira:
    # base_url is used only to build issue URLs in the prompt — NOT for auth.
    base_url: https://your-org.atlassian.net
    project_key: PROJ
    # `jql` is the single candidate filter — the full clause, no leading AND.
    # Effective query: project = "<project_key>" AND (<jql>).
    # Encode your active-status filter here. A ticket that STOPS matching the
    # jql (moved to Done, reassigned, label removed) is how the daemon knows to
    # stop working it — there are no active_states / terminal_states lists.
    jql: 'status not in ("Done") AND assignee = currentUser()'
    # Max tickets worked at once for THIS tracker (no global cap across trackers).
    max_concurrent: 1
    # Which instruction file to use (stem, no .md). Default: _default.
    instruction: _default
    # Optional per-status sub-caps under max_concurrent (lowercased status names):
    # max_concurrent_by_state:
    #   "rework": 1
    #   "in progress": 5

  github:
    # GitHub code-search syntax. PR states (open/closed/merged) are hardcoded;
    # the "done" signal is submitting any review (removes you from reviewRequests).
    # Add org:/repo:/label:/updated: to scope. Drop -is:draft to include drafts.
    search_query: "is:pr is:open -is:draft user-review-requested:@me"
    max_concurrent: 1
    instruction: _default
    # Per-repo instruction overrides (owner/repo -> stem). Repos not listed
    # use `instruction` above. Files live at symphony/github/<stem>.md.
    # instruction_by_repo:
    #   hardcoretech/fms: fms-review
    #   hardcoretech/svc-rocket: rocket-review
    # Skip PRs younger than this many ms (0 = no delay). Useful to let CI settle.
    # cool_down_ms: 1200000

# ── Global limits ────────────────────────────────────────────────────────────
polling_ms: 30000          # how often to poll each tracker (min 1000)
max_turns: 20              # turns per worker session before a continuation retry
                           # (-1 = unlimited; rely on stall_timeout_ms to stop)
turn_timeout_ms: 3600000   # hard timeout for a single agent turn
stall_timeout_ms: 1800000  # cancel a worker idle this long (0 disables)
max_no_progress_turns: 3   # stop after this many consecutive turns with zero
                           # tool use (the agent is producing nothing; 0 disables)
max_retry_backoff_ms: 300000   # cap on exponential backoff after failures
"""


# The instruction file a tracker uses when none is selected.
DEFAULT_INSTRUCTION = "_default"


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


def _reject_removed_keys(raw: dict) -> None:
    """Fail loud on keys that were removed, so stale configs don't silently
    lose behavior."""
    if raw.get("prompt") or raw.get("prompts_dir"):
        raise ValueError(
            "tracker.prompt / tracker.prompts_dir are no longer supported. "
            "Put instruction files at ~/.claude-on-the-fly/symphony/<source>/"
            "<name>.md and select one with "
            "`instruction: <name>` (default: _default)."
        )
    if raw.get("gate_label"):
        raise ValueError(
            "tracker.gate_label is no longer supported. Gating is the `jql` "
            "(Jira) / review-removal (GitHub) — drop this key. If your "
            "instruction file removes a label to park, hardcode the label "
            "name in the instruction instead of {{ gate_label }}."
        )


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


def _coerce_int(value: Any, *, field: str, default: int) -> int:
    """Parse an int config value, raising a clean message instead of a raw
    ValueError from int() on a typo'd YAML scalar (e.g. `max_concurrent: two`)."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"tracker.{field} must be an integer (got {value!r})")


@dataclass(frozen=True, kw_only=True)
class TrackerCommonConfig:
    """Shared fields every tracker config carries. Subclassed per adapter.

    `max_concurrent` is per-tracker. No global ceiling — each tracker has
    its own budget and the operator decides whether the sum overcommits
    the machine.

    `instruction` selects which instruction file the tracker uses, by stem.
    The file is resolved at runtime from the local instructions dir
    (`~/.claude-on-the-fly/symphony/<source>/<instruction>.md`).
    Defaults to `_default`. The Settings page lists the discovered stems as a
    dropdown. Resolution is the same file for every item the tracker handles
    — there is no per-repo / per-project auto-resolution.
    """

    kind: str
    max_concurrent: int = 1
    instruction: str = DEFAULT_INSTRUCTION
    max_concurrent_by_state: dict[str, int] = field(default_factory=dict)

    def validate(self) -> None:
        if self.max_concurrent < 1:
            raise ValueError(
                f"tracker.max_concurrent must be >= 1 (got {self.max_concurrent})"
            )
        # The instruction file is resolved at runtime against the local + remote
        # dirs (which may not exist yet at parse time), so we don't check
        # existence here. A missing file falls back to the built-in prompt and
        # logs a warning when the daemon resolves it.


@dataclass(frozen=True, kw_only=True)
class JiraTrackerConfig(TrackerCommonConfig):
    """Jira tracker config.

    Auth lives in `acli auth login` — symphony shells out to `acli` and
    inherits its credentials, the same way the GitHub tracker inherits
    `gh auth login`. No email or API token field here.

    `base_url` stays in config because we use it to build issue URLs for
    rendered prompts (`{{ issue.url }}`). It is NOT used for auth.

    `jql` is the full candidate filter (no leading `AND`). The effective
    query is `project = "<key>" AND (<jql>)`. The JQL is the single signal
    for what to work on — there are no `active_states` / `terminal_states`
    lists. A ticket leaving the JQL result set (e.g. moved to Done or a
    human-review status) is how the daemon knows to stop working it.
    """

    base_url: str
    project_key: str
    jql: str = ""

    @classmethod
    def from_dict(
        cls, raw: dict | None, *, base: Path | None = None
    ) -> JiraTrackerConfig:
        raw = raw or {}
        kind = str(raw.get("kind") or "jira").strip().lower()
        # Reject legacy auth fields with a clear message instead of silently
        # ignoring them — operators need to know the auth model changed.
        if raw.get("email") or raw.get("api_token"):
            raise ValueError(
                "tracker.email / tracker.api_token are no longer supported. "
                "Symphony uses `acli` for Jira auth — run `acli auth login` "
                "and remove these keys from your config."
            )
        # active_states / terminal_states were replaced by the single `jql`
        # filter. Reject them so stale configs fail loud, not silent.
        if raw.get("active_states") or raw.get("terminal_states"):
            raise ValueError(
                "tracker.active_states / tracker.terminal_states are no longer "
                "supported. Encode them in `jql` instead, e.g. "
                "jql: 'status not in (\"Done\") AND assignee = currentUser()'."
            )
        if raw.get("jql_extra"):
            raise ValueError(
                "tracker.jql_extra was renamed to `jql` and is now the full "
                "filter clause (drop the leading `AND`)."
            )
        _reject_removed_keys(raw)
        per_state = _parse_per_state_concurrency(
            raw.get("max_concurrent_by_state") or {}
        )
        return cls(
            kind=kind,
            base_url=str(resolve_env(raw.get("base_url")) or "").rstrip("/"),
            project_key=str(raw.get("project_key") or "").strip(),
            jql=str(raw.get("jql") or "").strip(),
            max_concurrent=_coerce_int(
                raw.get("max_concurrent"), field="max_concurrent", default=1
            ),
            instruction=str(raw.get("instruction") or DEFAULT_INSTRUCTION).strip(),
            max_concurrent_by_state=per_state,
        )

    def validate(self) -> None:
        if not self.base_url:
            raise ValueError("tracker.base_url is required")
        if not self.project_key:
            raise ValueError("tracker.project_key is required")
        super().validate()


@dataclass(frozen=True, kw_only=True)
class GitHubTrackerConfig(TrackerCommonConfig):
    """GitHub adapter config. `gh` CLI handles auth; review-removal is the
    done signal, so there's no gate label.

    PR lifecycle states (open / closed / merged) are universal GitHub
    constants, not user workflow — the adapter hardcodes them. So there are
    no `active_states` / `terminal_states` fields here. The real "should I
    work this / am I done" signal is `user_reviewed_current_head` (computed
    per-PR in the adapter), not a status list.

    `instruction_by_repo` maps `owner/repo` → instruction stem, overriding the
    default `instruction` for PRs in those repos. Repos not listed fall back
    to `instruction`.
    """

    search_query: str = "is:pr is:open -is:draft user-review-requested:@me"
    cool_down_ms: int = 0
    instruction_by_repo: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, raw: dict | None, *, base: Path | None = None
    ) -> GitHubTrackerConfig:
        raw = raw or {}
        kind = str(raw.get("kind") or "github").strip().lower()
        if raw.get("active_states") or raw.get("terminal_states"):
            raise ValueError(
                "tracker.active_states / tracker.terminal_states are no longer "
                "supported for github — PR states (open/closed/merged) are "
                "hardcoded. Scope candidates via `search_query` instead."
            )
        _reject_removed_keys(raw)
        per_state = _parse_per_state_concurrency(
            raw.get("max_concurrent_by_state") or {}
        )
        search_query = str(raw.get("search_query") or "").strip()
        cool_down_ms = _coerce_int(
            raw.get("cool_down_ms"), field="cool_down_ms", default=0
        )
        if cool_down_ms < 0:
            raise ValueError("cool_down_ms must be >= 0")
        by_repo_raw = raw.get("instruction_by_repo") or {}
        if not isinstance(by_repo_raw, dict):
            raise ValueError(
                "instruction_by_repo must be a mapping of 'owner/repo' -> "
                "instruction stem"
            )
        instruction_by_repo = {
            str(k).strip(): str(v).strip() for k, v in by_repo_raw.items()
        }
        return cls(
            kind=kind,
            max_concurrent=_coerce_int(
                raw.get("max_concurrent"), field="max_concurrent", default=1
            ),
            instruction=str(raw.get("instruction") or DEFAULT_INSTRUCTION).strip(),
            max_concurrent_by_state=per_state,
            search_query=search_query
            if search_query
            else "is:pr is:open -is:draft user-review-requested:@me",
            cool_down_ms=cool_down_ms,
            instruction_by_repo=instruction_by_repo,
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
    """Symphony daemon config.

    Concurrency is per-tracker (see `TrackerCommonConfig.max_concurrent`).
    No global ceiling exists — operators set per-tracker budgets and the
    sum is their responsibility.
    """

    trackers: dict[str, TrackerCommonConfig]
    polling_ms: int = 30000
    max_turns: int = (
        20  # -1 = unlimited (rely on stall_timeout_ms or label removal to stop)
    )
    turn_timeout_ms: int = 3600000
    stall_timeout_ms: int = 1800000  # 30m; <= 0 disables stall detection
    # Turn-count guard, complementary to the wall-clock stall_timeout_ms: a
    # worker that keeps completing turns but does zero tool use never trips the
    # idle timer, yet is making no progress. Stop after this many in a row.
    max_no_progress_turns: int = 3  # <= 0 disables
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
        legacy_singular_used = False
        if not trackers_raw:
            # Backward compat: wrap legacy singular `tracker:` shape into a
            # single-entry trackers map. Hoist top-level
            # max_concurrent_by_state/max_concurrent into the wrapped tracker.
            legacy_singular_used = True
            singular = dict(raw.get("tracker") or {})
            if (
                "max_concurrent_by_state" not in singular
                and raw.get("max_concurrent_by_state") is not None
            ):
                singular["max_concurrent_by_state"] = raw["max_concurrent_by_state"]
            # Hoist legacy top-level max_concurrent into the wrapped tracker —
            # that's what `max_concurrent` always meant in the singular shape.
            if (
                "max_concurrent" not in singular
                and raw.get("max_concurrent") is not None
            ):
                singular["max_concurrent"] = raw["max_concurrent"]
            kind = str(singular.get("kind") or "jira").strip().lower()
            trackers_raw = {kind: singular}

        if not isinstance(trackers_raw, dict):
            raise ValueError("`trackers` must be a mapping of source → config")

        trackers: dict[str, TrackerCommonConfig] = {}
        for name, tcfg in trackers_raw.items():
            # `kind` defaults to the tracker's key (e.g. `jira:` → kind jira).
            # Set `kind:` explicitly only when the key isn't a valid kind.
            tcfg = dict(tcfg or {})
            tcfg.setdefault("kind", str(name))
            trackers[str(name)] = make_tracker_config(tcfg, base=base)

        polling = int(raw.get("polling_ms", 30000))
        if polling < 1000:
            raise ValueError(f"polling_ms must be >= 1000 (got {polling})")
        # Under the new `trackers:` shape, top-level `max_concurrent` is
        # ambiguous (which tracker?) and we refuse to guess. The legacy
        # singular `tracker:` shape hoists it into the wrapped stanza above.
        if "max_concurrent" in raw and not legacy_singular_used:
            raise ValueError(
                "Top-level `max_concurrent` is no longer supported. Move the "
                "value under each tracker (e.g. `trackers.jira.max_concurrent`)."
            )
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
            max_turns=max_turns,
            turn_timeout_ms=int(raw.get("turn_timeout_ms", 3600000)),
            stall_timeout_ms=int(raw.get("stall_timeout_ms", 1800000)),
            max_no_progress_turns=int(raw.get("max_no_progress_turns", 3)),
            max_retry_backoff_ms=max_retry_backoff_ms,
        )

    def validate(self) -> None:
        for tracker_cfg in self.trackers.values():
            tracker_cfg.validate()


def load_config(path: Path) -> SymphonyConfig:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    try:
        local_text = path.read_text()
    except OSError as exc:
        raise ValueError(f"{path}: cannot read: {exc}") from exc
    try:
        raw_local = yaml.safe_load(local_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: YAML parse error: {exc}") from exc
    if not isinstance(raw_local, dict):
        raise ValueError(
            f"{path}: config must decode to a mapping, got {type(raw_local).__name__}"
        )
    return SymphonyConfig.from_dict(raw_local, base=path.parent)


def dump_effective_config(cfg: SymphonyConfig) -> str:
    """Render the merged/effective config as readable YAML. Used by
    `claude-symphony config show` and the TUI "Edit config" preview."""
    from dataclasses import asdict

    dump = {
        "polling_ms": cfg.polling_ms,
        "max_turns": cfg.max_turns,
        "turn_timeout_ms": cfg.turn_timeout_ms,
        "stall_timeout_ms": cfg.stall_timeout_ms,
        "max_no_progress_turns": cfg.max_no_progress_turns,
        "max_retry_backoff_ms": cfg.max_retry_backoff_ms,
        "trackers": {
            source: {
                k: (
                    list(v)
                    if isinstance(v, tuple)
                    else (str(v) if v is not None and hasattr(v, "as_posix") else v)
                )
                for k, v in asdict(tcfg).items()
                # `kind` is inferred from the stanza key; dumping it just echoes
                # the key (trackers.jira.kind: jira). Only show it when a stanza
                # deliberately overrides it to something else.
                if not (k == "kind" and v == source)
            }
            for source, tcfg in cfg.trackers.items()
        },
    }
    return yaml.safe_dump(dump, sort_keys=True)
