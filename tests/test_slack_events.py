from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_on_the_fly.slack import SlackFrontend
from claude_on_the_fly.slack_events import (
    CHANNEL_CAP,
    PROCESSED_CAP,
    SlackEventSnapshot,
    SlackEventState,
)


def test_missing_state_is_empty(tmp_path: Path) -> None:
    assert SlackEventState(tmp_path / "missing.json").read() == SlackEventSnapshot()


def test_roundtrip_is_atomic_private_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / "state" / "slack.events.json"
    state = SlackEventState(path)
    processed = [f"{i}.0" for i in range(PROCESSED_CAP + 2)]
    channels = {f"C{i}": f"{i}.0" for i in range(CHANNEL_CAP + 2)}
    types = {channel: "im" for channel in channels}

    state.write(processed, channels, types)

    restored = state.read()
    assert restored.processed_ts == tuple(processed[-PROCESSED_CAP:])
    assert len(restored.active_channels) == CHANNEL_CAP
    assert set(restored.channel_types) == set(restored.active_channels)
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("body", ["not json", "[]"])
def test_invalid_file_fails_open(tmp_path: Path, body: str, caplog) -> None:
    path = tmp_path / "events.json"
    path.write_text(body)
    with caplog.at_level("WARNING", logger="claude_on_the_fly.slack_events"):
        assert SlackEventState(path).read() == SlackEventSnapshot()
    assert "slack events" in caplog.text


def test_invalid_members_are_filtered(tmp_path: Path) -> None:
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps(
            {
                "processed_ts": ["1.0", 2],
                "active_channels": {"C1": "1.0", "C2": 2},
                "channel_types": "wrong",
            }
        )
    )
    restored = SlackEventState(path).read()
    assert restored.processed_ts == ("1.0",)
    assert restored.active_channels == {"C1": "1.0"}
    assert restored.channel_types == {}


def test_failed_replace_removes_staging_file(tmp_path: Path) -> None:
    path = tmp_path / "events.json"
    state = SlackEventState(path)
    with (
        patch.object(Path, "replace", side_effect=OSError("read only")),
        pytest.raises(OSError, match="read only"),
    ):
        state.write(["1.0"], {"C1": "1.0"}, {"C1": "im"})
    assert list(tmp_path.glob("*.tmp")) == []
    assert not path.exists()


@patch("claude_on_the_fly.slack.AsyncApp")
def test_frontend_restores_and_persists_watermarks(mock_app, tmp_path: Path) -> None:
    state = MagicMock()
    state.read.return_value = SlackEventSnapshot(("2.0",), {"C1": "2.0"}, {"C1": "im"})
    frontend = SlackFrontend("xapp", "xoxp", "U1", event_state=state)
    assert list(frontend._processed_ts) == ["2.0"]
    assert frontend._active_channels == {"C1": "2.0"}

    frontend._remember_event("1.0", "C1", "channel")

    # Dedup remembers the observation, while the catch-up watermark never moves
    # backwards when history returns an older event.
    assert list(frontend._processed_ts) == ["2.0", "1.0"]
    assert frontend._active_channels == {"C1": "2.0"}
    assert frontend._channel_types["C1"] == "channel"
    state.write.assert_called_once()


@patch("claude_on_the_fly.slack.AsyncApp")
def test_persistence_failure_keeps_in_memory_dedup(
    mock_app, tmp_path: Path, caplog
) -> None:
    state = MagicMock()
    state.read.return_value = SlackEventSnapshot()
    state.write.side_effect = OSError("disk full")
    frontend = SlackFrontend("xapp", "xoxp", "U1", event_state=state)
    with caplog.at_level("ERROR", logger="claude_on_the_fly.slack"):
        frontend._remember_event("3.0", "C3", "im")
    assert "3.0" in frontend._processed_ts
    assert "could not persist" in caplog.text
