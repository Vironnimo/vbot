"""Run terminal payloads, failure logging and usage."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from core.runs import ChatRunManager, Run, RunCancelledError, RunInterruptedError
from core.utils.errors import VBotError
from tests.core.runs.runs_test_support import SESSION, assert_timing_payload

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("mark", "extras", "payload"),
    [
        ("completed", {"usage": {"input_tokens": 150}}, {"usage": {"input_tokens": 150}}),
        ("completed", None, {}),
        ("completed", {}, {}),
        ("failed", {"detail": "extra"}, {"error": "oops", "detail": "extra"}),
        ("failed", None, {"error": "oops"}),
    ],
    ids=["completed-extras", "completed-none", "completed-empty", "failed-extras", "failed-none"],
)
async def test_marked_terminal_event_carries_only_provided_extras(
    mark: str, extras: dict[str, Any] | None, payload: dict[str, Any]
) -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    if mark == "completed":
        run.mark_completed("result", payload_extras=extras)
    else:
        run.mark_failed(RuntimeError("oops"), payload_extras=extras)

    [event] = [event for event in run.events if event.type == f"run_{mark}"]
    assert event.payload == {"status": mark, **payload}


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_executor_terminal_payload_extras_ride_the_terminal_event(outcome: str) -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        run.terminal_payload_extras["session_usage"] = {"input_tokens": 7}
        started.set()
        await release.wait()
        if outcome == "failed":
            raise VBotError("boom")
        return "done"

    run = await manager.start(SESSION, execute)
    await started.wait()
    if outcome == "cancelled":
        run.request_cancel()
    release.set()
    if outcome == "completed":
        assert await run.wait() == "done"
    else:
        with pytest.raises(VBotError if outcome == "failed" else RunCancelledError):
            await run.wait()

    [event] = [event for event in run.events if event.type == f"run_{outcome}"]
    assert event.payload["status"] == outcome
    assert event.payload["session_usage"] == {"input_tokens": 7}
    assert_timing_payload(event.payload)


async def test_manager_marks_interruption_terminal_and_wait_raises_signal() -> None:
    manager = ChatRunManager()
    partial = {"content": "partial"}

    async def execute(run: Run) -> None:
        run.terminal_payload_extras["session_usage"] = {"input_tokens": 4}
        raise RunInterruptedError("network", result=partial)

    run = await manager.start(SESSION, execute)

    with pytest.raises(RunInterruptedError) as exc_info:
        await run.wait()

    assert exc_info.value.cause == "network"
    assert exc_info.value.result == partial
    assert run.result == partial
    assert run.status.value == "interrupted"
    [event] = [event for event in run.events if event.type == "run_interrupted"]
    assert event.payload["status"] == "interrupted"
    assert event.payload["cause"] == "network"
    assert event.payload["session_usage"] == {"input_tokens": 4}
    assert_timing_payload(event.payload)


@pytest.mark.parametrize(
    ("error", "level", "message", "traceback"),
    [
        (RuntimeError("kaboom"), logging.ERROR, "failed unexpectedly", True),
        (VBotError("expected boom"), logging.WARNING, "expected boom", False),
    ],
    ids=["unexpected-error", "vbot-error"],
)
async def test_failed_run_logs_once_by_error_kind(
    caplog: pytest.LogCaptureFixture, error: Exception, level: int, message: str, traceback: bool
) -> None:
    async def fail(_run: Run) -> Any:
        raise error

    caplog.set_level(logging.WARNING, logger="vbot.runs")
    run = await ChatRunManager().start(SESSION, fail)
    with pytest.raises(type(error)):
        await run.wait()

    [record] = [record for record in caplog.records if record.name == "vbot.runs"]
    assert record.levelno == level
    assert run.id in record.getMessage()
    assert message in record.getMessage()
    assert (record.exc_info is not None and record.exc_info[1] is error) is traceback


class _ResultWithUsage:
    usage = {"input_tokens": 200, "output_tokens": 30}


class _ResultWithoutUsage:
    usage = None


@pytest.mark.parametrize(
    ("result", "usage"),
    [
        (_ResultWithUsage(), {"input_tokens": 200, "output_tokens": 30}),
        ("done", None),
        (_ResultWithoutUsage(), None),
    ],
    ids=["usage", "plain-result", "usage-none"],
)
async def test_run_completed_carries_the_result_usage_when_present(
    result: Any, usage: dict[str, int] | None
) -> None:
    async def execute(_run: Run) -> Any:
        return result

    run = await ChatRunManager().start(SESSION, execute)
    await run.wait()

    [event] = [event for event in run.events if event.type == "run_completed"]
    assert event.payload["status"] == "completed"
    assert event.payload.get("usage") == usage
    assert_timing_payload(event.payload)
