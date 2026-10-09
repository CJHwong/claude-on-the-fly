"""Tests for resource_watch.py: the state machine, and one run against the real host."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("psutil")

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
    state, alert = watch.step({}, usage(95), 0, ARGS)
    assert alert is None and state == {"high_since": 0}
    state, alert = watch.step(state, usage(95), 200, ARGS)
    assert alert is None


def test_sustained_load_alerts_once_then_reminds():
    state, _ = watch.step({}, usage(95), 0, ARGS)
    state, alert = watch.step(state, usage(10, 96), 300, ARGS)
    assert alert == "cpu 10%, memory 96%; memory high for 5 min"
    state, alert = watch.step(state, usage(95), 600, ARGS)
    assert alert is None  # inside the reminder window
    state, alert = watch.step(state, usage(95), 3900, ARGS)
    assert alert is not None and state["alerted_at"] == 3900


def test_the_alert_clears_only_below_the_recovery_threshold():
    state = {"high_since": 0, "alerted_at": 300}
    kept, alert = watch.step(state, usage(85), 400, ARGS)
    assert kept == state and alert is None
    cleared, alert = watch.step(state, usage(70), 500, ARGS)
    assert cleared == {} and alert is None


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
