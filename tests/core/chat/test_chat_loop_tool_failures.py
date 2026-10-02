"""Failed, rejected and blocked Tool Calls in the chat loop, and no-Tool finalization."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat._step_outcomes import (
    MAX_IDENTICAL_FAILED_TOOL_CALLS,
    TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
    TOOL_ITERATION_LIMIT_FAILURE_CODE,
    _FailedToolCallCircuitBreaker,
    tool_result_facts,
)
from core.chat.messages import ToolCall, ToolCallRejection
from core.runs import TOOL_CALL_RESULT_EVENT, RunStatus
from core.sessions import ToolResultFacts
from core.tools import ToolContext, ToolContractError, ToolRegistry, tool_failure, tool_success
from core.utils.errors import ProviderError
from tests.core.chat.chat_loop_support import build_chat_loop, history, last_run, persisted_roles
from tests.core.chat.chat_loop_tools_test_support import (
    JsonObject,
    final,
    tool_results,
    tool_runtime,
    tool_turn,
)

_VALUE_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}
_VALID = {"value": "valid"}


def _counting_tool(
    invocations: list[JsonObject], *, result: Any = None, parameters: JsonObject | None = None
) -> ToolRegistry:
    def handler(_context: ToolContext, arguments: JsonObject) -> Any:
        invocations.append(arguments)
        if isinstance(result, Exception):
            raise result
        return tool_success({"value": arguments.get("value")}) if result is None else result

    tools = ToolRegistry()
    tools.register("probe", "Probe Tool.", parameters or {"type": "object"}, handler)
    return tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed_tools", "result", "arguments", "failure", "ran"),
    [
        ([], None, _VALID, tool_failure("tool_not_allowed", "Tool not allowed: probe."), False),
        (
            None,
            ValueError("extension change is unavailable"),
            _VALID,
            tool_failure(
                "tool_execution_error",
                "probe failed while running: extension change is unavailable. It is unknown "
                "how much of the call took effect. Check the current state before you call "
                "probe again.",
            ),
            True,
        ),
        (
            None,
            TimeoutError(),
            _VALID,
            tool_failure(
                "tool_execution_error",
                "probe failed while running: TimeoutError. It is unknown how much of the call "
                "took effect. Check the current state before you call probe again.",
            ),
            True,
        ),
        (
            None,
            ToolContractError("probe was not run: value names no target."),
            _VALID,
            tool_failure("invalid_arguments", "probe was not run: value names no target."),
            True,
        ),
        (
            None,
            {"content": "not enveloped"},
            _VALID,
            tool_failure(
                "invalid_tool_result", "Tool handler must return a valid result envelope: probe"
            ),
            True,
        ),
        (None, None, {"value": {"unknown": "target"}}, "invalid_arguments", False),
    ],
    ids=[
        "not-allowed",
        "handler-exception",
        "handler-exception-without-message",
        "handler-refusal",
        "non-envelope-result",
        "invalid-arguments",
    ],
)
async def test_failed_tool_call_persists_its_failure_and_the_run_continues(
    tmp_path: Path,
    allowed_tools: list[str] | None,
    result: Any,
    arguments: JsonObject,
    failure: JsonObject | str,
    ran: bool,
) -> None:
    invocations: list[JsonObject] = []
    runtime = tool_runtime(
        tmp_path,
        _counting_tool(invocations, result=result, parameters=_VALUE_SCHEMA),
        [tool_turn(("call_1", "probe", arguments)), final("Recovered")],
        allowed_tools=allowed_tools,
    )

    assistant = await build_chat_loop(runtime).send("coder", "Run it", session_id="session-one")

    messages = history(runtime)
    run = last_run(runtime)
    [persisted] = tool_results(messages)
    assert assistant.content == "Recovered"
    assert run.status == RunStatus.COMPLETED
    assert persisted_roles(messages) == ["user", "assistant", "tool", "assistant"]
    assert (len(invocations) == 1) is ran
    if isinstance(failure, str):
        assert persisted["error"]["code"] == failure
        assert '"value" must be a string' in persisted["error"]["message"]
    else:
        assert persisted == failure
    [event] = [
        event
        for event in await runtime.timelines.events(run)
        if event.type == TOOL_CALL_RESULT_EVENT
    ]
    assert event.payload["tool_call"] == {"id": "call_1", "index": 0, "name": "probe"}
    assert event.payload["result"] == persisted
    assert event.payload["timing"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_output_truncated_tool_calls_persist_failures_without_handler_side_effects(
    tmp_path: Path,
) -> None:
    invocations: list[JsonObject] = []
    runtime = tool_runtime(
        tmp_path,
        _counting_tool(invocations),
        [
            tool_turn(
                ("call_first", "probe", {"value": "first"}),
                ("call_second", "probe", {"value": "second"}),
                content="I started preparing both calls.",
                terminal_outcome="output_truncated",
            ),
            final("I will reissue complete calls if needed."),
        ],
    )

    assistant = await build_chat_loop(runtime).send("coder", "Run both", session_id="session-one")

    messages = history(runtime)
    assert assistant.content == "I will reissue complete calls if needed."
    assert invocations == []
    assert messages[1].content == "I started preparing both calls."
    assert persisted_roles(messages) == ["user", "assistant", "tool", "tool", "assistant"]
    assert [result["error"]["code"] for result in tool_results(messages)] == [
        "tool_call_truncated",
        "tool_call_truncated",
    ]
    assert [m["tool_call_id"] for m in runtime.adapter.requests[1]["messages"][-2:]] == [
        "call_first",
        "call_second",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_outcome", ["content_filtered", "stop"])
async def test_unsafe_terminal_outcome_fails_closed_before_tool_handler(
    tmp_path: Path,
    terminal_outcome: str,
) -> None:
    invocations: list[JsonObject] = []
    runtime = tool_runtime(
        tmp_path,
        _counting_tool(invocations),
        [
            tool_turn(
                ("call_probe", "probe"), content="unsafe partial", terminal_outcome=terminal_outcome
            )
        ],
    )

    with pytest.raises(ProviderError, match="unsafe terminal outcome"):
        await build_chat_loop(runtime).send("coder", "Run probe", session_id="session-one")

    messages = history(runtime)
    assert invocations == []
    assert persisted_roles(messages) == ["user", "assistant", "tool", "error"]
    assert tool_results(messages)[0]["error"]["code"] == "tool_call_rejected"


@pytest.mark.asyncio
async def test_malformed_non_streaming_call_fails_without_blocking_valid_sibling(
    tmp_path: Path,
) -> None:
    invocations: list[JsonObject] = []
    runtime = tool_runtime(
        tmp_path,
        _counting_tool(invocations, parameters=_VALUE_SCHEMA),
        [
            tool_turn(("call_bad", "probe", "{not json"), ("call_ok", "probe", {"value": "kept"})),
            final("Recovered after the rejected sibling."),
        ],
    )

    result = await build_chat_loop(runtime).send("coder", "Run both", session_id="session-one")

    messages = history(runtime)
    assert result.content == "Recovered after the rejected sibling."
    assert invocations == [{"value": "kept"}]
    assert persisted_roles(messages) == ["user", "assistant", "tool", "tool", "assistant"]
    assert messages[1].tool_calls is not None
    assert [call.rejection is not None for call in messages[1].tool_calls] == [True, False]
    rejected, kept = tool_results(messages)
    assert rejected["error"]["code"] == "malformed_tool_arguments"
    assert kept == tool_success({"value": "kept"})
    assert last_run(runtime).status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_malformed_tool_calls_container_becomes_rejected_call(tmp_path: Path) -> None:
    runtime = tool_runtime(
        tmp_path,
        None,
        [
            {"content": None, "tool_calls": "broken-container"},
            final("Recovered from the malformed Tool Call container."),
        ],
    )

    result = await build_chat_loop(runtime).send("coder", "Try it", session_id="session-one")

    messages = history(runtime)
    assert result.content == "Recovered from the malformed Tool Call container."
    assert persisted_roles(messages) == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls is not None
    [rejected] = messages[1].tool_calls
    assert rejected.name == "invalid_tool_call"
    assert rejected.rejection is not None and rejected.rejection.code == "malformed_tool_call"
    assert tool_results(messages)[0]["error"]["code"] == "malformed_tool_call"


@pytest.mark.asyncio
async def test_iteration_limit_finalizes_without_tools_and_rejects_further_calls(
    tmp_path: Path,
) -> None:
    invocations: list[JsonObject] = []
    runtime = tool_runtime(
        tmp_path,
        _counting_tool(invocations),
        [
            tool_turn(("call_first", "probe")),
            tool_turn(("call_ignored", "probe")),
            final("I received the disabled-Tool Result and will answer without Tools."),
        ],
    )

    result = await build_chat_loop(runtime, max_tool_iterations=0).send(
        "coder", "Probe?", session_id="session-one"
    )

    messages = history(runtime)
    assert result.content == "I received the disabled-Tool Result and will answer without Tools."
    assert invocations == []
    requests = runtime.adapter.requests
    assert len(requests) == 3
    assert all(request["kwargs"]["tools"] == [] for request in requests[1:])
    assert persisted_roles(messages) == [
        "user",
        "assistant",
        "tool",
        "note",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [result["error"]["code"] for result in tool_results(messages)] == [
        TOOL_ITERATION_LIMIT_FAILURE_CODE,
        TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
    ]
    assert last_run(runtime).status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_repeated_no_tool_finalization_violations_complete_without_unbounded_loop(
    tmp_path: Path,
) -> None:
    runtime = tool_runtime(
        tmp_path,
        _counting_tool([]),
        [
            tool_turn(("call_limit", "probe")),
            tool_turn(("call_violation_1", "probe")),
            tool_turn(("call_violation_2", "probe")),
        ],
    )

    result = await build_chat_loop(runtime, max_tool_iterations=0).send(
        "coder", "Probe?", session_id="session-one"
    )

    assert result.tool_calls is not None
    assert result.tool_calls[0].id == "call_violation_2"
    requests = runtime.adapter.requests
    assert len(requests) == 3
    assert all(request["kwargs"]["tools"] == [] for request in requests[1:])
    assert [result["error"]["code"] for result in tool_results(history(runtime))][1:] == [
        TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
        TOOL_FINALIZATION_DISABLED_FAILURE_CODE,
    ]
    assert last_run(runtime).status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_identical_failed_tool_call_finalizes_without_tools_after_the_limit(
    tmp_path: Path,
) -> None:
    invocations: list[JsonObject] = []
    failing = tool_failure("invalid_arguments", "name is required")
    runtime = tool_runtime(
        tmp_path,
        _counting_tool(invocations, result=failing),
        [
            tool_turn((f"call_{index}", "probe", {"action": "create", "name": "demo"}))
            for index in range(MAX_IDENTICAL_FAILED_TOOL_CALLS)
        ]
        + [final("The Tool remains blocked; I cannot complete it.")],
    )

    result = await build_chat_loop(runtime).send("coder", "Create it", session_id="session-one")

    assert result.content == "The Tool remains blocked; I cannot complete it."
    assert len(invocations) == MAX_IDENTICAL_FAILED_TOOL_CALLS
    assert len(runtime.adapter.requests) == MAX_IDENTICAL_FAILED_TOOL_CALLS + 1
    assert runtime.adapter.requests[-1]["kwargs"]["tools"] == []
    assert persisted_roles(history(runtime)) == [
        "user",
        *(["assistant", "tool"] * MAX_IDENTICAL_FAILED_TOOL_CALLS),
        "note",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_tool_iteration_limit_is_scoped_to_current_run(tmp_path: Path) -> None:
    runtime = tool_runtime(
        tmp_path,
        _counting_tool([]),
        [
            tool_turn(("call_1", "probe")),
            tool_turn(("call_2", "probe")),
            final("First run done"),
            tool_turn(("call_3", "probe")),
            tool_turn(("call_4", "probe")),
            final("Second run done"),
        ],
    )
    chat_loop = build_chat_loop(runtime, max_tool_iterations=2)

    first = await chat_loop.send("coder", "Batch one", session_id="session-one")
    second = await chat_loop.send("coder", "Batch two", session_id="session-one")

    assert (first.content, second.content) == ("First run done", "Second run done")
    run_roles = ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert persisted_roles(history(runtime)) == run_roles * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loop_limit", "run_limit"),
    [(1000, 1), (1, 5)],
    ids=["narrows-the-loop-limit", "never-raises-the-loop-limit"],
)
async def test_run_tool_iteration_limit_narrows_the_loop_limit(
    tmp_path: Path, loop_limit: int, run_limit: int
) -> None:
    invocations: list[JsonObject] = []
    runtime = tool_runtime(
        tmp_path,
        _counting_tool(invocations),
        [tool_turn(("call_1", "probe")), tool_turn(("call_2", "probe")), final("Done")],
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime, max_tool_iterations=loop_limit)

    run = await loop.start_run(
        "coder", "Probe twice", session_id="session-one", max_tool_iterations=run_limit
    )
    await run.wait()

    # One dispatched iteration; the second call gets the ordinary limit failure.
    assert len(invocations) == 1
    rejected = tool_results(history(runtime))[1]["error"]
    assert rejected["code"] == TOOL_ITERATION_LIMIT_FAILURE_CODE
    assert rejected["message"].startswith("The Run reached its limit of 1 dispatched")


def _tool_message(call: ToolCall, result: JsonObject) -> ChatMessage:
    return ChatMessage.tool(tool_call_id=call.id, name=call.name, content=json.dumps(result))


def test_failed_tool_call_circuit_breaker_resets_on_change_and_success() -> None:
    breaker = _FailedToolCallCircuitBreaker()
    berlin = ToolCall(id="call-berlin", name="weather", arguments={"city": "Berlin"})
    paris = ToolCall(id="call-paris", name="weather", arguments={"city": "Paris"})
    failed = tool_failure("weather_error", "unavailable")

    for _ in range(MAX_IDENTICAL_FAILED_TOOL_CALLS - 1):
        assert breaker.observe([berlin], [_tool_message(berlin, failed)]) is None
    assert breaker.observe([paris], [_tool_message(paris, failed)]) is None
    assert breaker.observe([paris], [_tool_message(paris, tool_success({}))]) is None
    for _ in range(MAX_IDENTICAL_FAILED_TOOL_CALLS - 1):
        assert breaker.observe([paris], [_tool_message(paris, failed)]) is None
    assert breaker.observe([paris], [_tool_message(paris, failed)]) == "weather"


def test_failed_tool_call_circuit_breaker_keys_error_class_schema_and_rejection() -> None:
    breaker = _FailedToolCallCircuitBreaker(limit=2)
    call = ToolCall(id="call", name="weather", arguments={"city": "Berlin"})

    class Registry:
        fingerprint = "schema-v1"

        def schema_fingerprint(self, name: str) -> str:
            assert name == "weather"
            return self.fingerprint

    registry = Registry()

    def failed(code: str) -> list[ChatMessage]:
        return [_tool_message(call, tool_failure(code, "failed"))]

    assert breaker.observe([call], failed("timeout"), registry) is None
    assert breaker.observe([call], failed("permission_denied"), registry) is None
    registry.fingerprint = "schema-v2"
    assert breaker.observe([call], failed("permission_denied"), registry) is None
    assert breaker.observe([call], failed("permission_denied"), registry) == "weather"

    def rejected(fingerprint: str) -> ToolCall:
        return ToolCall(
            id=f"call-{fingerprint}",
            name="write",
            arguments={},
            rejection=ToolCallRejection(
                code="malformed_tool_arguments",
                message="Arguments were malformed.",
                fingerprint=fingerprint,
            ),
        )

    rejected_breaker = _FailedToolCallCircuitBreaker(limit=2)
    malformed = tool_failure("malformed_tool_arguments", "rejected")
    first, second = rejected("raw-a"), rejected("raw-b")
    assert rejected_breaker.observe([first], [_tool_message(first, malformed)]) is None
    assert rejected_breaker.observe([second], [_tool_message(second, malformed)]) is None
    assert rejected_breaker.observe([second], [_tool_message(second, malformed)]) == "write"


def test_tool_result_facts_skip_messages_that_are_not_tool_results() -> None:
    call = ToolCall(id="call", name="probe", arguments={})
    # The role alone marks a Tool Result: a tool_call_id on another role is invalid
    # but unchecked until persistence, and must not produce facts.
    stray = replace(
        ChatMessage.assistant(model="openai/gpt-5.2", content="{}"), tool_call_id="call-stray"
    )
    messages = [ChatMessage.user("Run it"), stray, _tool_message(call, tool_success({}))]

    assert tool_result_facts(messages) == {"call": ToolResultFacts(status="completed", ok=True)}
