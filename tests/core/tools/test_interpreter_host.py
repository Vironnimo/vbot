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
from core.tools._interpreter_host import InterpreterHost


def _host_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name.endswith("interpreter_host") and record.levelno == logging.WARNING
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workers", "starts", "on_worker", "warnings"),
    [
        (1, ["succeeds"], True, 0),
        # No worker can start: every object runs inline, reported once beside the failure.
        (1, ["fails"], False, 2),
        # A launch whose own start fails while another start is in flight waits
        # for that one instead of leaving the wait to `open` on the Event Loop.
        (2, ["succeeds", "fails-at-once"], True, 1),
    ],
    ids=["started", "start-failed", "failed-beside-another-start"],
)
async def test_objects_opened_while_the_first_worker_starts_wait_for_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    workers: int,
    starts: list[str],
    on_worker: bool,
    warnings: int,
) -> None:
    starting = threading.Event()
    gate = threading.Event()
    plan = iter(starts)
    plan_lock = threading.Lock()
    start_worker = _interpreter_host._InterpreterWorker

    def planned_start(*arguments: object) -> object:
        # The test decides how each start ends, and when the first one does.
        with plan_lock:
            outcome = next(plan)
        if outcome == "fails-at-once":
            raise RuntimeError("subinterpreters are unavailable")
        starting.set()
        gate.wait(5)
        if outcome == "fails":
            raise RuntimeError("subinterpreters are unavailable")
        return start_worker(*arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(_interpreter_host, "_InterpreterWorker", planned_start)
    host = InterpreterHost((), name="test", workers=workers)

    try:
        with (
            caplog.at_level(logging.WARNING),
            concurrent.futures.ThreadPoolExecutor(3) as pool,
        ):
            # As a command's launch: prepare off the Event Loop, then open on it.
            first = pool.submit(host.prepare)
            assert await asyncio.to_thread(starting.wait, 5)
            later = [pool.submit(host.prepare) for _ in range(2)]
            # Every start allowed is in flight: they wait for it, so `open` on
            # the Event Loop does not.
            done, _ = await asyncio.to_thread(concurrent.futures.wait, later, 0.1)
            assert not done
            gate.set()
            for prepared in (first, *later):
                prepared.result(5)
            objects = [host.open(SimpleNamespace, thread=threading.get_ident) for _ in range(3)]
        threads = {await hosted.call("thread") for hosted in objects}
    finally:
        host.close()

    here = threading.get_ident()
    if on_worker:
        assert len(threads) == 1 and here not in threads
    else:
        assert threads == {here}
    assert len(_host_warnings(caplog)) == warnings


@pytest.mark.asyncio
async def test_a_worker_that_does_not_start_in_time_counts_as_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    release = threading.Event()
    run = _interpreter_host._InterpreterWorker._run

    def hanging_run(worker: object, *arguments: object) -> None:
        # The start resolves only once the test releases it.
        release.wait(5)
        run(worker, *arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(_interpreter_host._InterpreterWorker, "_run", hanging_run)
    monkeypatch.setattr(_interpreter_host, "_WORKER_START_SECONDS", 0.0)
    host = InterpreterHost((), name="test", workers=1)
    try:
        with caplog.at_level(logging.WARNING):
            await asyncio.to_thread(host.prepare)
            hosted = host.open(SimpleNamespace, thread=threading.get_ident)
            assert await hosted.call("thread") == threading.get_ident()
        abandoned = [thread for thread in threading.enumerate() if thread.name == "vbot-test-0"]
    finally:
        release.set()
        host.close()

    # Treated as a failed start: inline, warned beside the failure.
    warnings = _host_warnings(caplog)
    assert len(warnings) == 2
    assert warnings[0].exc_info is not None
    assert isinstance(warnings[0].exc_info[1], TimeoutError)
    # The abandoned start's thread ends once its start does.
    assert len(abandoned) == 1
    abandoned[0].join(5)
    assert not abandoned[0].is_alive()
