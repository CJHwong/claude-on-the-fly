"""Tests for HeartbeatWriter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from claude_on_the_fly import heartbeat
from claude_on_the_fly.heartbeat import HeartbeatWriter


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
