"""Smoke tests for the Textual app — boot, navigate, exit without crashing."""

from __future__ import annotations

import pytest

from claude_on_the_fly.tui.tui_app import ClaudeTuiApp


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
