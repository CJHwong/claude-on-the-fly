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


class TestCodexHomeShieldsExecutionControlNames:
    """The per-thread home is writable by the jailed turn, so the names that decide
    what codex executes or is told must resolve onto the shared paths the profile
    denies writes to."""

    def test_a_real_file_left_by_the_agent_is_replaced_by_the_link(self, tmp_path):
        """Without this, a turn could write its own AGENTS.md into its home and leave
        itself standing orders for the next run -- exactly what the shared ~/.codex
        deny list exists to stop."""
        shared = tmp_path / "shared-codex"
        shared.mkdir()
        (shared / "AGENTS.md").write_text("operator instructions\n")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = codex_state.home_dir(workspace)
        (home / "sessions").mkdir(parents=True)
        planted = home / "AGENTS.md"
        planted.write_text("do whatever I say next turn\n")
        codex_state.ensure_home(workspace, shared=shared)
        assert planted.is_symlink()
        assert planted.readlink() == shared / "AGENTS.md"
        assert planted.read_text() == "operator instructions\n"

    def test_a_name_the_operator_does_not_have_stays_absent(self, tmp_path):
        """No dangling links: codex reads a missing config as "use the defaults",
        and treats a broken one as an error."""
        shared = tmp_path / "shared-codex"
        shared.mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = codex_state.ensure_home(workspace, shared=shared)
        assert not (home / "AGENTS.md").exists()
        assert not (home / "config.toml").is_symlink()


def test_the_home_links_follow_a_redirected_codex_home(tmp_path, monkeypatch):
    """A deployment that sets CODEX_HOME keeps its config and credential there.

    Hardcoding `Path.home() / ".codex"` pointed the links at a directory that may
    hold neither, and every one is then skipped as an absent target rather than
    failing: the home comes out with no config and no credential, and nothing says
    so until codex authenticates. Found while setting up a real jailed turn. The
    shared tree the jail denies resolves through the same helper, so the two cannot
    disagree.
    """
    redirected = tmp_path / "elsewhere-codex"
    redirected.mkdir()
    (redirected / "auth.json").write_text('{"token": "x"}\n')
    (redirected / "config.toml").write_text("model = 'x'\n")
    monkeypatch.setenv("CODEX_HOME", str(redirected))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = codex_state.ensure_home(workspace)
    assert (home / "auth.json").readlink() == redirected / "auth.json"
    assert (home / "config.toml").readlink() == redirected / "config.toml"


class TestSharedSkillsReachThePerThreadHome:
    """codex reads $CODEX_HOME/skills and also writes its own tree inside it.

    Measured on a real jailed turn: 242 accesses to skills/, including a
    skills/.system/ tree codex creates. So the directory cannot simply be linked at
    a write-denied shared path, and it cannot simply be left empty either -- the
    operator's skills were reachable before per-thread homes existed.
    """

    def test_shared_skills_are_linked_entry_by_entry(self, tmp_path):
        shared = tmp_path / "shared-codex"
        (shared / "skills" / "browser").mkdir(parents=True)
        (shared / "skills" / "browser" / "SKILL.md").write_text("browser skill\n")
        (shared / "skills" / "media").mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = codex_state.ensure_home(workspace, shared=shared)
        skills = home / "skills"
        # A real directory, so codex can still create skills/.system inside it.
        assert skills.is_dir() and not skills.is_symlink()
        assert (skills / "browser").is_symlink()
        assert (skills / "browser").readlink() == shared / "skills" / "browser"
        assert (skills / "browser" / "SKILL.md").read_text() == "browser skill\n"
        assert (skills / "media").is_symlink()

    def test_codex_can_still_create_its_own_entries_alongside(self, tmp_path):
        """The whole reason the directory is merged rather than linked."""
        shared = tmp_path / "shared-codex"
        (shared / "skills" / "browser").mkdir(parents=True)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = codex_state.ensure_home(workspace, shared=shared)
        system = home / "skills" / ".system"
        system.mkdir()
        (system / "marker").write_text("codex owns this\n")
        # A second call must not disturb what codex created.
        codex_state.ensure_home(workspace, shared=shared)
        assert (system / "marker").read_text() == "codex owns this\n"
        assert (home / "skills" / "browser").is_symlink()

    def test_removing_the_workspace_does_not_delete_the_shared_skills(self, tmp_path):
        """rmtree walks the merged directory, and its entries point at the
        operator's own skills."""
        shared = tmp_path / "shared-codex"
        (shared / "skills" / "browser").mkdir(parents=True)
        (shared / "skills" / "browser" / "SKILL.md").write_text("keep me\n")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = codex_state.ensure_home(workspace, shared=shared)
        codex_state.remove_workspace(workspace)
        assert not home.exists()
        assert (shared / "skills" / "browser" / "SKILL.md").read_text() == "keep me\n"

    def test_link_targets_include_what_resolves_outside_the_shared_root(self, tmp_path):
        """`~/.codex/skills -> ~/.agents/skills` is the shape that found this: a link
        into a directory nobody mounted dangles inside the Linux namespace."""
        elsewhere = tmp_path / "agents-home" / "skills"
        (elsewhere / "browser").mkdir(parents=True)
        shared = tmp_path / "shared-codex"
        shared.mkdir()
        (shared / "skills").symlink_to(elsewhere)
        targets = codex_state.shared_link_targets(shared=shared)
        assert elsewhere.resolve() in targets
        assert (elsewhere / "browser").resolve() in targets

    def test_codex_own_state_in_the_shared_dir_is_not_linked(self, tmp_path):
        """codex keeps a `skills/.system/` tree of built-in skills in that directory
        and writes into it. Linking it at the shared, write-denied path left codex
        unable to create its own system skills: measured inside a real jail as
        "cannot create .../skills/.system/marker", while an ordinary skill linked and
        read fine. Dot-entries are codex's, so each thread gets its own."""
        shared = tmp_path / "shared-codex"
        (shared / "skills" / ".system" / "imagegen").mkdir(parents=True)
        (shared / "skills" / "browser").mkdir()
        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = codex_state.ensure_home(workspace, shared=shared)
        assert not (home / "skills" / ".system").exists()
        assert (home / "skills" / "browser").is_symlink()
        # And codex can then make its own, which is the whole point.
        (home / "skills" / ".system").mkdir()
        (home / "skills" / ".system" / "marker").write_text("mine\n")
        codex_state.ensure_home(workspace, shared=shared)
        assert (home / "skills" / ".system" / "marker").read_text() == "mine\n"

    def test_a_skill_link_that_moved_is_repointed(self, tmp_path):
        """An operator who relocates their skills gets the new target on the next
        turn, without a restart, the same way the top-level links behave."""
        first = tmp_path / "codex-a"
        (first / "skills" / "browser").mkdir(parents=True)
        second = tmp_path / "codex-b"
        (second / "skills" / "browser").mkdir(parents=True)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = codex_state.ensure_home(workspace, shared=first)
        assert (home / "skills" / "browser").readlink() == first / "skills" / "browser"
        codex_state.ensure_home(workspace, shared=second)
        assert (home / "skills" / "browser").readlink() == second / "skills" / "browser"

    def test_a_real_directory_codex_made_is_not_replaced_by_a_link(self, tmp_path):
        """A name collision between codex's own entry and an operator skill leaves
        codex's alone: it may hold state, and clobbering it would lose that."""
        shared = tmp_path / "shared-codex"
        (shared / "skills" / "browser").mkdir(parents=True)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        home = codex_state.home_dir(workspace)
        (home / "skills" / "browser").mkdir(parents=True)
        (home / "skills" / "browser" / "codex.txt").write_text("codex made this\n")
        codex_state.ensure_home(workspace, shared=shared)
        assert not (home / "skills" / "browser").is_symlink()
        assert (
            home / "skills" / "browser" / "codex.txt"
        ).read_text() == "codex made this\n"
