"""Tools: execution behavior."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from core.tools import (
    ToolCall,
    ToolContext,
    ToolContractError,
    ToolExecutionConfig,
    ToolExecutor,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from tests.core.tools.tools_helpers import (
    READ_FILE_SCHEMA,
    JsonObject,
    make_execution_config,
    register_read_file,
)


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_exact_provider_contract_flows_to_dispatch_validation(self) -> None:
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
                    "properties": {
                        "path": {
                            "type": "string",
                            "enum": ["README.md"],
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            }
        ]
        contracts = registry.contracts_for_provider_definitions(definitions)
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="read_file", arguments={"path": "SECRET.md"})],
            replace(
                make_execution_config(allowed_tools=["read_file"]),
                input_contracts=contracts,
            ),
        )

        assert results[0]["error"]["code"] == "invalid_arguments"
        assert handler_calls == []
        with pytest.raises(ToolContractError):
            contracts["read_file"].validate_arguments({"path": "SECRET.md"})

    @pytest.mark.asyncio
    async def test_nesting_depth_flows_from_config_to_context(self) -> None:
        registry = ToolRegistry()
        seen_depths: list[int] = []

        def depth_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            seen_depths.append(context.nesting_depth)
            return tool_success({"nesting_depth": context.nesting_depth})

        registry.register(
            "depth",
            "Return the current nesting depth for testing.",
            {"type": "object"},
            depth_handler,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="depth", arguments={})],
            ToolExecutionConfig(
                agent_id="agent-1",
                session_id="session-1",
                run_id="run-1",
                workspace=Path("workspace"),
                vbot_root=Path("app"),
                data_root=Path("data"),
                allowed_tools=["*"],
                nesting_depth=3,
            ),
        )

        assert seen_depths == [3]
        assert results == [tool_success({"nesting_depth": 3})]

    @pytest.mark.asyncio
    async def test_cwd_flows_from_config_to_context(self) -> None:
        registry = ToolRegistry()
        seen_cwds: list[Path] = []

        def cwd_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            seen_cwds.append(context.effective_cwd)
            return tool_success({"cwd": str(context.effective_cwd)})

        registry.register(
            "cwd",
            "Return the effective working directory for testing.",
            {"type": "object"},
            cwd_handler,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="cwd", arguments={})],
            ToolExecutionConfig(
                agent_id="agent-1",
                session_id="session-1",
                run_id="run-1",
                workspace=Path("workspace"),
                vbot_root=Path("app"),
                data_root=Path("data"),
                cwd=Path("repo"),
                allowed_tools=["*"],
            ),
        )

        assert seen_cwds == [Path("repo")]
        assert results == [tool_success({"cwd": str(Path("repo"))})]

    @pytest.mark.asyncio
    async def test_cwd_defaults_to_workspace_when_config_has_none(self) -> None:
        registry = ToolRegistry()
        seen_cwds: list[Path] = []

        def cwd_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            seen_cwds.append(context.effective_cwd)
            return tool_success({"cwd": str(context.effective_cwd)})

        registry.register(
            "cwd_default",
            "Return the effective working directory for testing the fallback.",
            {"type": "object"},
            cwd_handler,
        )
        executor = ToolExecutor(registry)

        await executor.execute_many(
            [ToolCall(id="call-1", name="cwd_default", arguments={})],
            make_execution_config(allowed_tools=["*"], workspace=Path("workspace")),
        )

        # No project cwd in the config: tools resolve against the workspace,
        # preserving today's identity-agent behavior.
        assert seen_cwds == [Path("workspace")]

    @pytest.mark.asyncio
    async def test_cancel_hooks_flow_from_config_to_context_through_execute_one(self) -> None:
        registry = ToolRegistry()
        registered_callbacks: list[Callable[[], None]] = []
        user_cancelled = False

        def cancel_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            def cancel_callback() -> None:
                nonlocal user_cancelled
                user_cancelled = True

            context.on_cancel(cancel_callback)
            return tool_success({"was_cancelled": context.was_cancelled_by_user()})

        registry.register(
            "cancel_probe",
            "Probe cancel hooks wired through ToolExecutionConfig.",
            {"type": "object"},
            cancel_handler,
        )
        executor = ToolExecutor(registry)

        def registration_hook(callback: Callable[[], None]) -> None:
            registered_callbacks.append(callback)

        cancel_check_calls = 0

        def cancel_check_hook() -> bool:
            nonlocal cancel_check_calls
            cancel_check_calls += 1
            return True

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="cancel_probe", arguments={})],
            ToolExecutionConfig(
                agent_id="agent-1",
                session_id="session-1",
                run_id="run-1",
                workspace=Path("workspace"),
                vbot_root=Path("app"),
                data_root=Path("data"),
                allowed_tools=["*"],
                cancel_registration_hook=registration_hook,
                cancel_check_hook=cancel_check_hook,
            ),
        )

        assert len(registered_callbacks) == 1
        assert cancel_check_calls == 1
        assert results == [tool_success({"was_cancelled": True})]

        registered_callbacks[0]()
        assert user_cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_hooks_default_to_safe_noop_in_executor(self) -> None:
        registry = ToolRegistry()
        seen_values: dict[str, bool] = {}

        def cancel_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            context.on_cancel(lambda: None)
            seen_values["was_cancelled"] = context.was_cancelled_by_user()
            return tool_success({"ok": True})

        registry.register(
            "cancel_default",
            "Probe cancel hooks default to no-op when config has none.",
            {"type": "object"},
            cancel_handler,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="cancel_default", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )

        assert seen_values == {"was_cancelled": False}
        assert results == [tool_success({"ok": True})]

    @pytest.mark.asyncio
    async def test_unknown_tool_becomes_failed_result(self) -> None:
        executor = ToolExecutor(ToolRegistry())

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="missing_tool", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )

        assert results == [tool_failure("tool_not_found", "Tool not found: missing_tool")]

    @pytest.mark.asyncio
    async def test_disallowed_tool_becomes_failed_result(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="read_file", arguments={"path": "SOUL.md"})],
            make_execution_config(allowed_tools=[]),
        )

        assert results == [tool_failure("tool_not_allowed", "Tool not allowed: read_file")]

    @pytest.mark.asyncio
    async def test_invalid_arguments_become_failed_result(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="read_file", arguments=[])],
            make_execution_config(allowed_tools=["*"]),
        )

        assert results == [
            tool_failure(
                "invalid_arguments",
                "arguments: expected JSON object, received JSON array [type]",
            )
        ]

    @pytest.mark.asyncio
    async def test_argument_error_message_does_not_determine_failure_code(self) -> None:
        registry = ToolRegistry()

        def validating_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            raise ValueError("return_format must be a string")

        registry.register(
            "validating",
            "Validate arguments for testing.",
            {"type": "object"},
            validating_handler,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="validating", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )

        assert results == [tool_failure("invalid_arguments", "return_format must be a string")]

    @pytest.mark.asyncio
    async def test_handler_exception_becomes_failed_result(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = ToolRegistry()

        def failing_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            raise RuntimeError("boom")

        registry.register("failing", "Fail for testing.", {"type": "object"}, failing_handler)
        executor = ToolExecutor(registry)

        with caplog.at_level(logging.ERROR, logger="vbot.tools"):
            results = await executor.execute_many(
                [ToolCall(id="call-1", name="failing", arguments={})],
                make_execution_config(allowed_tools=["*"]),
            )

        assert results == [tool_failure("tool_execution_error", "boom")]
        crash_records = [
            record
            for record in caplog.records
            if record.levelno == logging.ERROR and "crashed unexpectedly" in record.getMessage()
        ]
        assert crash_records, "expected an error log for the crashing tool handler"
        assert crash_records[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_non_serializable_handler_result_becomes_failed_result(self) -> None:
        registry = ToolRegistry()

        registry.register(
            "broken_result",
            "Return a value that cannot enter Session JSON.",
            {"type": "object"},
            lambda _context, _arguments: tool_success({"path": Path("README.md")}),
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [ToolCall(id="call-1", name="broken_result", arguments={})],
            make_execution_config(allowed_tools=["*"]),
        )

        assert results[0]["ok"] is False
        assert results[0]["error"]["code"] == "invalid_tool_result"
        assert "not JSON-serializable" in results[0]["error"]["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("abort_kind", ["base_exception", "child_cancel", "parent_cancel"])
    async def test_parallel_abort_drains_siblings_before_propagating(self, abort_kind) -> None:
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
    async def test_parallel_execution_overlaps_and_preserves_order(self) -> None:
        registry = ToolRegistry()
        started: list[str] = []
        release_second = asyncio.Event()

        async def slow_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            started.append(context.tool_call_id)
            if context.tool_call_id == "call-1":
                await release_second.wait()
            else:
                release_second.set()
            return tool_success({"id": context.tool_call_id})

        registry.register(
            "slow",
            "Slow tool for testing.",
            {"type": "object"},
            slow_handler,
        )
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
            [
                ToolCall(id="call-1", name="slow", arguments={}),
                ToolCall(id="call-2", name="slow", arguments={}),
            ],
            make_execution_config(allowed_tools=["*"]),
        )

        assert started == ["call-1", "call-2"]
        assert results == [tool_success({"id": "call-1"}), tool_success({"id": "call-2"})]

    @pytest.mark.asyncio
    async def test_same_tool_can_run_multiple_times_in_parallel(self) -> None:
        registry = ToolRegistry()
        active_count = 0
        max_active_count = 0
        release = asyncio.Event()

        async def same_tool_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            nonlocal active_count, max_active_count
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            if max_active_count == 2:
                release.set()
            await release.wait()
            active_count -= 1
            return tool_success({"id": context.tool_call_id})

        registry.register(
            "same",
            "Same tool for testing.",
            {"type": "object"},
            same_tool_handler,
        )
        executor = ToolExecutor(registry, per_run_limit=2, global_limit=2)

        results = await executor.execute_many(
            [
                ToolCall(id="call-1", name="same", arguments={}),
                ToolCall(id="call-2", name="same", arguments={}),
            ],
            make_execution_config(allowed_tools=["*"]),
        )

        assert max_active_count == 2
        assert results == [tool_success({"id": "call-1"}), tool_success({"id": "call-2"})]

    @pytest.mark.asyncio
    async def test_serial_tool_is_a_barrier_between_parallel_safe_groups(self) -> None:
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
        executor = ToolExecutor(registry)

        results = await executor.execute_many(
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
    async def test_unknown_tool_does_not_split_parallel_safe_siblings(self) -> None:
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
    async def test_semaphore_queues_overflow_with_lowered_limits(self) -> None:
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
    async def test_global_limit_is_shared_across_executor_instances(self) -> None:
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
