"""Chat-loop tests grouped by fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.model_tasks import TASK_IMAGE_UNDERSTANDING
from core.providers.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
)
from core.runs import (
    ERROR_MESSAGE_PERSISTED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    RunStatus,
)
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    ToolRegistry,
    tool_success,
)
from core.utils.errors import ConfigError, ProviderError
from tests.core.chat.chat_loop_support import (
    ClosingStubAdapter,
    StubAdapter,
    StubAgent,
    StubModels,
    StubRuntime,
    build_chat_loop,
    persisted_roles,
    session_address,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
async def test_fallback_rebuilds_route_gated_image_tool_visibility(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/vision-model",
        fallback_model="anthropic/text-model::api-key",
        allowed_tools=[ANALYZE_IMAGE_TOOL_NAME],
    )
    primary_adapter = StubAdapter(
        [ProviderRateLimitError("primary rate limited")],  # type: ignore[list-item]
        wire_media_types=frozenset({"image/png"}),
    )
    fallback_adapter = StubAdapter([{"content": "Recovered", "tool_calls": None}])
    tools = ToolRegistry()
    tools.register(
        ANALYZE_IMAGE_TOOL_NAME,
        "Analyze images.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"analysis": "ok"}),
    )
    models = StubModels(
        {
            ("openai", "vision-model"): 128_000,
            ("anthropic", "text-model"): 128_000,
        },
        input_modalities={
            ("openai", "vision-model"): ("text", "image"),
            ("anthropic", "text-model"): ("text",),
        },
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        adapters_by_connection={
            "openai:api-key": primary_adapter,
            "anthropic:api-key": fallback_adapter,
        },
        provider_ids={"openai", "anthropic"},
        tools=tools,
        models=models,
        available_task_models={TASK_IMAGE_UNDERSTANDING},
    )

    await build_chat_loop(runtime).send("coder", "Inspect it", session_id="s1")

    primary_tools = primary_adapter.requests[0]["kwargs"]["tools"]
    fallback_tools = fallback_adapter.requests[0]["kwargs"]["tools"]
    assert ANALYZE_IMAGE_TOOL_NAME not in {definition["name"] for definition in primary_tools}
    assert ANALYZE_IMAGE_TOOL_NAME in {definition["name"] for definition in fallback_tools}


@pytest.mark.asyncio
async def test_send_closes_adapter_when_aclose_exists(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert adapter.closed is True


@pytest.mark.asyncio
async def test_send_closes_adapter_after_provider_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([ProviderError("provider failed", retryable=False)])  # type: ignore[list-item]
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ProviderError, match="provider failed"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert adapter.closed is True


@pytest.mark.asyncio
async def test_provider_rate_limit_error_is_persisted_and_run_fails(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([ProviderRateLimitError("too many requests")])  # type: ignore[list-item]
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ProviderRateLimitError, match="too many requests"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.FAILED
    assert persisted_roles(messages) == ["user", "error"]
    assert run.iteration_count == 0
    assert messages[-1].iteration_count == 0
    assert run.events[-1].payload["iteration_count"] == 0
    assert messages[1].error_kind == "rate_limit"
    assert messages[1].content == "too many requests"
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        ERROR_MESSAGE_PERSISTED_EVENT,
        "run_failed",
    ]
    assert run.events[2].payload["message"]["role"] == "error"
    assert run.events[2].payload["message"]["error_kind"] == "rate_limit"


@pytest.mark.asyncio
async def test_fallback_model_activates_on_retryable_error(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_model="anthropic/claude-sonnet-4::api-key",
        allowed_tools=["*"],
    )
    primary_adapter = StubAdapter([ProviderRateLimitError("primary rate limited")])  # type: ignore[list-item]
    fallback_adapter = StubAdapter([{"content": "Recovered", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        adapters_by_connection={
            "openai:api-key": primary_adapter,
            "anthropic:api-key": fallback_adapter,
        },
        provider_ids={"openai", "anthropic"},
    )

    assistant = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    fallback_events = [
        event for event in run.events if event.type == MODEL_FALLBACK_ACTIVATED_EVENT
    ]
    assert assistant.content == "Recovered"
    assert persisted_roles(messages) == ["user", "note", "assistant"]
    assert run.iteration_count == 1
    assert messages[-1].iteration_count == 1
    assert run.events[-1].payload["iteration_count"] == 1
    assert messages[1].content == (
        "Primary model unavailable. Switched to anthropic/claude-sonnet-4::api-key for this run."
    )
    assert len(fallback_events) == 1
    assert fallback_events[0].payload == {
        "from_model": "openai/gpt-5.2",
        "to_model": "anthropic/claude-sonnet-4::api-key",
    }
    assert primary_adapter.requests[0]["model_id"] == "gpt-5.2"
    assert fallback_adapter.requests[0]["model_id"] == "claude-sonnet-4"


@pytest.mark.asyncio
async def test_streaming_fallback_activates_after_same_model_recovery_is_exhausted(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_model="anthropic/claude-sonnet-4::api-key",
        allowed_tools=["*"],
    )
    primary_adapter = StubAdapter(
        [],
        stream_responses=[ProviderRateLimitError("primary rate limited") for _ in range(3)],
    )
    fallback_adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Recovered"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        adapters_by_connection={
            "openai:api-key": primary_adapter,
            "anthropic:api-key": fallback_adapter,
        },
        provider_ids={"openai", "anthropic"},
    )

    assistant = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    assert assistant.content == "Recovered"
    assert len(primary_adapter.stream_requests) == 3
    assert len(fallback_adapter.stream_requests) == 1
    assert run.status == RunStatus.COMPLETED
    assert [event.type for event in run.events if event.type == MODEL_FALLBACK_ACTIVATED_EVENT] == [
        MODEL_FALLBACK_ACTIVATED_EVENT
    ]


@pytest.mark.asyncio
async def test_fallback_adapter_construction_failure(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_model="anthropic/claude-sonnet-4::api-key",
        allowed_tools=["*"],
    )
    primary_adapter = StubAdapter([ProviderRateLimitError("primary rate limited")])  # type: ignore[list-item]
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        adapters_by_connection={"openai:api-key": primary_adapter},
        raise_on_connection={"anthropic:api-key": ConfigError("bad credential")},
        provider_ids={"openai", "anthropic"},
    )

    with pytest.raises(ConfigError, match="bad credential"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    event_types = [event.type for event in run.events]
    assert run.status == RunStatus.FAILED
    assert persisted_roles(messages) == ["user", "error"]
    assert ERROR_MESSAGE_PERSISTED_EVENT in event_types
    assert MODEL_FALLBACK_ACTIVATED_EVENT not in event_types


@pytest.mark.asyncio
async def test_next_turn_reuses_primary_model(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_model="anthropic/claude-sonnet-4::api-key",
        allowed_tools=["*"],
    )
    primary_adapter = StubAdapter(
        [
            ProviderRateLimitError("primary rate limited"),
            {"content": "Primary turn 2", "tool_calls": None},
        ]
    )
    fallback_adapter = StubAdapter([{"content": "Fallback turn 1", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        adapters_by_connection={
            "openai:api-key": primary_adapter,
            "anthropic:api-key": fallback_adapter,
        },
        provider_ids={"openai", "anthropic"},
    )

    first_assistant = await build_chat_loop(runtime).send("coder", "turn 1", session_id="s1")
    second_assistant = await build_chat_loop(runtime).send("coder", "turn 2", session_id="s1")

    fallback_event_count = sum(
        1
        for run in runtime.chat_runs._runs.values()
        for event in run.events
        if event.type == MODEL_FALLBACK_ACTIVATED_EVENT
    )
    assert first_assistant.content == "Fallback turn 1"
    assert second_assistant.content == "Primary turn 2"
    assert len(primary_adapter.requests) == 2
    assert len(fallback_adapter.requests) == 1
    assert fallback_event_count == 1


@pytest.mark.asyncio
async def test_fallback_not_triggered_on_non_retryable_error(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_model="anthropic/claude-sonnet-4::api-key",
        allowed_tools=["*"],
    )
    primary_adapter = StubAdapter([ProviderAuthError("invalid credential")])  # type: ignore[list-item]
    fallback_adapter = StubAdapter([{"content": "Should not be used", "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        adapters_by_connection={
            "openai:api-key": primary_adapter,
            "anthropic:api-key": fallback_adapter,
        },
        provider_ids={"openai", "anthropic"},
    )

    with pytest.raises(ProviderAuthError, match="invalid credential"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.FAILED
    assert persisted_roles(messages) == ["user", "error"]
    assert not any(event.type == MODEL_FALLBACK_ACTIVATED_EVENT for event in run.events)
    assert fallback_adapter.requests == []


@pytest.mark.asyncio
async def test_fallback_not_triggered_when_fallback_model_empty(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([ProviderRateLimitError("primary rate limited")])  # type: ignore[list-item]
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ProviderRateLimitError, match="primary rate limited"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    assert run.status == RunStatus.FAILED
    assert persisted_roles(messages) == ["user", "error"]
    assert not any(event.type == MODEL_FALLBACK_ACTIVATED_EVENT for event in run.events)


@pytest.mark.asyncio
async def test_fallback_stays_active_for_rest_of_run(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_model="anthropic/claude-sonnet-4::api-key",
        allowed_tools=["echo"],
    )
    primary_adapter = StubAdapter([ProviderRateLimitError("primary rate limited")])  # type: ignore[list-item]
    fallback_adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "echo", "arguments": {"value": "x"}}],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "echo",
        "Echo value.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"value": arguments["value"]}),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        adapters_by_connection={
            "openai:api-key": primary_adapter,
            "anthropic:api-key": fallback_adapter,
        },
        provider_ids={"openai", "anthropic"},
        tools=tools,
    )

    assistant = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.content == "Done"
    assert len(primary_adapter.requests) == 1
    assert len(fallback_adapter.requests) == 2
    assert primary_adapter.requests[0]["model_id"] == "gpt-5.2"
    assert all(request["model_id"] == "claude-sonnet-4" for request in fallback_adapter.requests)


@pytest.mark.asyncio
async def test_fallback_request_strips_primary_provider_reasoning_meta(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_model="anthropic/claude-sonnet-4::api-key",
        allowed_tools=["echo"],
    )
    primary_adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning": "Primary readable reasoning",
                "reasoning_meta": {"reasoning_details": [{"type": "primary-opaque"}]},
                "tool_calls": [{"id": "call_1", "name": "echo", "arguments": {"value": "x"}}],
            },
            ProviderRateLimitError("primary rate limited"),
        ]
    )
    fallback_adapter = StubAdapter(
        [
            {
                "content": "Done",
                "reasoning": "Fallback reasoning",
                "reasoning_meta": {"content_blocks": [{"type": "thinking", "signature": "fb"}]},
                "tool_calls": None,
            }
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "echo",
        "Echo value.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"value": arguments["value"]}),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        adapters_by_connection={
            "openai:api-key": primary_adapter,
            "anthropic:api-key": fallback_adapter,
        },
        provider_ids={"openai", "anthropic"},
        tools=tools,
    )

    assistant = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.content == "Done"
    assert assistant.reasoning_scope == "anthropic/claude-sonnet-4::api-key"
    # The primary's own tool-continuation request still round-trips its meta.
    primary_followup_assistants = [
        message
        for message in primary_adapter.requests[1]["messages"]
        if message.get("role") == "assistant"
    ]
    assert any("reasoning_meta" in message for message in primary_followup_assistants)
    # The fallback provider must never see the primary's reasoning fields.
    fallback_assistants = [
        message
        for message in fallback_adapter.requests[0]["messages"]
        if message.get("role") == "assistant"
    ]
    assert fallback_assistants
    assert all(
        "reasoning" not in message and "reasoning_meta" not in message
        for message in fallback_assistants
    )
    # The completed Tool turn's readable work survives the route change only
    # as explicitly provider-neutral context, after its Tool result.
    fallback_messages = fallback_adapter.requests[0]["messages"]
    portable_notes = [
        message
        for message in fallback_messages
        if message.get("role") == "user"
        and "Primary readable reasoning" in message.get("content", "")
    ]
    assert len(portable_notes) == 1
    tool_result_index = next(
        index for index, message in enumerate(fallback_messages) if message.get("role") == "tool"
    )
    portable_note_index = fallback_messages.index(portable_notes[0])
    assert portable_note_index > tool_result_index
    assert "primary-opaque" not in str(fallback_messages)


@pytest.mark.asyncio
async def test_fallback_failure_persists_fallback_error(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_model="anthropic/claude-sonnet-4::api-key",
        allowed_tools=["*"],
    )
    primary_adapter = StubAdapter([ProviderRateLimitError("primary rate limited")])  # type: ignore[list-item]
    fallback_adapter = StubAdapter([ProviderRateLimitError("fallback rate limited")])  # type: ignore[list-item]
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        adapters_by_connection={
            "openai:api-key": primary_adapter,
            "anthropic:api-key": fallback_adapter,
        },
        provider_ids={"openai", "anthropic"},
    )

    with pytest.raises(ProviderRateLimitError, match="fallback rate limited"):
        await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    error_events = [event for event in run.events if event.type == ERROR_MESSAGE_PERSISTED_EVENT]
    assert run.status == RunStatus.FAILED
    assert len(error_events) == 1
    assert any(event.type == MODEL_FALLBACK_ACTIVATED_EVENT for event in run.events)
    assert persisted_roles(messages) == ["user", "note", "error"]
    error_message = next(message for message in messages if message.role == "error")
    assert error_message.error_kind == "rate_limit"
