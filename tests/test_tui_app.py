"""Smoke tests for the Textual app — boot, navigate, exit without crashing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from claude_on_the_fly.tui.tui_app import (
    ClaudeTuiApp,
    _event_sig,
    _events_since,
)


def _ev(ts, type_, ident, uuid="u", **extra):
    return {"ts": ts, "type": type_, "identifier": ident, "session_uuid": uuid, **extra}


class _FakeLog:
    """Stand-in for EventLog: tail(n) returns the last n of a mutable list."""

    def __init__(self, records):
        self._records = records

    def tail(self, n):
        return self._records[-n:]


class TestEventsSince:
    def test_none_baseline_yields_nothing(self):
        # Priming the baseline: nothing is "new" until we've recorded a mark.
        recs = [_ev("t1", "dispatched", "A"), _ev("t2", "worker_done", "B")]
        assert _events_since(recs, None) == []

    def test_returns_rows_after_last_seen(self):
        a = _ev("t1", "dispatched", "A", "u1")
        b = _ev("t2", "worker_done", "B", "u2")
        c = _ev("t3", "worker_failed", "C", "u3")
        assert _events_since([a, b, c], _event_sig(a)) == [b, c]

    def test_aged_out_baseline_treats_all_as_new(self):
        # last_sig no longer in the window → over-notify rather than drop.
        b = _ev("t2", "worker_done", "B", "u2")
        c = _ev("t3", "worker_done", "C", "u3")
        gone = _event_sig(_ev("t0", "dispatched", "Z", "u0"))
        assert _events_since([b, c], gone) == [b, c]

    def test_same_ts_disambiguated_by_uuid(self):
        a = _ev("t1", "worker_done", "A", "u1")
        b = _ev("t1", "worker_done", "B", "u2")  # same ts, different job
        assert _events_since([a, b], _event_sig(a)) == [b]


class TestWorkerEventNotify:
    def _bare_app(self):
        app = ClaudeTuiApp()
        # Stub the Textual methods so the handler can be exercised without a
        # running app/driver.
        app.notify = MagicMock()  # ty: ignore[invalid-assignment]
        app.bell = MagicMock()  # ty: ignore[invalid-assignment]
        return app

    def test_done_notifies_info_with_bell(self):
        app = self._bare_app()
        app._notify_worker_event(
            _ev("t", "worker_done", "owner/repo#9", reason="inactive")
        )
        app.notify.assert_called_once()
        assert "owner/repo#9" in app.notify.call_args.args[0]
        assert app.notify.call_args.kwargs["severity"] == "information"
        app.bell.assert_called_once()

    def test_failed_notifies_error_with_bell(self):
        app = self._bare_app()
        app._notify_worker_event(_ev("t", "worker_failed", "ACES-3"))
        assert app.notify.call_args.kwargs["severity"] == "error"
        assert "ACES-3" in app.notify.call_args.args[0]
        app.bell.assert_called_once()

    def test_poll_primes_then_notifies_only_new_worker_events(self):
        app = self._bare_app()
        seed = _ev("t1", "dispatched", "X", "u1")
        log = _FakeLog([seed])
        app._event_log = log
        app._last_event_sig = app._latest_event_sig()  # baseline = seed

        app._poll_worker_events()  # nothing newer than the baseline
        app.notify.assert_not_called()

        log._records.append(_ev("t2", "dispatched", "Y", "u2"))  # not a worker event
        log._records.append(_ev("t3", "worker_done", "owner/repo#1", "u3"))
        app._poll_worker_events()

        app.notify.assert_called_once()
        assert "owner/repo#1" in app.notify.call_args.args[0]
        app.bell.assert_called_once()


@pytest.mark.asyncio
async def test_app_boots_to_dashboard():
    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DashboardScreen"


@pytest.mark.asyncio
async def test_press_d_pushes_doctor():
    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DoctorScreen"


@pytest.mark.asyncio
async def test_press_l_pushes_logs():
    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "LogsScreen"


@pytest.mark.asyncio
async def test_escape_returns_from_doctor():
    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DashboardScreen"


@pytest.mark.asyncio
async def test_g_opens_config_preview_and_escape_returns(tmp_path, monkeypatch):
    """`g` shows the resolved config; Esc returns to the dashboard."""
    import claude_on_the_fly.tui.screens.config_preview as cp

    # Point the preview at a throwaway config so it renders real content
    # without touching the user's real ~/.claude-on-the-fly/symphony.yaml.
    cfg = tmp_path / "symphony.yaml"
    cfg.write_text(
        "tracker:\n  base_url: https://x.atlassian.net\n  project_key: PROJ\n"
    )
    monkeypatch.setattr(cp, "CONFIG_PATH", cfg)

    from textual.widgets import Static

    app = ClaudeTuiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfigPreviewScreen"
        body = app.screen.query_one("#config-preview", Static)
        assert "project_key: PROJ" in str(body.render())
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DashboardScreen"
