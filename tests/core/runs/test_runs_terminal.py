"""Run terminal payloads, failure logging and usage."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import weakref
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from core.providers.errors import ProviderOutcomeUnknownError
from core.runs import (
    ChatRunManager,
    Run,
    RunCancelledError,
    RunInterruptedError,
    RunStatus,
)
from core.sessions import SessionAddress
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


_UNEXPECTED = RuntimeError("kaboom")
_NETWORK_FAILURE = VBotError("connection reset")


@pytest.mark.parametrize(
    ("outcome", "level", "fields", "traceback"),
    [
        ("completed", logging.INFO, (), False),
        ("cancelled", logging.INFO, ("reason=user", "cancelled_by=rpc"), False),
        ("expected-failure", logging.WARNING, ("expected boom",), False),
        ("unexpected-failure", logging.ERROR, (), True),
        ("interrupted", logging.WARNING, ("cause=network", "connection reset"), False),
    ],
)
async def test_every_run_outcome_logs_one_terminal_line_at_its_level(
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    level: int,
    fields: tuple[str, ...],
    traceback: bool,
) -> None:
    started = asyncio.Event()

    async def execute(run: Run) -> str:
        run.model = "openrouter/test-model"
        run.internal = True
        run.iteration_count, run.tool_call_count = 2, 3
        run.retry_count, run.stream_recovery_count = 4, 1
        started.set()
        if outcome == "cancelled":
            await asyncio.Event().wait()
        if outcome == "expected-failure":
            raise VBotError("expected boom")
        if outcome == "unexpected-failure":
            raise _UNEXPECTED
        if outcome == "interrupted":
            raise RunInterruptedError("network") from _NETWORK_FAILURE
        return "done"

    caplog.set_level(logging.INFO, logger="vbot.runs")
    address = SessionAddress(project_id="acme", agent_id="coder", session_id="session-one")
    run = await ChatRunManager().start(address, execute)
    await started.wait()
    if outcome == "cancelled":
        run.request_cancel(reason="user", initiator="rpc")
    with contextlib.suppress(Exception):
        await run.wait()

    [record] = [record for record in caplog.records if record.name == "vbot.runs"]
    assert record.levelno == level
    message = record.getMessage()
    for field in (
        f"run={run.id}",
        "agent=coder",
        "session=session-one",
        "project=acme",
        "kind=user",
        "internal=true",
        "model=openrouter/test-model",
        "duration_ms=",
        "iterations=2",
        "tool_calls=3",
        "input_tokens=0",
        "output_tokens=0",
        "retries=4",
        "stream_recoveries=1",
        *fields,
    ):
        assert field in message
    assert (record.exc_info is not None and record.exc_info[1] is _UNEXPECTED) is traceback


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


class _Resource:
    def close(self) -> None:
        return None


class _KeywordOSError(OSError):
    __slots__ = ("detail",)

    def __init__(self, message: str, *, detail: object) -> None:
        super().__init__(13, message, "fixture")
        self.detail = detail


class _ValidationFixture(BaseModel):
    count: int


def _validation_error() -> ValidationError:
    try:
        _ValidationFixture.model_validate({"count": "bad"})
    except ValidationError as error:
        return error
    raise AssertionError("fixture unexpectedly validated")


class _UncopyableValidationError(ValidationError):
    def __copy__(self) -> _UncopyableValidationError:
        raise TypeError("fixture copy refused")


@pytest.mark.parametrize(
    "kind",
    [
        "provider",
        "os",
        "keyword-os",
        "unicode",
        "name",
        "group",
        "interrupted",
        "pydantic",
        "pydantic-cause",
    ],
)
async def test_retained_errors_preserve_semantics_without_execution_or_waiter_frames(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Logging's separate contract test captures the original traceback. Pytest's
    # retained LogRecords would themselves keep these execution frames alive.
    monkeypatch.setattr("core.runs.run._LOGGER.error", lambda *args, **kwargs: None)
    handles: list[weakref.ref[_Resource]] = []

    def failure() -> Exception:
        if kind == "provider":
            return ProviderOutcomeUnknownError("unavailable", operation_key="fixture")
        if kind == "os":
            return OSError(13, "denied", "fixture")
        if kind == "keyword-os":
            return _KeywordOSError("denied", detail=None)
        if kind == "unicode":
            return UnicodeDecodeError("utf8", b"x", 0, 1, "invalid fixture")
        if kind == "name":
            return NameError("missing", name="fixture_id")
        if kind == "group":
            return ExceptionGroup("fixture", [ValueError("nested")])
        if kind == "pydantic":
            return _validation_error()
        if kind == "pydantic-cause":
            return RuntimeError("validation failed")
        return RunInterruptedError("network", result={"content": "partial"})

    async def execute(_run: Run) -> None:
        resource = _Resource()
        handles.append(weakref.ref(resource))
        try:
            raise _validation_error() if kind == "pydantic-cause" else ValueError("cause")
        except ValueError as cause:
            error = failure()
            error.add_note("fixture note")
            # Cause/context graphs can contain cycles, including through a group.
            cause.__cause__ = error
            raise error from cause

    manager = ChatRunManager()
    run = await manager.start(SESSION, execute)
    expected = failure()
    for _ in range(2):
        with pytest.raises(type(expected)) as caught:
            await run.wait()
        assert type(caught.value) is type(expected)
        assert str(caught.value) == str(expected)
        assert caught.value.__dict__ == {**expected.__dict__, "__notes__": ["fixture note"]}
        for field in ("filename", "name", "detail", "encoding", "object", "start", "end"):
            if hasattr(expected, field):
                assert getattr(caught.value, field) == getattr(expected, field)
        assert caught.value is not run.error
        if isinstance(expected, ValidationError):
            assert isinstance(caught.value, ValidationError)
            assert caught.value.errors() == expected.errors()
        assert run.error is not None and run.error.__traceback__ is None
        cause = run.error.__cause__
        assert cause is not None and cause.__traceback__ is None
        assert cause.__cause__ is run.error
        assert run.error.__context__ is cause
        if kind == "pydantic-cause":
            assert isinstance(cause, ValidationError)
            assert cause.errors() == _validation_error().errors()
        if isinstance(run.error, BaseExceptionGroup):
            assert run.error.exceptions[0].__traceback__ is None
        del caught
    gc.collect()
    assert handles[0]() is None
    await manager.aclose()


@pytest.mark.parametrize("nested", [False, True], ids=["direct", "cause"])
async def test_uncloneable_error_cannot_prevent_terminal_settlement(
    nested: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.runs.run._LOGGER.error", lambda *args, **kwargs: None)

    async def execute(_run: Run) -> None:
        error = _UncopyableValidationError.from_exception_data(
            "fixture", [{"type": "int_parsing", "loc": ("count",), "input": "bad"}]
        )
        if nested:
            raise RuntimeError("validation failed") from error
        raise error

    manager = ChatRunManager()
    run = await manager.start(SESSION, execute)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(run.wait(), timeout=1)
    assert run.status == RunStatus.FAILED
    assert run.error is not None and run.error.__traceback__ is None
    fallback = run.error.__cause__ if nested else run.error
    assert isinstance(fallback, RuntimeError)
    assert fallback.__traceback__ is None
    assert "_UncopyableValidationError" in str(fallback)
    assert "count" in str(fallback)
    assert run.events[-1].type == "run_failed"

    async def succeed(_run: Run) -> str:
        return "done"

    next_run = await manager.start(SESSION, succeed)
    assert await next_run.wait() == "done"
    await manager.aclose()


def _register_resource(run: Run) -> weakref.ref[_Resource]:
    """Hand one resource to every Run callback registry; return only a weak handle."""
    resource = _Resource()

    async def observe(_status: RunStatus) -> None:
        resource.close()

    run.add_cancel_callback(resource.close)
    run.add_completion_finalizer(observe)
    run.add_completion_observer(observe)
    run.begin_tool_call("call-active")
    run.register_tool_cancel("call-active", resource.close)
    run.register_tool_background("call-active", lambda: resource is not None)
    run.register_tool_cancel("call-cancelled", resource.close)
    run.cancel_tool_call("call-cancelled")
    return weakref.ref(resource)


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_finished_run_releases_execution_callbacks(outcome: str) -> None:
    manager = ChatRunManager()
    handles: list[weakref.ref[_Resource]] = []
    registered = asyncio.Event()

    async def execute(run: Run) -> str:
        handles.append(_register_resource(run))
        registered.set()
        if outcome == "failed":
            raise RuntimeError("boom")
        if outcome == "cancelled":
            await asyncio.Event().wait()
        return "done"

    run = await manager.start(SESSION, execute)
    await registered.wait()
    if outcome == "cancelled":
        run.request_cancel()
    expected_error = {"failed": RuntimeError, "cancelled": RunCancelledError}.get(outcome)
    if expected_error is None:
        assert await run.wait() == "done"
    else:
        with pytest.raises(expected_error):
            await run.wait()
    # A registration after the terminal state is not retained either.
    handles.append(_register_resource(run))
    gc.collect()

    assert run.status == RunStatus(outcome)
    assert [handle() for handle in handles] == [None, None]
    # The per-call cancel outcome stays observable; nothing is left to cancel.
    assert run.tool_call_cancelled("call-cancelled")
    assert not run.cancel_tool_call("call-active")
    assert run.controls()["background_tool_call_ids"] == []
