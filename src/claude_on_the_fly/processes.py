"""Shared teardown for subprocesses started in dedicated sessions."""

import asyncio
import contextlib
import os
import signal


async def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the dedicated group, then reap its leader before propagating cancellation.

    The group can outlive its leader. Kill it even when returncode is set.
    Shield the wait and retain its task across repeated cancellation so shutdown
    cannot finish while cleanup is still running. Process announcements belong
    to callers, not this helper.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    waiter = asyncio.create_task(proc.wait())
    cancelled = False
    with contextlib.suppress(ProcessLookupError):
        while not waiter.done():
            try:
                await asyncio.shield(waiter)
            except asyncio.CancelledError:
                cancelled = True
        waiter.result()
    if cancelled:
        raise asyncio.CancelledError
