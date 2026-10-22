"""Tests for resource_watch.py: the state machine, and runs against the real host."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "resource_watch", Path(__file__).with_name("resource_watch.py")
)
watch = importlib.util.module_from_spec(SPEC)
sys.modules["resource_watch"] = watch
SPEC.loader.exec_module(watch)

ARGS = watch.parse_args(
    [
        "--threshold",
        "90",
        "--recovery-threshold",
        "80",
        "--sustain-seconds",
        "300",
        "--reminder-seconds",
        "3600",
    ]
)


def usage(cpu: float, memory: float = 10) -> dict[str, float]:
    return {"cpu": cpu, "memory": memory}


def test_a_short_spike_does_not_alert():
    state, alert, _ = watch.step({}, usage(95), 0, ARGS)
    assert alert is None and state == {"high_since": 0}
    state, alert, _ = watch.step(state, usage(95), 200, ARGS)
    assert alert is None


def test_sustained_load_alerts_once_then_reminds():
    state, _, _ = watch.step({}, usage(95), 0, ARGS)
    state, alert, _ = watch.step(state, usage(10, 96), 300, ARGS)
    assert alert == "cpu 10%, memory 96%; memory high for 5 min"
    state, alert, _ = watch.step(state, usage(95), 600, ARGS)
    assert alert is None  # inside the reminder window
    state, alert, _ = watch.step(state, usage(95), 3900, ARGS)
    assert alert is not None and state["alerted_at"] == 3900


def test_the_alert_clears_only_below_the_recovery_threshold():
    state = {"high_since": 0, "alerted_at": 300}
    kept, alert, _ = watch.step(state, usage(85), 400, ARGS)
    assert kept == state and alert is None
    cleared, alert, recovered = watch.step(state, usage(70), 500, ARGS)
    assert (
        cleared == {}
        and alert is None
        and recovered == "recovered: cpu 70%, memory 10%"
    )
    _, _, recovered = watch.step({"high_since": 0}, usage(70), 500, ARGS)
    assert recovered is None  # it never alerted, so there is nothing to clear


def test_recovery_above_threshold_is_refused():
    with pytest.raises(SystemExit):
        watch.parse_args(["--threshold", "50", "--recovery-threshold", "60"])


def test_main_writes_state_and_exits_by_alert(tmp_path, monkeypatch, capsys):
    path = tmp_path / "s" / "state.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"high_since": 0}))
    monkeypatch.setattr(watch, "sample", lambda: usage(99))
    assert watch.main(["--state", str(path), "--sustain-seconds", "0"]) == 1
    assert "cpu 99%" in capsys.readouterr().out
    assert "alerted_at" in json.loads(path.read_text())


def test_unreadable_state_starts_fresh(tmp_path, capsys):
    path = tmp_path / "state.json"
    path.write_text("{")
    assert watch.load(path) == {}
    assert "ignoring unreadable state" in capsys.readouterr().err
    assert watch.load(tmp_path / "missing.json") == {}


def test_one_real_sample(tmp_path):
    reading = watch.sample()
    assert set(reading) == {"cpu", "memory"} and all(
        0 <= v <= 100 for v in reading.values()
    )
    assert (
        watch.main(
            [
                "--state",
                str(tmp_path / "s.json"),
                "--threshold",
                "100.1",
                "--recovery-threshold",
                "100.1",
            ]
        )
        == 0
    )


def test_recovery_runs_the_command_once(tmp_path, monkeypatch, capsys):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"high_since": 0, "alerted_at": 1}))
    out = tmp_path / "sent.txt"
    monkeypatch.setattr(watch, "sample", lambda: usage(5))
    command = f'printf "%s" "$RESOURCE_WATCH_MESSAGE" > {out}'
    assert watch.main(["--state", str(path), "--recovery-command", command]) == 0
    assert out.read_text() == "recovered: cpu 5%, memory 10%"
    assert json.loads(path.read_text()) == {}
    out.unlink()
    assert watch.main(["--state", str(path), "--recovery-command", command]) == 0
    assert not out.exists()  # already clear: no second notice


def test_a_failing_recovery_command_alerts(tmp_path, monkeypatch, capsys):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"high_since": 0, "alerted_at": 1}))
    monkeypatch.setattr(watch, "sample", lambda: usage(5))
    assert watch.main(["--state", str(path), "--recovery-command", "exit 3"]) == 1
    assert "--recovery-command exited 3" in capsys.readouterr().out


def test_recovery_without_a_command_only_logs(tmp_path, monkeypatch, capsys):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"high_since": 0, "alerted_at": 1}))
    monkeypatch.setattr(watch, "sample", lambda: usage(5))
    assert watch.main(["--state", str(path)]) == 0
    assert "recovered" in capsys.readouterr().out
