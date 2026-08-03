"""Daemon-owned codex thread mappings.

Every rejection here exists because the workspace is agent-writable: the mapping
decides which codex thread a session resumes into, so a record that does not
authenticate against the exact workspace and session it claims must not be
honoured. These tests forge each of those records in turn.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from claude_on_the_fly import codex_state


@pytest.fixture(autouse=True)
def mappings_dir(tmp_path, monkeypatch):
    root = tmp_path / "codex-sessions"
    root.mkdir()
    monkeypatch.setattr(codex_state, "MAPPINGS_DIR", root)
    return root


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _write_raw(workspace, session_uuid, record: dict) -> None:
    """Plant a record the writer would never produce."""
    path = codex_state.mapping_path(workspace, session_uuid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


def _valid(workspace, session_uuid="s1", thread_id="thread-1") -> dict:
    return {
        "backend": "codex",
        "workspace": str(workspace.resolve()),
        "session_uuid": session_uuid,
        "thread_id": thread_id,
    }


class TestReadRejectsAnythingItCannotAuthenticate:
    def test_a_round_trip_is_honoured(self, workspace):
        codex_state.write_thread_id(workspace, "s1", "thread-1")
        assert codex_state.read_thread_id(workspace, "s1") == "thread-1"

    def test_a_record_from_another_backend_is_refused(self, workspace):
        _write_raw(workspace, "s1", {**_valid(workspace), "backend": "claude"})
        assert codex_state.read_thread_id(workspace, "s1") is None

    def test_a_record_naming_a_different_session_is_refused(self, workspace):
        """The filename is a hash of the pair, so a mismatch means the file was
        planted rather than written by us."""
        _write_raw(workspace, "s1", {**_valid(workspace), "session_uuid": "other"})
        assert codex_state.read_thread_id(workspace, "s1") is None

    def test_a_record_with_no_workspace_is_refused(self, workspace):
        record = _valid(workspace)
        del record["workspace"]
        _write_raw(workspace, "s1", record)
        assert codex_state.read_thread_id(workspace, "s1") is None

    def test_a_record_naming_a_different_workspace_is_refused(
        self, workspace, tmp_path
    ):
        _write_raw(
            workspace,
            "s1",
            {**_valid(workspace), "workspace": str(tmp_path / "elsewhere")},
        )
        assert codex_state.read_thread_id(workspace, "s1") is None

    @pytest.mark.parametrize(
        "thread_id",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="blank"),
            pytest.param(12345, id="not-a-string"),
            pytest.param(None, id="null"),
        ],
    )
    def test_a_record_without_a_usable_thread_id_is_refused(self, workspace, thread_id):
        _write_raw(workspace, "s1", {**_valid(workspace), "thread_id": thread_id})
        assert codex_state.read_thread_id(workspace, "s1") is None

    @pytest.mark.parametrize(
        "thread_id",
        [
            pytest.param("x" * 513, id="over-length"),
            pytest.param("thread\x00id", id="control-character"),
            pytest.param("thread\nid", id="newline"),
        ],
    )
    def test_a_record_with_an_abusive_thread_id_is_refused(self, workspace, thread_id):
        """It ends up on a codex command line, so length and control characters
        are bounded at the point of trust, not at the point of use."""
        _write_raw(workspace, "s1", {**_valid(workspace), "thread_id": thread_id})
        assert codex_state.read_thread_id(workspace, "s1") is None


class TestWriteRefusesWhatItCannotStandBehind:
    @pytest.mark.parametrize(
        "thread_id",
        [
            pytest.param("", id="empty"),
            pytest.param("  ", id="blank"),
            pytest.param(None, id="null"),
            pytest.param(7, id="not-a-string"),
        ],
    )
    def test_an_empty_thread_id_is_refused(self, workspace, thread_id):
        with pytest.raises(ValueError, match="empty thread id"):
            codex_state.write_thread_id(workspace, "s1", thread_id)

    @pytest.mark.parametrize(
        "thread_id",
        [
            pytest.param("x" * 513, id="over-length"),
            pytest.param("bad\x01id", id="control-character"),
        ],
    )
    def test_an_abusive_thread_id_is_refused(self, workspace, thread_id):
        with pytest.raises(ValueError, match="invalid thread id"):
            codex_state.write_thread_id(workspace, "s1", thread_id)

    def test_a_short_write_leaves_no_partial_mapping(self, workspace, monkeypatch):
        """A truncated record would still parse as JSON often enough to matter,
        so the writer fails the whole write rather than replacing the target."""
        monkeypatch.setattr(codex_state.os, "write", lambda fd, data: 0)
        with pytest.raises(OSError, match="short write"):
            codex_state.write_thread_id(workspace, "s1", "thread-1")
        assert not codex_state.mapping_path(workspace, "s1").exists()

    def test_a_failed_write_removes_its_own_temp_file(self, workspace, monkeypatch):
        def boom(fd, data):
            raise OSError("disk full")

        monkeypatch.setattr(codex_state.os, "write", boom)
        with pytest.raises(OSError, match="disk full"):
            codex_state.write_thread_id(workspace, "s1", "thread-1")
        assert list(codex_state.MAPPINGS_DIR.iterdir()) == []

    def test_the_mapping_is_owner_only(self, workspace):
        path = codex_state.write_thread_id(workspace, "s1", "thread-1")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestReadingARecordThatIsNotAPlainFile:
    def test_a_symlink_is_not_followed(self, workspace, tmp_path):
        """A symlink in the mapping dir is someone redirecting the read."""
        target = tmp_path / "planted.json"
        target.write_text(json.dumps(_valid(workspace)))
        path = codex_state.mapping_path(workspace, "s1")
        path.symlink_to(target)
        assert codex_state.read_thread_id(workspace, "s1") is None

    def test_a_directory_is_not_a_record(self, workspace):
        codex_state.mapping_path(workspace, "s1").mkdir(parents=True)
        assert codex_state.read_thread_id(workspace, "s1") is None

    def test_unparseable_json_is_not_a_record(self, workspace):
        codex_state.mapping_path(workspace, "s1").write_text("{not json")
        assert codex_state.read_thread_id(workspace, "s1") is None

    def test_a_json_scalar_is_not_a_record(self, workspace):
        codex_state.mapping_path(workspace, "s1").write_text('"just a string"')
        assert codex_state.read_thread_id(workspace, "s1") is None

    def test_undecodable_bytes_are_not_a_record(self, workspace):
        codex_state.mapping_path(workspace, "s1").write_bytes(b"\xff\xfe\x00")
        assert codex_state.read_thread_id(workspace, "s1") is None


class TestMappingsForWorkspace:
    def test_newest_first(self, workspace):
        first = codex_state.write_thread_id(workspace, "s1", "t1")
        second = codex_state.write_thread_id(workspace, "s2", "t2")
        os.utime(first, (1, 1))
        os.utime(second, (2, 2))
        found = codex_state.mappings_for_workspace(workspace)
        assert [uuid for _p, uuid, _m in found] == ["s2", "s1"]

    def test_an_unlistable_directory_yields_nothing(self, workspace, monkeypatch):
        def boom(self, _pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(codex_state.Path, "glob", boom)
        assert codex_state.mappings_for_workspace(workspace) == []

    def test_records_that_do_not_authenticate_are_skipped(self, workspace, tmp_path):
        codex_state.write_thread_id(workspace, "keeper", "t1")
        # Same directory, each one failing a different gate.
        _write_raw(
            workspace,
            "wrong-backend",
            {**_valid(workspace, "wrong-backend"), "backend": "x"},
        )
        _write_raw(
            workspace, "no-uuid", {**_valid(workspace, "no-uuid"), "session_uuid": 42}
        )
        _write_raw(
            workspace,
            "wrong-ws",
            {**_valid(workspace, "wrong-ws"), "workspace": str(tmp_path / "nope")},
        )
        _write_raw(
            workspace, "no-thread", {**_valid(workspace, "no-thread"), "thread_id": ""}
        )
        record = _valid(workspace, "no-ws-key")
        del record["workspace"]
        _write_raw(workspace, "no-ws-key", record)

        found = codex_state.mappings_for_workspace(workspace)
        assert [uuid for _p, uuid, _m in found] == ["keeper"]

    def test_a_record_filed_under_the_wrong_name_is_skipped(self, workspace):
        """The filename is the hash of (workspace, session). A valid-looking
        record under any other name was not written by us."""
        path = codex_state.MAPPINGS_DIR / "not-the-hash.json"
        path.write_text(json.dumps(_valid(workspace)))
        assert codex_state.mappings_for_workspace(workspace) == []

    def test_a_record_that_vanishes_mid_scan_is_skipped(self, workspace, monkeypatch):
        """The scan authenticates a record and then stats it for an mtime. A
        file removed between those two steps must drop out of the listing, not
        take the whole scan down."""
        codex_state.write_thread_id(workspace, "s1", "t1")
        real_lstat = codex_state.Path.lstat
        real_read = codex_state.read_thread_id
        armed = {"yes": False}

        def flaky_lstat(self):
            if armed["yes"] and self.suffix == ".json":
                raise OSError("vanished")
            return real_lstat(self)

        def read_then_arm(ws, session_uuid):
            # Arm only once the record has authenticated, so the failure lands
            # on the mtime stat and nowhere earlier.
            result = real_read(ws, session_uuid)
            armed["yes"] = True
            return result

        monkeypatch.setattr(codex_state.Path, "lstat", flaky_lstat)
        monkeypatch.setattr(codex_state, "read_thread_id", read_then_arm)
        assert codex_state.mappings_for_workspace(workspace) == []


class TestRemoveWorkspace:
    def test_it_removes_only_this_workspaces_mappings(self, workspace, tmp_path):
        other = tmp_path / "other-ws"
        other.mkdir()
        codex_state.write_thread_id(workspace, "s1", "t1")
        kept = codex_state.write_thread_id(other, "s2", "t2")
        codex_state.remove_workspace(workspace)
        assert not codex_state.mapping_path(workspace, "s1").exists()
        assert kept.exists()

    def test_an_unlistable_directory_is_not_an_error(self, workspace, monkeypatch):
        def boom(self, _pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(codex_state.Path, "glob", boom)
        codex_state.remove_workspace(workspace)

    def test_records_that_do_not_authenticate_are_left_alone(self, workspace, tmp_path):
        """Removal is as authenticated as reading: this daemon deletes what it
        wrote, not whatever happens to sit in the directory."""
        _write_raw(
            workspace,
            "wrong-backend",
            {**_valid(workspace, "wrong-backend"), "backend": "x"},
        )
        _write_raw(
            workspace, "no-uuid", {**_valid(workspace, "no-uuid"), "session_uuid": 42}
        )
        _write_raw(
            workspace,
            "wrong-ws",
            {**_valid(workspace, "wrong-ws"), "workspace": str(tmp_path / "nope")},
        )
        record = _valid(workspace, "no-ws-key")
        del record["workspace"]
        _write_raw(workspace, "no-ws-key", record)
        before = sorted(p.name for p in codex_state.MAPPINGS_DIR.iterdir())

        codex_state.remove_workspace(workspace)

        assert sorted(p.name for p in codex_state.MAPPINGS_DIR.iterdir()) == before

    def test_a_mapping_that_cannot_be_unlinked_is_not_fatal(
        self, workspace, monkeypatch
    ):
        codex_state.write_thread_id(workspace, "s1", "t1")

        def boom(self, **_kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(codex_state.Path, "unlink", boom)
        codex_state.remove_workspace(workspace)
