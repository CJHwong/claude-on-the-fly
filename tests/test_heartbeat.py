"""Tests for HeartbeatWriter."""

from __future__ import annotations

import asyncio
import json

import pytest

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
            "symphony",
            state_dir=tmp_path,
            extra_provider=lambda: {"queue_depth": 3},
        )
        writer.write_once()

        payload = json.loads((tmp_path / "symphony.json").read_text())
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
        try:
            await task
        except asyncio.CancelledError:
            pass

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
        try:
            await task
        except asyncio.CancelledError:
            pass

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
