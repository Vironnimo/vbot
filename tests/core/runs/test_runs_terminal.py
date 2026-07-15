"""Run terminal payload, failure logging, and usage tests."""

from __future__ import annotations

from .runs_test_support import (
    Any,
    ChatRunManager,
    Run,
    RunCancelledError,
    VBotError,
    assert_timing_payload,
    asyncio,
    logging,
    pytest,
)

pytestmark = pytest.mark.asyncio


async def test_mark_completed_includes_payload_extras_when_provided() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_completed(
        "result",
        payload_extras={"usage": {"input_tokens": 150, "output_tokens": 12}},
    )

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload == {
        "status": "completed",
        "usage": {"input_tokens": 150, "output_tokens": 12},
    }


async def test_executor_terminal_payload_extras_ride_completed_event() -> None:
    manager = ChatRunManager()

    async def execute(run: Run) -> str:
        run.terminal_payload_extras["session_usage"] = {"input_tokens": 12}
        return "done"

    run = await manager.start(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )
    assert await run.wait() == "done"

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload["session_usage"] == {"input_tokens": 12}
    assert_timing_payload(completed_events[0].payload)


async def test_executor_terminal_payload_extras_ride_failed_and_cancelled_events() -> None:
    manager = ChatRunManager()

    async def failing(run: Run) -> str:
        run.terminal_payload_extras["session_usage"] = {"input_tokens": 7}
        raise VBotError("boom")

    failed_run = await manager.start(
        agent_id="coder", session_id="session-fail", executor=failing, project_id=None
    )
    with pytest.raises(VBotError):
        await failed_run.wait()
    failed_events = [event for event in failed_run.events if event.type == "run_failed"]
    assert failed_events[0].payload["session_usage"] == {"input_tokens": 7}

    release = asyncio.Event()
    started = asyncio.Event()

    async def cancellable(run: Run) -> str:
        run.terminal_payload_extras["session_usage"] = {"input_tokens": 9}
        started.set()
        await release.wait()
        return "late"

    cancelled_run = await manager.start(
        agent_id="coder", session_id="session-cancel", executor=cancellable, project_id=None
    )
    await started.wait()
    cancelled_run.request_cancel()
    release.set()
    with pytest.raises(RunCancelledError):
        await cancelled_run.wait()
    cancelled_events = [event for event in cancelled_run.events if event.type == "run_cancelled"]
    assert cancelled_events[0].payload["session_usage"] == {"input_tokens": 9}


async def test_mark_completed_omits_payload_extras_when_not_provided() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_completed("result")

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload == {"status": "completed"}


async def test_mark_completed_omits_payload_extras_when_empty() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_completed("result", payload_extras=None)

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload == {"status": "completed"}


async def test_mark_failed_includes_payload_extras_when_provided() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_failed(RuntimeError("oops"), payload_extras={"detail": "extra"})

    failed_events = [event for event in run.events if event.type == "run_failed"]
    assert len(failed_events) == 1
    assert failed_events[0].payload == {
        "status": "failed",
        "error": "oops",
        "detail": "extra",
    }


async def test_mark_failed_omits_payload_extras_when_not_provided() -> None:
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    run.mark_failed(RuntimeError("oops"))

    failed_events = [event for event in run.events if event.type == "run_failed"]
    assert len(failed_events) == 1
    assert failed_events[0].payload == {"status": "failed", "error": "oops"}


async def test_mark_failed_logs_unexpected_error_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-VBotError failure logs at ERROR with the exception attached."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    caplog.set_level(logging.ERROR, logger="vbot.runs")
    run.mark_failed(RuntimeError("kaboom"))

    error_records = [
        record
        for record in caplog.records
        if record.name == "vbot.runs" and record.levelno == logging.ERROR
    ]
    assert len(error_records) == 1
    record = error_records[0]
    assert "failed unexpectedly" in record.getMessage()
    assert run.id in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)


async def test_mark_failed_logs_vbot_error_as_warning_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An expected VBotError failure logs at WARNING with no traceback."""
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")

    caplog.set_level(logging.WARNING, logger="vbot.runs")
    run.mark_failed(VBotError("expected boom"))

    warning_records = [
        record
        for record in caplog.records
        if record.name == "vbot.runs" and record.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    record = warning_records[0]
    assert "Run %s failed" not in record.getMessage()
    assert "expected boom" in record.getMessage()
    assert record.exc_info is None
    # No ERROR-level record for an expected failure.
    assert not [
        rec for rec in caplog.records if rec.name == "vbot.runs" and rec.levelno == logging.ERROR
    ]


async def test_failed_run_logs_once_through_manager_executor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An executor that raises reaches mark_failed exactly once and logs once."""
    manager = ChatRunManager()

    async def fail(_run: Run) -> Any:
        raise RuntimeError("executor boom")

    caplog.set_level(logging.ERROR, logger="vbot.runs")
    run = await manager.start(
        agent_id="coder", session_id="session-one", executor=fail, project_id=None
    )
    with pytest.raises(RuntimeError, match="executor boom"):
        await run.wait()

    error_records = [
        record
        for record in caplog.records
        if record.name == "vbot.runs"
        and record.levelno == logging.ERROR
        and "failed unexpectedly" in record.getMessage()
    ]
    assert len(error_records) == 1


async def test_run_completed_includes_usage_from_result_object() -> None:
    """Usage attribute on executor result appears in run_completed payload."""

    class FakeResult:
        usage = {"input_tokens": 200, "output_tokens": 30}

    manager = ChatRunManager()

    async def execute(run: Run) -> FakeResult:
        return FakeResult()

    run = await manager.start(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )
    await run.wait()

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert_timing_payload(completed_events[0].payload)
    assert completed_events[0].payload["status"] == "completed"
    assert completed_events[0].payload["usage"] == {
        "input_tokens": 200,
        "output_tokens": 30,
    }


async def test_run_completed_omits_usage_when_result_has_no_usage() -> None:
    """When the executor returns a plain string, no usage key appears in run_completed."""
    manager = ChatRunManager()

    async def execute(run: Run) -> str:
        return "done"

    run = await manager.start(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )
    await run.wait()

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert_timing_payload(completed_events[0].payload)
    assert completed_events[0].payload["status"] == "completed"
    assert "usage" not in completed_events[0].payload


async def test_run_completed_omits_usage_when_usage_is_none() -> None:
    """When the result has usage=None, no usage key appears in run_completed."""

    class ResultWithNoneUsage:
        usage = None

    manager = ChatRunManager()

    async def execute(run: Run) -> ResultWithNoneUsage:
        return ResultWithNoneUsage()

    run = await manager.start(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )
    await run.wait()

    completed_events = [event for event in run.events if event.type == "run_completed"]
    assert len(completed_events) == 1
    assert_timing_payload(completed_events[0].payload)
    assert completed_events[0].payload["status"] == "completed"
    assert "usage" not in completed_events[0].payload
