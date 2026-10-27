"""Slack events whose handler raised, kept for the next catch-up to retry."""

from __future__ import annotations

import json

import pytest

from claude_on_the_fly.slack_failed_events import FailedSlackEvents


def _event(ts: str, channel: str = "D1", text: str = "hi") -> dict:
    return {"channel": channel, "ts": ts, "user": "U1", "text": text}


class TestFailedSlackEvents:
    def test_a_failure_survives_a_restart(self, tmp_path):
        path = tmp_path / "slack.failed_events.json"
        FailedSlackEvents(path).record_failure(_event("100.0", text="lost"))

        assert FailedSlackEvents(path).pending(now=101) == [
            _event("100.0", text="lost")
        ]

    def test_pending_is_oldest_first(self, tmp_path):
        store = FailedSlackEvents(tmp_path / "f.json")
        store.record_failure(_event("102.0"))
        store.record_failure(_event("100.0"))
        store.record_failure(_event("101.0", channel="D2"))

        assert [event["ts"] for event in store.pending(now=103)] == [
            "100.0",
            "101.0",
            "102.0",
        ]

    def test_a_repeat_failure_counts_an_attempt_without_a_second_copy(self, tmp_path):
        path = tmp_path / "f.json"
        store = FailedSlackEvents(path)
        store.record_failure(_event("100.0"))
        store.record_failure(_event("100.0"))

        assert len(FailedSlackEvents(path)) == 1
        rows = json.loads(path.read_text())["events"]
        assert rows[0]["attempts"] == 2

    def test_repeated_failures_keep_it_and_warn(self, tmp_path, caplog):
        """Reconnects replay the store during a long outage. A cap would drop
        the event before the restart that ships the fix."""
        path = tmp_path / "f.json"
        store = FailedSlackEvents(path)
        with caplog.at_level("WARNING"):
            for _ in range(5):
                store.record_failure(_event("100.0"))

        assert len(FailedSlackEvents(path)) == 1
        assert json.loads(path.read_text())["events"][0]["attempts"] == 5
        assert "failed again (5 attempts)" in caplog.text

    def test_discard_removes_one_event(self, tmp_path):
        path = tmp_path / "f.json"
        store = FailedSlackEvents(path)
        store.record_failure(_event("100.0"))
        store.record_failure(_event("101.0"))
        store.discard("D1", "100.0")
        store.discard("D1", "999.0")

        assert [event["ts"] for event in FailedSlackEvents(path).pending(now=102)] == [
            "101.0"
        ]

    def test_capacity_drops_the_oldest_with_a_warning(self, tmp_path, caplog):
        path = tmp_path / "f.json"
        store = FailedSlackEvents(path, capacity=2)
        with caplog.at_level("WARNING"):
            for ts in ("100.0", "101.0", "102.0"):
                store.record_failure(_event(ts))

        assert [event["ts"] for event in store.pending(now=103)] == ["101.0", "102.0"]
        assert "full" in caplog.text
        reloaded = FailedSlackEvents(path, capacity=1)
        assert [event["ts"] for event in reloaded.pending(now=103)] == ["102.0"]

    def test_pending_drops_events_older_than_the_window(self, tmp_path, caplog):
        path = tmp_path / "f.json"
        store = FailedSlackEvents(path)
        store.record_failure(_event("100.0"))
        store.record_failure(_event("190.0"))
        with caplog.at_level("WARNING"):
            fresh = store.pending(now=200, max_age_seconds=20)

        assert [event["ts"] for event in fresh] == ["190.0"]
        assert len(FailedSlackEvents(path)) == 1
        assert "older than" in caplog.text

    def test_the_default_window_is_a_day(self, tmp_path):
        store = FailedSlackEvents(tmp_path / "f.json")
        store.record_failure(_event("1000.0"))

        assert store.pending(now=1000 + 5 * 3600)
        assert not store.pending(now=1000 + 25 * 3600)

    @pytest.mark.parametrize(
        "event",
        [
            {"ts": "100.0"},
            {"channel": "D1"},
            {"channel": "D1", "ts": "not-a-timestamp"},
            {"channel": "D1", "ts": "100.0", "text": object()},
        ],
    )
    def test_an_unusable_event_is_not_stored(self, event, tmp_path, caplog):
        path = tmp_path / "f.json"
        store = FailedSlackEvents(path)
        with caplog.at_level("WARNING"):
            store.record_failure(event)

        assert len(FailedSlackEvents(path)) == 0

    @pytest.mark.parametrize(
        "body",
        ["not json", "[]", '{"events": "bad"}', '{"nope": []}'],
    )
    def test_an_unusable_file_degrades_to_empty(self, body, tmp_path, caplog):
        path = tmp_path / "f.json"
        path.write_text(body)
        with caplog.at_level("WARNING"):
            assert len(FailedSlackEvents(path)) == 0
        assert "ignoring" in caplog.text

    def test_a_missing_file_starts_empty_and_is_silent(self, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            assert len(FailedSlackEvents(tmp_path / "absent.json")) == 0
        assert caplog.text == ""

    def test_invalid_rows_are_ignored(self, tmp_path):
        path = tmp_path / "f.json"
        path.write_text(
            json.dumps(
                {
                    "events": [
                        {"event": _event("100.0"), "attempts": 1},
                        "not a row",
                        {"event": "not an event", "attempts": 1},
                        {"event": _event("101.0"), "attempts": "one"},
                        {"event": {"channel": "D1", "ts": "bad"}, "attempts": 1},
                    ]
                }
            )
        )
        assert [event["ts"] for event in FailedSlackEvents(path).pending(now=101)] == [
            "100.0"
        ]

    def test_a_failed_write_keeps_the_event_in_memory(
        self, tmp_path, monkeypatch, caplog
    ):
        store = FailedSlackEvents(tmp_path / "f.json")

        def refuse(*_args, **_kwargs):
            raise OSError("read-only")

        monkeypatch.setattr("pathlib.Path.write_text", refuse)
        with caplog.at_level("WARNING"):
            store.record_failure(_event("100.0"))

        assert len(store) == 1
        assert "could not write" in caplog.text
