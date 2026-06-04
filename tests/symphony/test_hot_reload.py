"""Phase 5 hot reload — config.yaml mtime → reload + cancellation paths."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock


from claude_on_the_fly.symphony.config import SymphonyConfig, load_config
from claude_on_the_fly.symphony.cursor import CursorStore
from claude_on_the_fly.symphony.orchestrator import (
    _cancel_workers_for_sources,
    _maybe_reload_config,
    _trim_workers_to_budget,
)
from claude_on_the_fly.symphony.state import OrchestratorState
from claude_on_the_fly.symphony.tracker.issue import Issue


def _issue(**overrides) -> Issue:
    defaults = {
        "id": "10042",
        "identifier": "PROJ-1",
        "title": "t",
        "state": "In Progress",
        "description_raw": None,
        "priority": 3,
        "labels": (),
        "blocked_by": (),
        "parent_key": None,
        "url": "https://x/browse/PROJ-1",
        "created_at": None,
        "updated_at": None,
        "type": "Story",
        "source": "jira",
    }
    return Issue(**(defaults | {k: v for k, v in overrides.items() if k in defaults}))  # type: ignore[arg-type]


def _write_config(tmp_path: Path, body: str) -> Path:
    cfg_path = tmp_path / "symphony.yaml"
    cfg_path.write_text(body)
    return cfg_path


def _prompt_file(tmp_path: Path, name: str = "symphony-prompt.md") -> Path:
    p = tmp_path / name
    p.write_text("hi")
    return p


# ---------------------------------------------------------------------------
# _cancel_workers_for_sources
# ---------------------------------------------------------------------------


def test_cancel_workers_for_sources_cancels_only_listed() -> None:
    state = OrchestratorState()
    j = state.claim(_issue(id="1", identifier="J-1", source="jira"))
    g = state.claim(_issue(id="2", identifier="owner/repo#1", source="github"))
    j.task = MagicMock()
    j.task.done.return_value = False
    g.task = MagicMock()
    g.task.done.return_value = False

    n = _cancel_workers_for_sources(state, {"github"}, reason="dropped")
    assert n == 1
    g.task.cancel.assert_called_once()
    j.task.cancel.assert_not_called()


def test_cancel_workers_skips_already_done() -> None:
    state = OrchestratorState()
    e = state.claim(_issue(id="1", identifier="J-1"))
    e.task = MagicMock()
    e.task.done.return_value = True

    n = _cancel_workers_for_sources(state, {"jira"}, reason="dropped")
    assert n == 0
    e.task.cancel.assert_not_called()


# ---------------------------------------------------------------------------
# _trim_workers_to_budget
# ---------------------------------------------------------------------------


def test_trim_workers_to_budget_cancels_newest_first() -> None:
    state = OrchestratorState()
    # Claim three jira workers in order; later claims have larger started_at.
    a = state.claim(_issue(id="1", identifier="J-1"))
    a.task = MagicMock()
    a.task.done.return_value = False
    # Sleep to ensure distinct started_at values.
    time.sleep(0.001)
    b = state.claim(_issue(id="2", identifier="J-2"))
    b.task = MagicMock()
    b.task.done.return_value = False
    time.sleep(0.001)
    c = state.claim(_issue(id="3", identifier="J-3"))
    c.task = MagicMock()
    c.task.done.return_value = False

    # Budget = 1; expect b and c (newest two) to be cancelled.
    n = _trim_workers_to_budget(state, "jira", new_budget=1)
    assert n == 2
    a.task.cancel.assert_not_called()
    b.task.cancel.assert_called_once()
    c.task.cancel.assert_called_once()


def test_trim_workers_to_budget_no_op_when_under_budget() -> None:
    state = OrchestratorState()
    e = state.claim(_issue(id="1", identifier="J-1"))
    e.task = MagicMock()
    e.task.done.return_value = False
    assert _trim_workers_to_budget(state, "jira", new_budget=5) == 0
    e.task.cancel.assert_not_called()


# ---------------------------------------------------------------------------
# _maybe_reload_config
# ---------------------------------------------------------------------------


def _initial_config(tmp_path: Path) -> tuple[Path, SymphonyConfig]:
    cfg_path = _write_config(
        tmp_path,
        """
tracker:
  base_url: https://x.atlassian.net
  project_key: PROJ
  max_concurrent: 2
""",
    )
    cfg = load_config(cfg_path)
    cfg.validate()
    return cfg_path, cfg


def test_no_change_returns_same_config(tmp_path: Path) -> None:
    cfg_path, cfg = _initial_config(tmp_path)
    state = OrchestratorState()
    trackers: dict = {"jira": MagicMock()}
    stores: dict = {}
    mtime = cfg_path.stat().st_mtime
    new_cfg, new_mtime = _maybe_reload_config(
        config_path=cfg_path,
        config=cfg,
        last_mtime=mtime,
        state=state,
        trackers=trackers,
        prompt_stores={},
        cursor_stores=stores,
        state_root=tmp_path / "state",
    )
    assert new_cfg is cfg
    assert new_mtime == mtime


def test_reload_reduces_max_concurrent_cancels_newest(tmp_path: Path) -> None:
    cfg_path, cfg = _initial_config(tmp_path)

    # Pre-populate two running workers under the jira source.
    state = OrchestratorState()
    a = state.claim(_issue(id="1", identifier="J-1"))
    a.task = MagicMock()
    a.task.done.return_value = False
    time.sleep(0.001)
    b = state.claim(_issue(id="2", identifier="J-2"))
    b.task = MagicMock()
    b.task.done.return_value = False

    # Now rewrite the config with max_concurrent: 1.
    time.sleep(0.01)
    cfg_path.write_text(
        """
tracker:
  base_url: https://x.atlassian.net
  project_key: PROJ
  max_concurrent: 1
"""
    )

    new_cfg, _ = _maybe_reload_config(
        config_path=cfg_path,
        config=cfg,
        last_mtime=0,  # force reload (mtime != 0)
        state=state,
        trackers={"jira": MagicMock()},
        prompt_stores={},
        cursor_stores={"jira": CursorStore(tmp_path / "state", "jira")},
        state_root=tmp_path / "state",
    )

    assert new_cfg.tracker.max_concurrent == 1
    # b is the newest, gets cancelled. a survives.
    b.task.cancel.assert_called_once()
    a.task.cancel.assert_not_called()


def test_reload_removes_tracker_cancels_its_workers(tmp_path: Path) -> None:
    # Start with jira + github.
    cfg_path = _write_config(
        tmp_path,
        """
trackers:
  jira:
    kind: jira
    base_url: https://x.atlassian.net
    project_key: PROJ
  github:
    kind: github
""",
    )
    cfg = load_config(cfg_path)
    cfg.validate()

    state = OrchestratorState()
    j_entry = state.claim(_issue(id="1", identifier="J-1", source="jira"))
    j_entry.task = MagicMock()
    j_entry.task.done.return_value = False
    g_entry = state.claim(_issue(id="2", identifier="owner/repo#1", source="github"))
    g_entry.task = MagicMock()
    g_entry.task.done.return_value = False

    # Rewrite config to drop github.
    time.sleep(0.01)
    cfg_path.write_text(
        """
trackers:
  jira:
    kind: jira
    base_url: https://x.atlassian.net
    project_key: PROJ
"""
    )

    trackers = {"jira": MagicMock(), "github": MagicMock()}
    prompt_stores: dict = {"jira": None, "github": None}
    cursors = {"jira": CursorStore(tmp_path / "state", "jira")}

    new_cfg, _ = _maybe_reload_config(
        config_path=cfg_path,
        config=cfg,
        last_mtime=0,  # force reload
        state=state,
        trackers=trackers,
        prompt_stores=prompt_stores,
        cursor_stores=cursors,
        state_root=tmp_path / "state",
    )

    assert set(new_cfg.trackers) == {"jira"}
    assert "github" not in trackers
    # prompt stores rebuilt for surviving tracker only.
    assert "github" not in prompt_stores
    assert "jira" in prompt_stores
    # github worker cancelled, jira worker still running.
    g_entry.task.cancel.assert_called_once()
    j_entry.task.cancel.assert_not_called()


def test_reload_keeps_last_known_good_on_broken_yaml(tmp_path: Path) -> None:
    cfg_path, cfg = _initial_config(tmp_path)
    state = OrchestratorState()
    # Now corrupt the config.
    time.sleep(0.01)
    cfg_path.write_text("this: is: invalid: too: many: colons:")

    new_cfg, _ = _maybe_reload_config(
        config_path=cfg_path,
        config=cfg,
        last_mtime=0,  # force reload attempt
        state=state,
        trackers={"jira": MagicMock()},
        prompt_stores={},
        cursor_stores={},
        state_root=tmp_path / "state",
    )
    # Reload failed, original config preserved.
    assert new_cfg is cfg
