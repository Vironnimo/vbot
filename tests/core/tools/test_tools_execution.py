"""Tools: ToolExecutor calls, their ToolContext, failure envelopes and scheduling."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.tools import (
    ToolCall,
    ToolContext,
    ToolContractError,
    ToolExecutor,
    ToolRegistry,
    model_names,
    tool_failure,
    tool_success,
)
from tests.core.tools.tools_test_support import (
    READ_FILE_SCHEMA,
    JsonObject,
    make_execution_config,
    register_read_file,
)


async def _run_one(
    registry: ToolRegistry,
    name: str,
    arguments: Any = None,
    *,
    allowed_tools: list[str] | None = None,
    **config: Any,
) -> JsonObject:
    [result] = await ToolExecutor(registry).execute_many(
        [ToolCall(id="call-1", name=name, arguments={} if arguments is None else arguments)],
        make_execution_config(
            allowed_tools=["*"] if allowed_tools is None else allowed_tools, **config
        ),
    )
    return result


# --- The call's ToolContext ------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_context_carries_the_run_configuration(tmp_path: Path) -> None:
    registry = ToolRegistry()
    events: list[tuple[str, JsonObject]] = []
    notes: list[str] = []
    cancel_registrations: list[tuple[str, Callable[[], None]]] = []
    seen: JsonObject = {}

    async def emit(event_type: str, payload: JsonObject) -> None:
        events.append((event_type, payload))

    async def probe(context: ToolContext, arguments: JsonObject) -> JsonObject:
        await context.emit("tool_progress", {"step": 1})
        context.add_note("reminder")
        context.on_cancel(lambda: None)
        seen.update(
            nesting_depth=context.nesting_depth,
            effective_cwd=context.effective_cwd,
            resolved=context.resolve_path("src/main.py"),
            run_cancelled=context.is_cancelled(),
            call_cancelled=context.was_cancelled_by_user(),
        )
        return tool_success({})

    registry.register("probe", "Probe the call context.", {"type": "object"}, probe)

    result = await _run_one(
        registry,
        "probe",
        workspace=tmp_path / "workspace",
        cwd=tmp_path / "repo",
        nesting_depth=3,
        emit_hook=emit,
        note_hook=notes.append,
        cancellation_hook=lambda: True,
        tool_call_cancel_registrar=lambda call_id, callback: cancel_registrations.append(
            (call_id, callback)
        ),
        tool_call_cancel_check=lambda call_id: call_id == "call-1",
    )

    assert result == tool_success({})
    assert seen == {
        "nesting_depth": 3,
        "effective_cwd": tmp_path / "repo",
        "resolved": (tmp_path / "repo" / "src" / "main.py").resolve(),
        "run_cancelled": True,
        "call_cancelled": True,
    }
    assert events == [("tool_progress", {"step": 1})]
    assert notes == ["reminder"]
    assert [call_id for call_id, _callback in cancel_registrations] == ["call-1"]


@pytest.mark.asyncio
async def test_handler_context_without_run_hooks_uses_the_workspace_and_ignores_hooks() -> None:
    registry = ToolRegistry()
    seen: JsonObject = {}

    async def probe(context: ToolContext, arguments: JsonObject) -> JsonObject:
        await context.emit("tool_progress", {"step": 1})
        context.add_note("reminder")
        context.on_cancel(lambda: None)
        seen.update(
            nesting_depth=context.nesting_depth,
            effective_cwd=context.effective_cwd,
            run_cancelled=context.is_cancelled(),
            call_cancelled=context.was_cancelled_by_user(),
        )
        return tool_success({})

    registry.register("probe", "Probe the call context.", {"type": "object"}, probe)

    assert await _run_one(registry, "probe") == tool_success({})
    assert seen == {
        "nesting_depth": 0,
        "effective_cwd": Path("workspace"),
        "run_cancelled": False,
        "call_cancelled": False,
    }


# --- Calls that never reach a handler -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "allowed_tools", "model_name", "expected"),
    [
        pytest.param(
            "missing_tool",
            ["read_file", "hidden"],
            None,
            tool_failure(
                "tool_not_found",
                "Unknown Tool: missing_tool. Call one of the available Tools instead: read_file.",
            ),
            id="unknown-names-offered-tools",
        ),
        pytest.param(
            "missing_tool",
            [],
            None,
            tool_failure(
                "tool_not_found", "Unknown Tool: missing_tool. No Tools are available in this Run."
            ),
            id="unknown-without-offered-tools",
        ),
        pytest.param(
            "read_file",
            [],
            None,
            tool_failure("tool_not_allowed", "Tool not allowed: read_file"),
            id="not-allowed",
        ),
        pytest.param(
            "read_file",
            [],
            "host_read",
            tool_failure("tool_not_allowed", "Tool not allowed: host_read"),
            id="not-allowed-under-its-model-name",
        ),
    ],
)
async def test_unknown_or_disallowed_tool_becomes_a_failed_result(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    allowed_tools: list[str],
    model_name: str | None,
    expected: JsonObject,
) -> None:
    if model_name is not None:
        monkeypatch.setattr(model_names, "_MODEL_NAMES", {"read_file": model_name})
    registry = ToolRegistry()
    register_read_file(registry)
    registry.register(
        "hidden", "Deferred Tool.", {"type": "object"}, lambda _c, _a: {}, deferred=True
    )
    registry.register("denied", "Not allowed.", {"type": "object"}, lambda _c, _a: {})

    result = await _run_one(registry, name, {"path": "SOUL.md"}, allowed_tools=allowed_tools)

    assert result == expected


@pytest.mark.asyncio
async def test_session_scoped_dispatch_checks_grant_before_allowlist() -> None:
    registry = ToolRegistry()
    registry.register(
        name="history",
        description="Read this Session's earlier original records.",
        parameters={"type": "object"},
        handler=lambda _context, _arguments: tool_success({}),
        session_scoped=True,
    )

    unavailable = await _run_one(registry, "history", allowed_tools=[])
    denied = await _run_one(registry, "history", allowed_tools=[], session_tool_grants=("history",))
    granted = await _run_one(
        registry, "history", allowed_tools=["history"], session_tool_grants=("history",)
    )

    assert unavailable["error"]["code"] == "history_unavailable"
    assert denied["error"]["code"] == "tool_not_allowed"
    assert granted["ok"] is True


@pytest.mark.asyncio
async def test_non_object_arguments_become_a_failed_result() -> None:
    registry = ToolRegistry()
    register_read_file(registry)

    result = await _run_one(registry, "read_file", [])

    assert result == tool_failure(
        "invalid_arguments",
        "read_file was not run:\n"
        "- Arguments must be a JSON object of named parameters; received an array.\n"
        "read_file parameters: path (required).",
    )


@pytest.mark.asyncio
async def test_exact_provider_contract_flows_to_dispatch_validation() -> None:
    registry = ToolRegistry()
    handler_calls: list[JsonObject] = []

    def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        handler_calls.append(arguments)
        return tool_success({})

    registry.register(
        "read_file",
        "Read a UTF-8 text file from the workspace.",
        READ_FILE_SCHEMA,
        handler,
    )
    definitions = [
        {
            "name": "read_file",
            "description": "Read only the public README.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "enum": ["README.md"]}},
                "required": ["path"],
                "additionalProperties": False,
            },
        }
    ]
    contracts = registry.contracts_for_provider_definitions(definitions)

    result = await _run_one(
        registry,
        "read_file",
        {"path": "SECRET.md"},
        allowed_tools=["read_file"],
        input_contracts=contracts,
    )

    assert result["error"]["code"] == "invalid_arguments"
    assert handler_calls == []
    with pytest.raises(ToolContractError):
        contracts["read_file"].validate_arguments({"path": "SECRET.md"})


# --- Handler outcomes -----------------------------------------------------------


def _raise_argument_error(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
    raise ValueError("return_format must be a string")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "result_schema", "code", "message"),
    [
        pytest.param(
            _raise_argument_error,
            None,
            "invalid_arguments",
            "return_format must be a string",
            id="handler-argument-error",
        ),
        pytest.param(
            lambda _context, _arguments: {"content": "not enveloped"},
            None,
            "invalid_tool_result",
            "envelope",
            id="not-an-envelope",
        ),
        pytest.param(
            lambda _context, _arguments: tool_success({"path": Path("README.md")}),
            None,
            "invalid_tool_result",
            "not JSON-serializable",
            id="not-serializable",
        ),
        pytest.param(
            lambda _context, _arguments: tool_success({"wrong": True}),
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            "invalid_tool_result",
            "value",
            id="violates-result-schema",
        ),
    ],
)
async def test_unusable_handler_outcome_becomes_a_failed_result(
    handler: Callable[[ToolContext, JsonObject], JsonObject],
    result_schema: JsonObject | None,
    code: str,
    message: str,
) -> None:
    registry = ToolRegistry()
    registry.register(
        "probe", "Probe handler outcomes.", {"type": "object"}, handler, result_schema=result_schema
    )

    result = await _run_one(registry, "probe")

    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert message in result["error"]["message"]


@pytest.mark.asyncio
async def test_handler_crash_becomes_failed_result_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ToolRegistry()

    def failing_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        raise RuntimeError("boom")

    registry.register("failing", "Fail for testing.", {"type": "object"}, failing_handler)

    with caplog.at_level(logging.ERROR, logger="vbot.tools"):
        result = await _run_one(registry, "failing")

    assert result == tool_failure("tool_execution_error", "boom")
    crash_records = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and "crashed unexpectedly" in record.getMessage()
    ]
    assert crash_records, "expected an error log for the crashing tool handler"
    assert crash_records[0].exc_info is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_change", ["remove", "replace"])
@pytest.mark.parametrize("valid_result", [False, True])
async def test_running_call_keeps_its_result_contract(
    catalog_change: str, valid_result: bool
) -> None:
    registry = ToolRegistry()
    started = asyncio.Event()
    resume = asyncio.Event()
    effects: list[str] = []
    result = tool_success({"value": "completed" if valid_result else 1})

    async def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        started.set()
        await resume.wait()
        effects.append("completed")
        return result

    original = registry.register(
        "dynamic",
        "Test Tool.",
        {"type": "object"},
        handler,
        result_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    task = asyncio.create_task(_run_one(registry, "dynamic", allowed_tools=["dynamic"]))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        candidates = []
        if catalog_change == "replace":
            candidates.append(
                ToolRegistry().register(
                    "dynamic",
                    "Replacement Tool.",
                    {"type": "object"},
                    lambda _context, _arguments: tool_success({"value": 2}),
                    result_schema={
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                )
            )
        registry.replace_owned_tools([original], candidates)
    finally:
        resume.set()
        completed = await task

    assert effects == ["completed"]
    if valid_result:
        assert completed == result
    else:
        assert completed["error"]["code"] == "invalid_tool_result"


# --- Scheduling -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("abort_kind", ["base_exception", "child_cancel", "parent_cancel"])
async def test_parallel_abort_drains_siblings_before_propagating(abort_kind: str) -> None:
    class ToolAbort(BaseException):
        pass

    registry = ToolRegistry()
    sibling_started = asyncio.Event()
    sibling_settled = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    abort = asyncio.CancelledError() if abort_kind != "base_exception" else ToolAbort()
    side_effects = []

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        if context.tool_call_id == "abort":
            await sibling_started.wait()
            if abort_kind == "parent_cancel":
                await asyncio.Event().wait()
            raise abort
        sibling_started.set()
        try:
            await asyncio.Event().wait()
            side_effects.append("unexpected")
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            sibling_settled.set()
        return tool_success({})

    registry.register("abort_probe", "Test probe.", {"type": "object"}, handler)
    task = asyncio.create_task(
        ToolExecutor(registry).execute_many(
            [
                ToolCall(id="abort", name="abort_probe", arguments={}),
                ToolCall(id="sibling", name="abort_probe", arguments={}),
            ],
            make_execution_config(allowed_tools=["*"]),
        )
    )
    await asyncio.wait_for(sibling_started.wait(), timeout=1)
    if abort_kind == "parent_cancel":
        task.cancel()
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not task.done()
    finally:
        cleanup_release.set()
    with pytest.raises(type(abort)):
        await asyncio.wait_for(task, timeout=1)
    assert sibling_settled.is_set()
    assert side_effects == []


@pytest.mark.asyncio
async def test_calls_of_one_tool_overlap_and_keep_their_order() -> None:
    registry = ToolRegistry()
    started: list[str] = []
    release_first = asyncio.Event()

    async def slow_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        started.append(context.tool_call_id)
        if context.tool_call_id == "call-1":
            # Completes only once call-2 runs, so both calls must be in flight together.
            await release_first.wait()
        else:
            release_first.set()
        return tool_success({"id": context.tool_call_id})

    registry.register("slow", "Slow tool for testing.", {"type": "object"}, slow_handler)

    results = await ToolExecutor(registry).execute_many(
        [
            ToolCall(id="call-1", name="slow", arguments={}),
            ToolCall(id="call-2", name="slow", arguments={}),
        ],
        make_execution_config(allowed_tools=["*"]),
    )

    assert started == ["call-1", "call-2"]
    assert results == [tool_success({"id": "call-1"}), tool_success({"id": "call-2"})]


@pytest.mark.asyncio
async def test_serial_tool_is_a_barrier_between_parallel_safe_groups() -> None:
    registry = ToolRegistry()
    events: list[str] = []
    active_safe_calls = 0

    async def safe_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal active_safe_calls
        active_safe_calls += 1
        events.append(f"start:{context.tool_call_id}")
        await asyncio.sleep(0.01)
        events.append(f"end:{context.tool_call_id}")
        active_safe_calls -= 1
        return tool_success({"id": context.tool_call_id})

    async def serial_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        assert active_safe_calls == 0
        events.append(f"start:{context.tool_call_id}")
        await asyncio.sleep(0)
        events.append(f"end:{context.tool_call_id}")
        return tool_success({"id": context.tool_call_id})

    registry.register(
        "safe",
        "Parallel-safe tool for testing.",
        {"type": "object"},
        safe_handler,
        parallel_safe=True,
    )
    registry.register(
        "serial",
        "Serial tool for testing.",
        {"type": "object"},
        serial_handler,
        parallel_safe=False,
    )

    results = await ToolExecutor(registry).execute_many(
        [
            ToolCall(id="safe-1", name="safe", arguments={}),
            ToolCall(id="safe-2", name="safe", arguments={}),
            ToolCall(id="serial", name="serial", arguments={}),
            ToolCall(id="safe-3", name="safe", arguments={}),
            ToolCall(id="safe-4", name="safe", arguments={}),
        ],
        make_execution_config(allowed_tools=["*"]),
    )

    assert events.index("end:safe-1") < events.index("start:serial")
    assert events.index("end:safe-2") < events.index("start:serial")
    assert events.index("end:serial") < events.index("start:safe-3")
    assert events.index("end:serial") < events.index("start:safe-4")
    assert [result["data"]["id"] for result in results] == [
        "safe-1",
        "safe-2",
        "serial",
        "safe-3",
        "safe-4",
    ]


@pytest.mark.asyncio
async def test_unknown_tool_does_not_split_parallel_safe_siblings() -> None:
    registry = ToolRegistry()
    active_count = 0
    max_active_count = 0
    both_safe_calls_started = asyncio.Event()

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal active_count, max_active_count
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        if active_count == 2:
            both_safe_calls_started.set()
        try:
            await asyncio.wait_for(both_safe_calls_started.wait(), timeout=1)
            return tool_success({"id": context.tool_call_id})
        finally:
            active_count -= 1

    registry.register(
        "safe",
        "Parallel-safe tool for testing.",
        {"type": "object"},
        handler,
        parallel_safe=True,
    )
    executor = ToolExecutor(registry, per_run_limit=3, global_limit=3)

    results = await executor.execute_many(
        [
            ToolCall(id="call-1", name="safe", arguments={}),
            ToolCall(id="call-unknown", name="missing", arguments={}),
            ToolCall(id="call-2", name="safe", arguments={}),
        ],
        make_execution_config(allowed_tools=["*"]),
    )

    assert max_active_count == 2
    assert results[0] == tool_success({"id": "call-1"})
    assert results[1]["error"]["code"] == "tool_not_found"
    assert results[2] == tool_success({"id": "call-2"})


@pytest.mark.asyncio
async def test_semaphore_queues_overflow_with_lowered_limits() -> None:
    registry = ToolRegistry()
    active_count = 0
    max_active_count = 0

    async def queued_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal active_count, max_active_count
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        await asyncio.sleep(0.01)
        active_count -= 1
        return tool_success({"id": context.tool_call_id})

    registry.register("queued", "Queued tool for testing.", {"type": "object"}, queued_handler)
    executor = ToolExecutor(registry, per_run_limit=1, global_limit=1)

    results = await executor.execute_many(
        [
            ToolCall(id="call-1", name="queued", arguments={}),
            ToolCall(id="call-2", name="queued", arguments={}),
            ToolCall(id="call-3", name="queued", arguments={}),
        ],
        make_execution_config(allowed_tools=["*"]),
    )

    assert max_active_count == 1
    assert results == [
        tool_success({"id": "call-1"}),
        tool_success({"id": "call-2"}),
        tool_success({"id": "call-3"}),
    ]


@pytest.mark.asyncio
async def test_global_limit_is_shared_across_executor_instances() -> None:
    registry = ToolRegistry()
    active_count = 0
    max_active_count = 0
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def globally_limited_handler(
        context: ToolContext,
        arguments: JsonObject,
    ) -> JsonObject:
        nonlocal active_count, max_active_count
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        if context.tool_call_id == "call-1":
            first_started.set()
        await release.wait()
        active_count -= 1
        return tool_success({"id": context.tool_call_id})

    registry.register(
        "global_limit",
        "Globally limited tool for testing.",
        {"type": "object"},
        globally_limited_handler,
    )
    first_executor = ToolExecutor(registry, per_run_limit=1, global_limit=1)
    second_executor = ToolExecutor(registry, per_run_limit=1, global_limit=1)

    first_task = asyncio.create_task(
        first_executor.execute_many(
            [ToolCall(id="call-1", name="global_limit", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )
    )
    await first_started.wait()
    second_task = asyncio.create_task(
        second_executor.execute_many(
            [ToolCall(id="call-2", name="global_limit", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )
    )
    await asyncio.sleep(0.01)

    assert max_active_count == 1

    release.set()
    assert await first_task == [tool_success({"id": "call-1"})]
    assert await second_task == [tool_success({"id": "call-2"})]
    assert max_active_count == 1


@pytest.mark.asyncio
async def test_coordinator_wait_does_not_hold_child_execution_capacity() -> None:
    registry = ToolRegistry()
    children: list[str] = []
    config = make_execution_config(allowed_tools=["*"])
    executor = ToolExecutor(registry, global_limit=1)

    async def coordinator(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        return (
            await executor.execute_many([ToolCall(id="child", name="leaf", arguments={})], config)
        )[0]

    def leaf(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        children.append(context.tool_call_id)
        return tool_success({})

    registry.register(
        "coordinator",
        "Wait for child",
        {"type": "object"},
        coordinator,
        execution_slot_required=False,
    )
    registry.register("leaf", "Child work", {"type": "object"}, leaf)
    results = await asyncio.wait_for(
        executor.execute_many([ToolCall(id="parent", name="coordinator", arguments={})], config), 2
    )
    assert children == ["child"]
    assert results == [tool_success({})]


@pytest.mark.asyncio
async def test_default_capacity_runs_500_tools_before_queueing_overflow() -> None:
    registry = ToolRegistry()
    admitted = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal count
        count += 1
        if count == 500:
            admitted.set()
        await release.wait()
        return tool_success({})

    registry.register("capacity", "Capacity test", {"type": "object"}, handler)
    executor = ToolExecutor(registry)
    task = asyncio.create_task(
        executor.execute_many(
            [ToolCall(id=f"call-{index}", name="capacity", arguments={}) for index in range(501)],
            make_execution_config(allowed_tools=["*"]),
        )
    )
    try:
        await asyncio.wait_for(admitted.wait(), 3)
        await asyncio.sleep(0)
        assert count == 500
    finally:
        release.set()
        await task
    assert count == 501
