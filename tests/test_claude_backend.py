"""Tests for Claude backend implementation details."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_on_the_fly.backends import claude as claude_mod


class TestExecPty:
    async def test_cancellation_reaps_the_process_group(self) -> None:
        """Frontends cancel a running turn to implement $stop."""
        started = asyncio.Event()

        async def never_finishes() -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()
            return b"", b""  # pragma: no cover

        proc = MagicMock()
        proc.returncode = None
        proc.communicate = never_finishes

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(
                claude_mod.agent, "_kill_process_tree", new_callable=AsyncMock
            ) as kill_process_tree,
        ):
            task = asyncio.create_task(
                claude_mod._exec_pty(Path("/tmp"), ["claude-pty", "hello"])
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        kill_process_tree.assert_awaited_once_with(proc)
