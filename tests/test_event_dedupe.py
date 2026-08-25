"""The processed-event set that survives a restart."""

from __future__ import annotations

import json

import pytest

from claude_on_the_fly.event_dedupe import ProcessedEvents


class TestProcessedEvents:
    def test_remembers_across_instances(self, tmp_path):
        """The whole point. An in-memory deque starts empty on every start, so a
        redelivery after a restart used to look like a new message."""
        path = tmp_path / "slack.events.json"
        first = ProcessedEvents(path)
        first.add("1700000000.0001")

        assert "1700000000.0001" in ProcessedEvents(path)

    def test_an_unseen_id_is_not_remembered(self, tmp_path):
        events = ProcessedEvents(tmp_path / "e.json")
        events.add("a")
        assert "b" not in events

    def test_a_repeat_is_not_rewritten(self, tmp_path):
        path = tmp_path / "e.json"
        events = ProcessedEvents(path)
        events.add("a")
        before = path.stat().st_mtime_ns
        events.add("a")
        assert path.stat().st_mtime_ns == before
        assert len(events) == 1

    def test_oldest_ids_fall_off_the_end(self, tmp_path):
        events = ProcessedEvents(tmp_path / "e.json", capacity=3)
        for event_id in ("a", "b", "c", "d"):
            events.add(event_id)
        assert "a" not in events
        assert "d" in events
        assert len(events) == 3

    def test_the_cap_survives_a_reload(self, tmp_path):
        path = tmp_path / "e.json"
        for event_id in ("a", "b", "c", "d"):
            ProcessedEvents(path, capacity=3).add(event_id)
        reloaded = ProcessedEvents(path, capacity=3)
        assert len(reloaded) == 3
        assert "a" not in reloaded

    def test_a_missing_file_starts_empty_and_is_silent(self, tmp_path, caplog):
        with caplog.at_level("WARNING", logger="claude_on_the_fly.event_dedupe"):
            assert len(ProcessedEvents(tmp_path / "absent.json")) == 0
        assert caplog.text == ""

    @pytest.mark.parametrize(
        "body", ["not json at all", '["a"]', '{"ids": "a"}', '{"nope": []}']
    )
    def test_an_unusable_file_degrades_to_empty(self, body, tmp_path, caplog):
        """Starting empty is the behaviour this install had before the file
        existed, so a corrupt one must never stop a daemon."""
        path = tmp_path / "e.json"
        path.write_text(body)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.event_dedupe"):
            events = ProcessedEvents(path)
        assert len(events) == 0
        assert "ignoring" in caplog.text

    def test_non_string_entries_are_dropped(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text(json.dumps({"ids": ["a", 7, None, "b"]}))
        events = ProcessedEvents(path)
        assert len(events) == 2
        assert "a" in events and "b" in events

    def test_an_unreadable_file_is_a_warning_not_a_crash(self, tmp_path, caplog):
        path = tmp_path / "sub" / "e.json"
        path.parent.mkdir()
        path.mkdir()  # a directory where a file belongs: read raises OSError
        with caplog.at_level("WARNING", logger="claude_on_the_fly.event_dedupe"):
            assert len(ProcessedEvents(path)) == 0
        assert "unreadable" in caplog.text

    def test_a_failed_write_does_not_lose_the_message_being_handled(
        self, tmp_path, monkeypatch, caplog
    ):
        """Raising here would cost the turn in flight to protect against a
        duplicate after some future restart. Wrong trade."""
        events = ProcessedEvents(tmp_path / "e.json")

        def refuse(*_a, **_kw):
            raise OSError("read-only file system")

        monkeypatch.setattr("pathlib.Path.write_text", refuse)
        with caplog.at_level("WARNING", logger="claude_on_the_fly.event_dedupe"):
            events.add("a")

        assert "could not write" in caplog.text
        assert "a" in events  # still deduped for the life of this process
