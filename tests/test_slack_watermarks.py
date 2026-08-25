"""Durable, bounded Slack restart watermarks."""

from __future__ import annotations

import json

import pytest

from claude_on_the_fly.slack_watermarks import SlackWatermarks


class TestSlackWatermarks:
    def test_remembers_timestamp_and_channel_type_across_instances(self, tmp_path):
        path = tmp_path / "slack.watermarks.json"
        SlackWatermarks(path).record("D1", "1700000000.1", "im")

        entry = SlackWatermarks(path).recent(now=1700000010)[0]
        assert (entry.channel, entry.ts, entry.channel_type) == (
            "D1",
            "1700000000.1",
            "im",
        )

    def test_older_timestamp_never_moves_a_channel_backward(self, tmp_path):
        path = tmp_path / "w.json"
        watermarks = SlackWatermarks(path)
        watermarks.record("D1", "200.0", "im")
        watermarks.record("D1", "100.0")

        assert SlackWatermarks(path).recent(now=201)[0].ts == "200.0"

    def test_a_later_kind_can_fill_an_existing_entry(self, tmp_path):
        path = tmp_path / "w.json"
        watermarks = SlackWatermarks(path)
        watermarks.record("D1", "100.0")
        watermarks.record("D1", "100.0", "im")

        assert SlackWatermarks(path).recent(now=101)[0].channel_type == "im"

    def test_capacity_keeps_only_the_most_recent_channels(self, tmp_path):
        path = tmp_path / "w.json"
        watermarks = SlackWatermarks(path, capacity=2)
        watermarks.record("D1", "100.0", "im")
        watermarks.record("D2", "101.0", "im")
        watermarks.record("D3", "102.0", "im")

        restored = SlackWatermarks(path, capacity=2).recent(now=103)
        assert [row.channel for row in restored] == ["D2", "D3"]

    def test_loading_trims_a_file_written_with_a_larger_capacity(self, tmp_path):
        path = tmp_path / "w.json"
        SlackWatermarks(path, capacity=3).record("D1", "100.0", "im")
        SlackWatermarks(path, capacity=3).record("D2", "101.0", "im")
        SlackWatermarks(path, capacity=3).record("D3", "102.0", "im")

        restored = SlackWatermarks(path, capacity=2).recent(now=103)
        assert [row.channel for row in restored] == ["D2", "D3"]

    def test_recent_excludes_watermarks_older_than_the_bound(self, tmp_path):
        watermarks = SlackWatermarks(tmp_path / "w.json")
        watermarks.record("OLD", "100.0", "im")
        watermarks.record("NEW", "190.0", "im")

        assert [
            row.channel for row in watermarks.recent(now=200, max_age_seconds=20)
        ] == ["NEW"]

    def test_a_repeat_is_not_rewritten(self, tmp_path):
        path = tmp_path / "w.json"
        watermarks = SlackWatermarks(path)
        watermarks.record("D1", "100.0", "im")
        before = path.stat().st_mtime_ns
        watermarks.record("D1", "100.0", "im")
        assert path.stat().st_mtime_ns == before

    @pytest.mark.parametrize(
        "body", ["not json", "[]", '{"channels": "bad"}', '{"nope": []}']
    )
    def test_an_unusable_file_degrades_to_empty(self, body, tmp_path, caplog):
        path = tmp_path / "w.json"
        path.write_text(body)
        with caplog.at_level("WARNING"):
            assert len(SlackWatermarks(path)) == 0
        assert "ignoring" in caplog.text

    def test_invalid_rows_are_ignored(self, tmp_path):
        path = tmp_path / "w.json"
        path.write_text(
            json.dumps(
                {
                    "channels": [
                        {"channel": "D1", "ts": "100.0", "channel_type": "im"},
                        "not a row",
                        {"channel": "D2", "ts": "not-a-timestamp"},
                        {"channel": 7, "ts": "101.0"},
                    ]
                }
            )
        )
        assert [row.channel for row in SlackWatermarks(path).recent(now=101)] == ["D1"]

    def test_a_failed_write_keeps_the_in_memory_position(
        self, tmp_path, monkeypatch, caplog
    ):
        watermarks = SlackWatermarks(tmp_path / "w.json")

        def refuse(*_args, **_kwargs):
            raise OSError("read-only")

        monkeypatch.setattr("pathlib.Path.write_text", refuse)
        with caplog.at_level("WARNING"):
            watermarks.record("D1", "100.0", "im")

        assert watermarks.recent(now=101)[0].channel == "D1"
        assert "could not write" in caplog.text
