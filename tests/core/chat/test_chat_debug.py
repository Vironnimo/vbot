"""Tests for chat loop debug context passing.

Verifies that the chat loop sets ``DebugContext`` on adapters before each
provider request, that the context includes correct run metadata, that
the streaming flag matches the chat loop's mode, that iteration_number
increments across tool-call iterations, and that the context is NOT
passed to adapters without a debug recorder.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatLoop, ChatSessionManager
from core.debug.recorder import DebugContext
from core.runs import ChatRunManager
from core.tools import ToolRegistry, tool_success

JsonObject = dict[str, Any]


# ---------------------------------------------------------------------------
# Stub adapter with debug context tracking
# ---------------------------------------------------------------------------


class DebugTrackingStubAdapter:
    """Stub adapter that records set_debug_context() calls.

    This is separate from the test_chat_loop.py StubAdapter so we can
    add debug context tracking without modifying existing test stubs.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.requests: list[JsonObject] = []
        self.debug_contexts: list[DebugContext] = []

    async def send(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> JsonObject:
        self.requests.append(
            {
                "messages": deepcopy(messages),
                "model_id": model_id,
                "kwargs": deepcopy(kwargs),
            }
        )
        if not self._responses:
            raise AssertionError("unexpected adapter request")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return cast(JsonObject, response)

    def normalize_response(self, response: JsonObject) -> JsonObject:
        return response

    def set_debug_context(self, ctx: DebugContext) -> None:
        """Record the debug context for later verification."""
        self.debug_contexts.append(ctx)

    async def aclose(self) -> None:
        pass


class DebugTrackingStreamingStubAdapter(DebugTrackingStubAdapter):
    """Stub adapter that also supports streaming."""

    def __init__(
        self,
        responses: list[Any],
        *,
        stream_responses: list[Any] | None = None,
    ) -> None:
        super().__init__(responses)
        self._stream_responses = stream_responses or []
        self.stream_requests: list[JsonObject] = []

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {
                "messages": deepcopy(messages),
                "model_id": model_id,
                "kwargs": deepcopy(kwargs),
            }
        )
        if not self._stream_responses:
            raise AssertionError("unexpected adapter stream request")
        response = self._stream_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        for delta in response:
            if isinstance(delta, Exception):
                raise delta
            yield delta


# ---------------------------------------------------------------------------
# Stub runtime
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StubAgent:
    id: str
    model: str
    fallback_model: str = ""
    temperature: float = 0.1
    thinking_effort: str = "high"
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    workspace: Path | None = None


class StubAgents:
    def __init__(self, agent: StubAgent) -> None:
        self._agent = agent

    def get(self, agent_id: str) -> StubAgent:
        assert agent_id == self._agent.id
        return self._agent


class StubProviders:
    def __init__(self, provider_ids: set[str]) -> None:
        self._provider_ids = provider_ids

    def get(self, provider_id: str) -> object:
        if provider_id not in self._provider_ids:
            raise KeyError(provider_id)

        @dataclass(frozen=True)
        class _Conn:
            id: str

        return _StubProviderConfig([_Conn("api-key")])


@dataclass(frozen=True)
class _StubProviderConfig:
    connections: list[Any]


class StubPrompts:
    def build_system_prompt(self, agent: StubAgent) -> str:
        return f"System for {agent.id}"

    def provider_tool_definitions(self, agent: StubAgent) -> list[JsonObject]:
        return [
            {
                "name": "get_weather",
                "description": "Get weather.",
                "parameters": {"type": "object"},
            }
        ]


class StubProviderCredentials:
    def __init__(self, usable_connection_ids: set[str]) -> None:
        self._usable_connection_ids = usable_connection_ids

    def has_credentials(self, _provider_id: str, connection_id: str | None = None) -> bool:
        return connection_id in self._usable_connection_ids


class StubRuntime:
    def __init__(
        self,
        *,
        data_dir: Path,
        agent: StubAgent,
        adapter: DebugTrackingStubAdapter,
        tools: ToolRegistry | None = None,
    ) -> None:
        self.agents = StubAgents(agent)
        self.chat_sessions = ChatSessionManager(data_dir)
        self.system_prompts = StubPrompts()
        self.tools = tools or ToolRegistry()
        self.chat_runs = ChatRunManager()
        self.providers = StubProviders({agent.model.split("/", 1)[0]})
        self.provider_credentials = StubProviderCredentials(
            {f"{agent.model.split('/', 1)[0]}:api-key"}
        )
        self.adapter = adapter
        self.adapter_provider_id: str | None = None
        self.adapter_connection_id: str | None = None

    def get_adapter(self, provider_id: str, connection_id: str) -> DebugTrackingStubAdapter:
        self.adapter_provider_id = provider_id
        self.adapter_connection_id = connection_id
        return self.adapter


# ---------------------------------------------------------------------------
# Debug context is set before adapter calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_context_is_set_before_send(tmp_path: Path) -> None:
    """The chat loop calls set_debug_context() before adapter.send()."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = DebugTrackingStubAdapter(
        [{"content": "Hello", "reasoning": None, "tool_calls": None}]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert len(adapter.debug_contexts) == 1


@pytest.mark.asyncio
async def test_debug_context_is_set_before_stream(tmp_path: Path) -> None:
    """The streaming chat loop calls set_debug_context() before adapter.stream()."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = DebugTrackingStreamingStubAdapter(
        [{"content": "Hello", "tool_calls": None}],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Hello"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    assert len(adapter.debug_contexts) == 1
    assert len(adapter.stream_requests) == 1


# ---------------------------------------------------------------------------
# Context includes correct fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_context_includes_correct_run_agent_session_ids(
    tmp_path: Path,
) -> None:
    """The debug context contains run_id, agent_id, and session_id."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = DebugTrackingStubAdapter(
        [{"content": "Hello", "reasoning": None, "tool_calls": None}]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    ctx = adapter.debug_contexts[0]
    assert ctx.agent_id == "coder"
    assert ctx.session_id == "session-one"
    assert ctx.run_id
    assert len(ctx.run_id) > 0


@pytest.mark.asyncio
async def test_debug_context_includes_provider_and_connection_ids(
    tmp_path: Path,
) -> None:
    """The debug context contains provider_id and connection_id."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = DebugTrackingStubAdapter(
        [{"content": "Hello", "reasoning": None, "tool_calls": None}]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    ctx = adapter.debug_contexts[0]
    assert ctx.provider_id == "openai"
    assert ctx.connection_id == "openai:api-key"


@pytest.mark.asyncio
async def test_debug_context_includes_model_id(tmp_path: Path) -> None:
    """The debug context contains the model_id."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = DebugTrackingStubAdapter(
        [{"content": "Hello", "reasoning": None, "tool_calls": None}]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    ctx = adapter.debug_contexts[0]
    assert ctx.model_id == "gpt-5.2"


# ---------------------------------------------------------------------------
# Context includes streaming flag matching the chat loop's mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_context_streaming_false_for_non_streaming_loop(
    tmp_path: Path,
) -> None:
    """Non-streaming ChatLoop sets streaming=False in debug context."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = DebugTrackingStubAdapter(
        [{"content": "Hello", "reasoning": None, "tool_calls": None}]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    ctx = adapter.debug_contexts[0]
    assert ctx.streaming is False


@pytest.mark.asyncio
async def test_debug_context_streaming_true_for_streaming_loop(
    tmp_path: Path,
) -> None:
    """Streaming ChatLoop sets streaming=True in debug context."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = DebugTrackingStreamingStubAdapter(
        [{"content": "Hello", "tool_calls": None}],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Hello"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    ctx = adapter.debug_contexts[0]
    assert ctx.streaming is True


# ---------------------------------------------------------------------------
# Context includes iteration_number (incrementing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_context_iteration_starts_at_one(tmp_path: Path) -> None:
    """The first iteration has iteration_number=1."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = DebugTrackingStubAdapter(
        [{"content": "Hello", "reasoning": None, "tool_calls": None}]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    ctx = adapter.debug_contexts[0]
    assert ctx.iteration_number == 1


@pytest.mark.asyncio
async def test_debug_context_iteration_increments_across_tool_calls(
    tmp_path: Path,
) -> None:
    """Iteration number increments when the model makes tool calls."""
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["echo"])
    adapter = DebugTrackingStubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "echo", "arguments": {"value": "first"}}],
            },
            {
                "content": None,
                "tool_calls": [{"id": "call_2", "name": "echo", "arguments": {"value": "second"}}],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )

    tools = ToolRegistry()
    tools.register(
        "echo",
        "Echo.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"value": arguments["value"]}),
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await ChatLoop(runtime).send("coder", "echo twice", session_id="session-one")

    assert len(adapter.debug_contexts) == 3
    assert adapter.debug_contexts[0].iteration_number == 1
    assert adapter.debug_contexts[1].iteration_number == 2
    assert adapter.debug_contexts[2].iteration_number == 3


# ---------------------------------------------------------------------------
# Context is NOT passed to adapters without a debug recorder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_debug_context_for_adapters_without_set_debug_context(
    tmp_path: Path,
) -> None:
    """The chat loop does not crash when the adapter lacks set_debug_context."""
    from tests.core.chat.test_chat_loop import StubAdapter as BaseStubAdapter
    from tests.core.chat.test_chat_loop import StubAgent as BaseStubAgent
    from tests.core.chat.test_chat_loop import StubRuntime as BaseStubRuntime

    agent = BaseStubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BaseStubAdapter([{"content": "Hello", "tool_calls": None}])

    runtime = BaseStubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    # Should not raise.
    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")
    assert assistant.content == "Hello"
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_no_debug_context_for_streaming_without_set_debug_context(
    tmp_path: Path,
) -> None:
    """The streaming chat loop does not crash when the adapter lacks
    set_debug_context."""
    from tests.core.chat.test_chat_loop import StubAdapter as BaseStubAdapter
    from tests.core.chat.test_chat_loop import StubAgent as BaseStubAgent
    from tests.core.chat.test_chat_loop import StubRuntime as BaseStubRuntime

    agent = BaseStubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BaseStubAdapter(
        [{"content": "Hello", "tool_calls": None}],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Hello"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime = BaseStubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )
    assert assistant.content == "Hello"


# ---------------------------------------------------------------------------
# Fallback adapter also receives debug context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_adapter_receives_debug_context(tmp_path: Path) -> None:
    """When the primary model fails and fallback is used, the fallback
    adapter also receives debug context."""
    from core.providers.errors import ProviderRateLimitError

    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        fallback_model="anthropic/claude-sonnet-4::api-key",
        allowed_tools=["*"],
    )
    primary_adapter = DebugTrackingStubAdapter([ProviderRateLimitError("primary rate limited")])
    fallback_adapter = DebugTrackingStubAdapter([{"content": "Recovered", "tool_calls": None}])

    class FallbackStubRuntime(StubRuntime):
        def __init__(
            self,
            *,
            data_dir: Path,
            agent: StubAgent,
            adapter: DebugTrackingStubAdapter,
            fallback_adapter: DebugTrackingStubAdapter,
            tools: ToolRegistry | None = None,
        ) -> None:
            super().__init__(data_dir=data_dir, agent=agent, adapter=adapter, tools=tools)
            self.fallback_adapter = fallback_adapter

        def get_adapter(self, provider_id: str, connection_id: str) -> DebugTrackingStubAdapter:
            self.adapter_provider_id = provider_id
            self.adapter_connection_id = connection_id
            if connection_id.startswith("anthropic"):
                return self.fallback_adapter
            return self.adapter

    runtime = FallbackStubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary_adapter,
        fallback_adapter=fallback_adapter,
    )
    runtime.providers = StubProviders({"openai", "anthropic"})
    runtime.provider_credentials = StubProviderCredentials({"openai:api-key", "anthropic:api-key"})

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.content == "Recovered"
    assert len(fallback_adapter.debug_contexts) == 1

    # Fallback context has correct provider info.
    fallback_ctx = fallback_adapter.debug_contexts[0]
    assert fallback_ctx.provider_id == "anthropic"
    assert fallback_ctx.connection_id == "anthropic:api-key"
    assert fallback_ctx.model_id == "claude-sonnet-4"
    # Same run/session info.
    assert fallback_ctx.run_id == primary_adapter.debug_contexts[0].run_id
    assert fallback_ctx.agent_id == "coder"
    assert fallback_ctx.session_id == "session-one"
