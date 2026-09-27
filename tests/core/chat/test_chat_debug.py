"""Debug context for Provider requests: every Model request of a Run, including a fallback
Model's, tells a recording Adapter which Run, Agent, Session, route and iteration it serves.

Adapters without ``set_debug_context`` (the plain test doubles used everywhere else) receive
nothing and run normally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.debug.recorder import DebugContext
from core.providers.errors import ProviderRateLimitError
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    last_run,
)

JsonObject = dict[str, Any]


class DebugTrackingAdapter(StubAdapter):
    """Records every debug context the Chat Run sets before a request."""

    def __init__(self, responses: list[Any], **options: Any) -> None:
        super().__init__(responses, **options)
        self.debug_contexts: list[DebugContext] = []

    def set_debug_context(self, context: DebugContext) -> None:
        self.debug_contexts.append(context)


def _echo_call(call_id: str, value: str) -> JsonObject:
    return {"id": call_id, "name": "echo", "arguments": {"value": value}}


def _echo_stream(call_id: str, value: str) -> list[JsonObject]:
    return [
        {
            "type": "tool_call_delta",
            "id": call_id,
            "name_delta": "echo",
            "arguments_delta": f'{{"value":"{value}"}}',
        },
        {"type": "finish", "reason": "tool_calls"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["plain", "streaming"])
async def test_every_model_request_of_a_run_carries_its_debug_context(
    tmp_path: Path, streaming: bool
) -> None:
    adapter = DebugTrackingAdapter(
        [
            {"content": None, "tool_calls": [_echo_call("call_1", "first")]},
            {"content": None, "tool_calls": [_echo_call("call_2", "second")]},
            {"content": "Done", "tool_calls": None},
        ],
        stream_responses=[
            _echo_stream("call_1", "first"),
            _echo_stream("call_2", "second"),
            [{"type": "content_delta", "text": "Done"}, {"type": "finish", "reason": "stop"}],
        ],
    )
    tools = ToolRegistry()
    tools.register(
        "echo",
        "Echo.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"value": arguments["value"]}),
    )
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["echo"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "echo twice", session_id="session-one"
    )

    run_id = last_run(runtime).id
    assert [
        (
            context.run_id,
            context.agent_id,
            context.session_id,
            context.provider_id,
            context.connection_id,
            context.model_id,
            context.streaming,
            context.iteration_number,
        )
        for context in adapter.debug_contexts
    ] == [
        (run_id, "coder", "session-one", "openai", "openai:api-key", "gpt-5.2", streaming, step)
        for step in (1, 2, 3)
    ]


@pytest.mark.asyncio
async def test_fallback_model_request_carries_the_same_runs_debug_context(
    tmp_path: Path, recovery_waits: list[float]
) -> None:
    primary = DebugTrackingAdapter([ProviderRateLimitError("primary rate limited")])
    fallback = DebugTrackingAdapter([{"content": "Recovered", "tool_calls": None}])
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_models=["anthropic/claude-sonnet-4::api-key"],
        allowed_tools=["*"],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary,
        provider_ids={"openai", "anthropic"},
        adapters_by_connection={"anthropic:api-key": fallback},
    )

    answer = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert answer.content == "Recovered"
    [primary_context] = primary.debug_contexts
    [fallback_context] = fallback.debug_contexts
    assert (
        fallback_context.provider_id,
        fallback_context.connection_id,
        fallback_context.model_id,
    ) == ("anthropic", "anthropic:api-key", "claude-sonnet-4")
    assert (fallback_context.run_id, fallback_context.agent_id, fallback_context.session_id) == (
        primary_context.run_id,
        "coder",
        "session-one",
    )
