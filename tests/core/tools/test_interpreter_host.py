"""InterpreterHost: objects on subinterpreter workers, also while the first one starts.

The render host's test (``test_terminal_emulator.py``) covers rendering on a
worker and the closed host; here the starts of the workers themselves.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from types import SimpleNamespace

import pytest

from core.tools import _interpreter_host
from core.tools._interpreter_host import HostedObject, InterpreterHost


@pytest.mark.asyncio
@pytest.mark.parametrize("start_fails", [False, True], ids=["started", "start-failed"])
async def test_objects_opened_while_the_first_worker_starts_wait_for_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, start_fails: bool
) -> None:
    starting = threading.Event()
    gate = threading.Event()
    start_worker = _interpreter_host._InterpreterWorker

    def gated_start(*arguments: object) -> object:
        # The test decides when the start ends, and whether it fails.
        starting.set()
        gate.wait(5)
        if start_fails:
            raise RuntimeError("subinterpreters are unavailable")
        return start_worker(*arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(_interpreter_host, "_InterpreterWorker", gated_start)
    host = InterpreterHost((), name="test", workers=1)

    def launch() -> HostedObject:
        # As a command's launch: prepare off the Event Loop, then open.
        host.prepare()
        return host.open(SimpleNamespace, thread=threading.get_ident)

    try:
        with (
            caplog.at_level(logging.WARNING),
            concurrent.futures.ThreadPoolExecutor(3) as pool,
        ):
            first = pool.submit(launch)
            assert await asyncio.to_thread(starting.wait, 5)
            later = [pool.submit(launch) for _ in range(2)]
            # Every allowed worker is starting: they wait for it instead of
            # running their objects on the Event Loop.
            done, _ = await asyncio.to_thread(concurrent.futures.wait, later, 0.1)
            assert not done
            gate.set()
            objects = [future.result(5) for future in (first, *later)]
        threads = {await hosted.call("thread") for hosted in objects}
    finally:
        host.close()

    here = threading.get_ident()
    warnings = [
        record
        for record in caplog.records
        if record.name.endswith("interpreter_host") and record.levelno == logging.WARNING
    ]
    if start_fails:
        # No worker can start: every object runs inline, reported once beside the failure.
        assert threads == {here}
        assert len(warnings) == 2
    else:
        assert len(threads) == 1 and here not in threads
        assert warnings == []
