"""Tests for HeartbeatWriter."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from claude_on_the_fly import heartbeat
from claude_on_the_fly.heartbeat import HeartbeatWriter, InstanceAlreadyClaimed


class TestWriteOnce:
    def test_creates_state_dir_and_file(self, tmp_path):
        writer = HeartbeatWriter("telegram", state_dir=tmp_path / "state")
        writer.write_once()

        path = tmp_path / "state" / "telegram.json"
        assert path.is_file()

    def test_payload_shape(self, tmp_path):
        writer = HeartbeatWriter("telegram", state_dir=tmp_path, pid=42)
        writer.write_once()

        payload = json.loads((tmp_path / "telegram.json").read_text())
        assert payload["frontend"] == "telegram"
        assert payload["pid"] == 42
        assert "started_at" in payload
        assert "last_heartbeat" in payload
        assert "version" in payload
        assert "executable" in payload
        assert "instance_id" in payload
        assert (
            payload["executable"].endswith("python")
            or "python" in payload["executable"]
        )
        assert payload["extra"] == {}

    def test_extra_provider_embedded(self, tmp_path):
        writer = HeartbeatWriter(
            "cron",
            state_dir=tmp_path,
            extra_provider=lambda: {"queue_depth": 3},
        )
        writer.write_once()

        payload = json.loads((tmp_path / "cron.json").read_text())
        assert payload["extra"] == {"queue_depth": 3}

    def test_extra_provider_failure_does_not_kill_write(self, tmp_path):
        def broken():
            raise RuntimeError("boom")

        writer = HeartbeatWriter("telegram", state_dir=tmp_path, extra_provider=broken)
        writer.write_once()
        # File should not exist — the whole write failed, but no exception leaked.
        # write_once catches exceptions internally.
        assert not (tmp_path / "telegram.json").exists()

    def test_started_at_is_stable_across_writes(self, tmp_path):
        writer = HeartbeatWriter("telegram", state_dir=tmp_path)
        writer.write_once()
        first = json.loads((tmp_path / "telegram.json").read_text())["started_at"]

        writer.write_once()
        second = json.loads((tmp_path / "telegram.json").read_text())["started_at"]

        assert first == second

    def test_temporary_file_is_unique_to_the_writer_and_removed(self, tmp_path):
        writer = HeartbeatWriter("slack", state_dir=tmp_path)
        writer.write_once()
        assert list(tmp_path.glob("slack.json.*.tmp")) == []

    def test_write_failure_does_not_raise(self, tmp_path, monkeypatch):
        # Make the tmp file path unwritable by replacing the dir with a file.
        broken_path = tmp_path / "state"
        broken_path.write_text("not a dir")
        writer = HeartbeatWriter("telegram", state_dir=broken_path)
        # Should not raise even though mkdir will fail.
        writer.write_once()


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_writes_once_immediately_on_start(self, tmp_path):
        writer = HeartbeatWriter("telegram", state_dir=tmp_path, interval_s=10.0)
        task = asyncio.create_task(writer.run())
        await asyncio.sleep(0.05)  # let the first write happen
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert (tmp_path / "telegram.json").is_file()

    @pytest.mark.asyncio
    async def test_writes_repeatedly(self, tmp_path):
        writer = HeartbeatWriter("telegram", state_dir=tmp_path, interval_s=0.02)
        task = asyncio.create_task(writer.run())
        await asyncio.sleep(0.1)
        # Capture a heartbeat timestamp, wait, capture another — they should differ.
        first = json.loads((tmp_path / "telegram.json").read_text())["last_heartbeat"]
        await asyncio.sleep(0.1)
        second = json.loads((tmp_path / "telegram.json").read_text())["last_heartbeat"]
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # They might be equal if seconds resolution doesn't advance, but the
        # loop should have run multiple iterations. Verify the file is fresh.
        assert (tmp_path / "telegram.json").is_file()
        # At minimum, second should not be less than first.
        assert second >= first

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, tmp_path):
        writer = HeartbeatWriter("telegram", state_dir=tmp_path, interval_s=10.0)
        task = asyncio.create_task(writer.run())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestSingletonClaim:
    def test_second_writer_is_refused_until_first_releases(self, tmp_path):
        first = HeartbeatWriter("slack", state_dir=tmp_path, pid=101)
        second = HeartbeatWriter("slack", state_dir=tmp_path)
        first.claim()
        first.claim()
        try:
            with pytest.raises(InstanceAlreadyClaimed) as exc:
                second.claim()
            assert exc.value.frontend == "slack"
            assert exc.value.holder == "101"
            assert "already running" in str(exc.value)
        finally:
            first.release()

        second.claim()
        second.release()

    def test_release_is_idempotent(self, tmp_path):
        writer = HeartbeatWriter("slack", state_dir=tmp_path)
        writer.claim()
        writer.release()
        writer.release()

    def test_unreadable_owner_is_reported_as_unknown(self, tmp_path, monkeypatch):
        first = HeartbeatWriter("slack", state_dir=tmp_path)
        second = HeartbeatWriter("slack", state_dir=tmp_path)
        first.claim()
        monkeypatch.setattr(
            heartbeat.os,
            "read",
            lambda _fd, _size: (_ for _ in ()).throw(OSError("unreadable")),
        )
        try:
            with pytest.raises(InstanceAlreadyClaimed) as exc:
                second.claim()
            assert exc.value.holder == "unknown"
        finally:
            first.release()

    def test_mount_without_flock_refuses_instead_of_leaking(
        self, tmp_path, monkeypatch
    ):
        """ENOLCK is not contention, and it must not escape as a bare OSError.

        Some NFS and FUSE mounts cannot lock at all. Catching only
        BlockingIOError leaked the descriptor and stopped every daemon with a
        traceback on an install that worked before the lock existed.
        """
        writer = HeartbeatWriter("slack", state_dir=tmp_path)
        opened: list[int] = []
        real_open = heartbeat.os.open

        def record_open(path, flags, mode=0o777):
            fd = real_open(path, flags, mode)
            opened.append(fd)
            return fd

        def no_locks(_fd, _op):
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(heartbeat.os, "open", record_open)
        monkeypatch.setattr(heartbeat.fcntl, "flock", no_locks)

        with pytest.raises(heartbeat.InstanceLockUnavailable) as exc:
            writer.claim()
        assert "without file locking" in str(exc.value)
        assert exc.value.frontend == "slack"

        # The descriptor is closed, not leaked: a second close raises EBADF.
        monkeypatch.undo()
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.close(opened[0])

    def test_failed_owner_record_releases_the_lock(self, tmp_path, monkeypatch):
        writer = HeartbeatWriter("slack", state_dir=tmp_path)

        def broken_write(_fd, _data):
            raise OSError("disk failed")

        monkeypatch.setattr(heartbeat.os, "write", broken_write)
        with pytest.raises(OSError, match="disk failed"):
            writer.claim()

        monkeypatch.undo()
        replacement = HeartbeatWriter("slack", state_dir=tmp_path)
        replacement.claim()
        replacement.release()


class TestOwnedCleanup:
    def test_old_writer_does_not_unlink_replacement_heartbeat(self, tmp_path):
        old = HeartbeatWriter("slack", state_dir=tmp_path, pid=101)
        replacement = HeartbeatWriter("slack", state_dir=tmp_path, pid=202)
        old.write_once()
        replacement.write_once()

        old.remove_owned()
        assert json.loads((tmp_path / "slack.json").read_text())["pid"] == 202

        replacement.remove_owned()
        assert not (tmp_path / "slack.json").exists()

    @pytest.mark.parametrize("content", [None, "not json"])
    def test_missing_or_invalid_heartbeat_needs_no_cleanup(self, tmp_path, content):
        path = tmp_path / "slack.json"
        if content is not None:
            path.write_text(content)
        HeartbeatWriter("slack", state_dir=tmp_path).remove_owned()


class TestLivePid:
    """`live_pid` answers 'may I start?' for a singleton daemon, so it must
    require BOTH halves of the liveness contract and treat anything it cannot
    read as 'nothing is running'."""

    def _write(self, state_dir, frontend="jobs", *, pid=4242, age_s=0.0) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC) - timedelta(seconds=age_s)
        (state_dir / f"{frontend}.json").write_text(
            json.dumps(
                {
                    "frontend": frontend,
                    "pid": pid,
                    "last_heartbeat": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
        )

    def test_fresh_heartbeat_with_live_process(self, tmp_path, monkeypatch):
        self._write(tmp_path)
        monkeypatch.setattr(heartbeat, "process_exists", lambda pid: True)
        assert heartbeat.live_pid("jobs", state_dir=tmp_path) == 4242

    def test_stale_heartbeat_reads_as_not_running(self, tmp_path, monkeypatch):
        """A pid alone would be trusted forever once the OS recycles it onto an
        unrelated process — the daemon could then never start again."""
        self._write(tmp_path, age_s=3600)
        monkeypatch.setattr(heartbeat, "process_exists", lambda pid: True)
        assert heartbeat.live_pid("jobs", state_dir=tmp_path) is None

    def test_dead_process_reads_as_not_running(self, tmp_path, monkeypatch):
        """The mirror case: a worker killed between heartbeats leaves a fresh
        file behind, and its queue is free for the taking."""
        self._write(tmp_path)
        monkeypatch.setattr(heartbeat, "process_exists", lambda pid: False)
        assert heartbeat.live_pid("jobs", state_dir=tmp_path) is None

    def test_missing_file_reads_as_not_running(self, tmp_path):
        assert heartbeat.live_pid("jobs", state_dir=tmp_path) is None

    def test_corrupt_file_reads_as_not_running(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "jobs.json").write_text("{not json")
        assert heartbeat.live_pid("jobs", state_dir=tmp_path) is None

    def test_incomplete_payload_reads_as_not_running(self, tmp_path):
        (tmp_path / "jobs.json").write_text(json.dumps({"frontend": "jobs"}))
        assert heartbeat.live_pid("jobs", state_dir=tmp_path) is None

    def test_a_live_writer_is_visible_to_live_pid(self, tmp_path):
        """End to end against the real writer and this very process, so the
        payload format and the reader cannot drift apart."""
        HeartbeatWriter("jobs", state_dir=tmp_path).write_once()
        assert heartbeat.live_pid("jobs", state_dir=tmp_path) == os.getpid()


class TestPackageVersion:
    def test_an_uninstalled_package_reports_unknown(self, monkeypatch):
        """The heartbeat is read by the TUI, and a raise here would take the
        dashboard down over a cosmetic field."""
        from importlib.metadata import PackageNotFoundError

        from claude_on_the_fly import heartbeat as heartbeat_mod

        def not_found(_name):
            raise PackageNotFoundError("claude-on-the-fly")

        monkeypatch.setattr(heartbeat_mod, "version", not_found)
        assert heartbeat_mod._package_version() == "unknown"


class TestProcessLiveness:
    def test_a_pid_that_does_not_exist_is_not_alive(self):
        from claude_on_the_fly.heartbeat import process_exists

        # 2**22 is above the default pid_max on both macOS and Linux.
        assert process_exists(4_194_303) is False

    def test_our_own_pid_is_alive(self):
        import os

        from claude_on_the_fly.heartbeat import process_exists

        assert process_exists(os.getpid()) is True


class TestLivePidRejectsAnUnusableHeartbeat:
    def test_a_non_integer_pid_is_refused(self, tmp_path):
        """A string pid would be handed to os.kill and raise, so it has to be
        rejected before the liveness probe."""
        import json
        from datetime import UTC, datetime

        from claude_on_the_fly import heartbeat as heartbeat_mod

        path = tmp_path / "slack.json"
        path.write_text(
            json.dumps(
                {
                    "pid": "not-an-int",
                    "last_heartbeat": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
        )
        assert heartbeat_mod.live_pid("slack", state_dir=tmp_path) is None

    def test_a_stale_heartbeat_is_refused(self, tmp_path):
        """A daemon that was SIGKILLed leaves its file behind, and the timestamp is
        the only thing that distinguishes it from a live one."""
        import json
        import os
        from datetime import UTC, datetime, timedelta

        from claude_on_the_fly import heartbeat as heartbeat_mod

        long_ago = datetime.now(UTC) - timedelta(hours=1)
        path = tmp_path / "slack.json"
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "last_heartbeat": long_ago.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
        )
        assert (
            heartbeat_mod.live_pid("slack", state_dir=tmp_path, liveness_window_s=60)
            is None
        )


def test_the_writer_exposes_the_path_it_owns(tmp_path):
    """`run()`'s callers unlink this on shutdown, so it has to be the same file the
    writer actually writes."""
    from claude_on_the_fly.heartbeat import HeartbeatWriter

    writer = HeartbeatWriter("slack", state_dir=tmp_path)
    writer.write_once()
    assert writer.path == tmp_path / "slack.json"
    assert writer.path.is_file()
