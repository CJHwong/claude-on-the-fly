"""migrate_thread: one per-thread workspace folds into its shared workspace."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_on_the_fly import codex_state, migration, transcript

UUID = "11111111-2222-5333-8444-555555555555"


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch, claude_projects_dir, codex_sessions_dir):
    monkeypatch.setattr(codex_state, "MAPPINGS_DIR", tmp_path / "codex-sessions")
    monkeypatch.setattr(
        "claude_on_the_fly.agent.DATA_DIR", tmp_path / "data", raising=False
    )


@pytest.fixture
def old_workspace(tmp_path) -> Path:
    path = tmp_path / "workspaces" / "slack" / "dm-hoss-1-2"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def new_workspace(tmp_path) -> Path:
    return tmp_path / "workspaces" / "slack" / "dm" / "U1"


def _claude_session(workspace: Path, uuid: str = UUID) -> Path:
    session_dir = transcript.claude_session_dir(workspace)
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{uuid}.jsonl"
    path.write_text('{"type":"user"}\n')
    return path


def _codex_session(workspace: Path, thread_id: str = "thread-1") -> Path:
    codex_state.write_thread_id(workspace, UUID, thread_id)
    rollout_dir = codex_state.home_dir(workspace) / "sessions" / "2026" / "09" / "14"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout = rollout_dir / f"rollout-2026-09-14T00-00-00-{thread_id}.jsonl"
    rollout.write_text('{"type":"session_meta"}\n')
    return rollout


class TestNothingToDo:
    def test_missing_old_workspace_is_a_noop(self, tmp_path, new_workspace):
        moved = migration.migrate_thread(
            tmp_path / "absent", new_workspace, [UUID], "1-2"
        )
        assert moved is False
        assert not new_workspace.exists()

    def test_same_workspace_is_a_noop(self, old_workspace):
        assert (
            migration.migrate_thread(old_workspace, old_workspace, [UUID], "1-2")
            is False
        )
        assert old_workspace.is_dir()

    def test_second_run_finds_nothing(self, old_workspace, new_workspace):
        _claude_session(old_workspace)
        assert migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert (
            migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
            is False
        )


class TestClaude:
    def test_session_jsonl_moves_under_the_new_hash(self, old_workspace, new_workspace):
        source = _claude_session(old_workspace)
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        target = transcript.claude_session_dir(new_workspace) / f"{UUID}.jsonl"
        assert target.read_text() == '{"type":"user"}\n'
        assert not source.exists()

    def test_subagent_dir_moves_with_the_session(self, old_workspace, new_workspace):
        source = _claude_session(old_workspace)
        sidecar = source.parent / UUID
        sidecar.mkdir()
        (sidecar / "agent-1.jsonl").write_text("{}\n")
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        moved = transcript.claude_session_dir(new_workspace) / UUID / "agent-1.jsonl"
        assert moved.is_file()
        assert not sidecar.exists()

    def test_other_sessions_and_caches_stay_behind(self, old_workspace, new_workspace):
        _claude_session(old_workspace)
        other = _claude_session(old_workspace, "99999999-0000-5000-8000-000000000000")
        cache = other.parent / ".continuation_cache.json"
        cache.write_text("{}")
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert other.is_file()
        assert cache.is_file()

    def test_existing_target_is_never_overwritten(
        self, old_workspace, new_workspace, caplog
    ):
        source = _claude_session(old_workspace)
        target_dir = transcript.claude_session_dir(new_workspace)
        target_dir.mkdir(parents=True)
        (target_dir / f"{UUID}.jsonl").write_text("newer\n")
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert (target_dir / f"{UUID}.jsonl").read_text() == "newer\n"
        assert source.is_file()
        assert "already exists" in caplog.text


class TestCodex:
    def test_mapping_is_rekeyed_and_rollout_moves_between_homes(
        self, old_workspace, new_workspace, scoped_sessions
    ):
        rollout = _codex_session(old_workspace)
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert codex_state.read_thread_id(new_workspace, UUID) == "thread-1"
        assert codex_state.read_thread_id(old_workspace, UUID) is None
        moved = (
            codex_state.home_dir(new_workspace)
            / "sessions"
            / "2026"
            / "09"
            / "14"
            / rollout.name
        )
        assert moved.is_file()
        assert not rollout.exists()

    def test_shared_home_rewrites_only_the_mapping(self, old_workspace, new_workspace):
        rollout = _codex_session(old_workspace)
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert codex_state.read_thread_id(new_workspace, UUID) == "thread-1"
        assert rollout.is_file()

    def test_no_mapping_means_nothing_to_move(self, old_workspace, new_workspace):
        _claude_session(old_workspace)
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert codex_state.read_thread_id(new_workspace, UUID) is None

    def test_missing_rollout_keeps_the_mapping_move(
        self, old_workspace, new_workspace, scoped_sessions, caplog
    ):
        codex_state.write_thread_id(old_workspace, UUID, "thread-gone")
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert codex_state.read_thread_id(new_workspace, UUID) == "thread-gone"
        assert "no rollout" in caplog.text


class TestFiles:
    def test_leftovers_move_under_threads_and_old_dir_goes(
        self, old_workspace, new_workspace
    ):
        (old_workspace / "report.pdf").write_bytes(b"pdf")
        (old_workspace / "outbox" / ".sent").mkdir(parents=True)
        (old_workspace / "outbox" / ".sent" / "a.txt").write_text("a")
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        thread_dir = new_workspace / "threads" / "1-2"
        assert (thread_dir / "report.pdf").read_bytes() == b"pdf"
        assert (thread_dir / "outbox" / ".sent" / "a.txt").is_file()
        assert not old_workspace.exists()

    def test_persona_links_are_dropped_not_moved(self, old_workspace, new_workspace):
        os.symlink("/nonexistent/CLAUDE.md", old_workspace / "CLAUDE.md")
        os.symlink("/nonexistent/CLAUDE.md", old_workspace / "AGENTS.md")
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert not (new_workspace / "threads" / "1-2" / "CLAUDE.md").is_symlink()
        assert not old_workspace.exists()

    def test_empty_old_dir_leaves_no_thread_dir(self, old_workspace, new_workspace):
        migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert not (new_workspace / "threads").exists()
        assert not old_workspace.exists()

    def test_move_failure_leaves_the_old_dir_in_place(
        self, old_workspace, new_workspace, monkeypatch, caplog
    ):
        (old_workspace / "report.pdf").write_bytes(b"pdf")

        def boom(*_args, **_kwargs):
            raise OSError("disk says no")

        monkeypatch.setattr(migration.shutil, "move", boom)
        moved = migration.migrate_thread(old_workspace, new_workspace, [UUID], "1-2")
        assert moved is False
        assert (old_workspace / "report.pdf").is_file()
        assert "disk says no" in caplog.text


# ---------------------------------------------------------------------------
# The bulk pass
# ---------------------------------------------------------------------------


def _resolver(kind: str, label: str) -> str | None:
    table = {
        ("dm", "hoss"): "dm/U_HOSS",
        ("conversation", "general"): "channel/C_GEN",
        ("conversation", "mpdm-a-b-1"): "mpim/G_AB",
    }
    return table.get((kind, label))


class TestPlanSlack:
    def test_every_old_shape_is_placed(self, tmp_path):
        root = tmp_path / "workspaces" / "slack"
        for name in (
            "dm-hoss-1786342813-662689",
            "dm-hoss-1786342813",
            "dm-hoss-root",
            "general-1786342813-662689",
            "mpdm-a-b-1-1786342813-662689",
        ):
            (root / name).mkdir(parents=True)
        plans = {p.old.name: p for p in migration.plan_slack(tmp_path, _resolver)}
        assert plans["dm-hoss-1786342813-662689"].new_name == "slack/dm/U_HOSS"
        assert plans["dm-hoss-1786342813-662689"].thread_key == "1786342813-662689"
        assert plans["dm-hoss-1786342813"].thread_key == "1786342813"
        assert plans["dm-hoss-root"].thread_key == "root"
        assert plans["general-1786342813-662689"].new_name == "slack/channel/C_GEN"
        assert plans["mpdm-a-b-1-1786342813-662689"].new_name == "slack/mpim/G_AB"

    def test_unknown_names_are_skipped_with_a_reason(self, tmp_path):
        root = tmp_path / "workspaces" / "slack"
        (root / "dm-stranger-1786342813-662689").mkdir(parents=True)
        (root / "gone-channel-1786342813").mkdir()
        (root / "smoke").mkdir()
        (root / "dm").mkdir()  # already the new layout
        plans = {p.old.name: p for p in migration.plan_slack(tmp_path, _resolver)}
        assert plans["dm-stranger-1786342813-662689"].new_name is None
        assert (
            "dm 'stranger' not on Slack"
            in plans["dm-stranger-1786342813-662689"].reason
        )
        assert "conversation 'gone-channel'" in plans["gone-channel-1786342813"].reason
        assert plans["smoke"].reason == "not a thread directory"
        assert plans["dm"].reason == "not a thread directory"

    def test_no_slack_directory_is_an_empty_plan(self, tmp_path):
        assert migration.plan_slack(tmp_path, _resolver) == []

    def test_sessions_come_from_both_backends(self, tmp_path):
        old = tmp_path / "workspaces" / "slack" / "dm-hoss-1786342813-662689"
        old.mkdir(parents=True)
        _claude_session(old)
        (transcript.claude_session_dir(old) / "not-a-uuid.jsonl").write_text("")
        codex_state.write_thread_id(old, "99999999-0000-5000-8000-000000000000", "t9")
        [plan] = migration.plan_slack(tmp_path, _resolver)
        assert plan.session_uuids == [UUID, "99999999-0000-5000-8000-000000000000"]


class TestPlanTelegram:
    def test_token_directories_fold_into_the_chat(self, tmp_path):
        root = tmp_path / "workspaces" / "telegram"
        (root / "42-20260606-120000").mkdir(parents=True)
        (root / "42-3").mkdir()  # an earlier build's counter token
        (root / "42-f74d").mkdir()  # and its short hex token
        (root / "42").mkdir()
        (root / "smoke").mkdir()
        plans = {p.old.name: p for p in migration.plan_telegram(tmp_path)}
        assert plans["42-20260606-120000"].new_name == "telegram/42"
        assert plans["42-20260606-120000"].thread_key == "20260606-120000"
        assert plans["42-3"].thread_key == "3"
        assert plans["42-f74d"].thread_key == "f74d"
        assert plans["42"].new_name is None
        assert plans["smoke"].new_name is None


class TestApplyAndRender:
    def test_apply_moves_every_session_and_counts_directories(self, tmp_path):
        old = tmp_path / "workspaces" / "slack" / "dm-hoss-1786342813-662689"
        old.mkdir(parents=True)
        _claude_session(old)
        _claude_session(old, "99999999-0000-5000-8000-000000000000")
        skipped = tmp_path / "workspaces" / "slack" / "smoke"
        skipped.mkdir()
        plans = migration.plan_slack(tmp_path, _resolver)
        assert migration.apply_plans(plans, tmp_path) == 1
        new = tmp_path / "workspaces" / "slack" / "dm" / "U_HOSS"
        moved = sorted(
            p.name for p in transcript.claude_session_dir(new).glob("*.jsonl")
        )
        assert moved == [f"{UUID}.jsonl", "99999999-0000-5000-8000-000000000000.jsonl"]
        assert not old.exists()
        assert skipped.is_dir()

    def test_render_lists_each_directory_and_totals(self, tmp_path):
        plans = [
            migration.ThreadPlan(
                tmp_path / "dm-hoss-1-2", "1-2", "slack/dm/U1", "", [UUID]
            ),
            migration.ThreadPlan(
                tmp_path / "smoke", "", None, "not a thread directory"
            ),
        ]
        text = migration.render_plans(plans)
        assert "dm-hoss-1-2 -> slack/dm/U1/threads/1-2  sessions=1" in text
        assert "smoke  SKIP: not a thread directory" in text
        assert text.endswith("1 directories to move (1 sessions), 1 skipped")


class TestSlackDirectory:
    class _Client:
        def __init__(self):
            self.calls: list[dict] = []

        def users_list(self, **kwargs):
            self.calls.append({"users_list": kwargs})
            if kwargs.get("cursor") is None:
                return {
                    "members": [{"name": "hoss", "id": "U_HOSS"}],
                    "response_metadata": {"next_cursor": "page2"},
                }
            return {"members": [{"name": "avery", "id": "U_AVERY"}]}

        def conversations_list(self, **kwargs):
            self.calls.append({"conversations_list": kwargs})
            return {
                "channels": [
                    {"name": "general", "id": "C_GEN"},
                    {"name": "mpdm-a-b-1", "id": "G_AB", "is_mpim": True},
                ],
                "response_metadata": {"next_cursor": ""},
            }

    def test_resolves_handles_and_channels_across_pages(self):
        client = self._Client()
        directory = migration.SlackDirectory(client)
        assert directory("dm", "hoss") == "dm/U_HOSS"
        assert directory("dm", "avery") == "dm/U_AVERY"
        assert directory("dm", "stranger") is None
        assert directory("conversation", "general") == "channel/C_GEN"
        assert directory("conversation", "mpdm-a-b-1") == "mpim/G_AB"
        assert directory("conversation", "gone") is None
        assert client.calls[1]["users_list"]["cursor"] == "page2"
        assert client.calls[2]["conversations_list"]["exclude_archived"] is False
