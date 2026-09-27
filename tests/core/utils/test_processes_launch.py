"""Subprocess launch: Windows process creation stays off the Event Loop."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any

import pytest

from core.utils.processes import create_subprocess_exec

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Proactor subprocess transport")


def _gate_os_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event, list[Any]]:
    """Hold OS process creation until the test releases it from the Event Loop."""
    from asyncio import windows_utils

    entered = threading.Event()
    release = threading.Event()
    created: list[Any] = []
    original = windows_utils.Popen

    def gated(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        # Only a free Event Loop can release this; on the loop thread it would time out.
        if not release.wait(5):
            raise TimeoutError("OS process creation blocked the Event Loop")
        popen = original(*args, **kwargs)
        created.append(popen)
        return popen

    monkeypatch.setattr(windows_utils, "Popen", gated)
    return entered, release, created


@pytest.mark.asyncio
async def test_os_process_creation_leaves_the_event_loop_free(monkeypatch):
    entered, release, _created = _gate_os_launch(monkeypatch)
    launch = asyncio.create_task(
        create_subprocess_exec(
            sys.executable, "-c", "print('ready')", stdout=asyncio.subprocess.PIPE
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    release.set()
    process = await asyncio.wait_for(launch, 10)

    output = await asyncio.wait_for(process.stdout.read(), 10)  # type: ignore[union-attr]
    assert output.strip() == b"ready"
    assert await asyncio.wait_for(process.wait(), 10) == 0


@pytest.mark.asyncio
async def test_cancelled_launch_kills_the_process_it_created(monkeypatch):
    entered, release, created = _gate_os_launch(monkeypatch)
    launch = asyncio.create_task(
        create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    launch.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await launch

    (process,) = created
    assert await asyncio.to_thread(process.wait, 10) is not None
