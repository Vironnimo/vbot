"""Tests for the minimal non-streaming agentic chat loop."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    ActiveRunError,
    ChatError,
    ChatLoop,
    ChatMessage,
    ChatRunManager,
    ChatSessionManager,
    RunCancelledError,
    RunStatus,
)
from core.tools import JsonObject as ToolJsonObject
from core.tools import (
    ToolContext,
    ToolRegistry,
    register_glob_tool,
    register_grep_tool,
    tool_failure,
    tool_success,
)
from core.utils.errors import ProviderError
from core.utils.tokens import estimate_tokens

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class StubAgent:
    id: str
    model: str
    connection: str | None = None
    fallback_connection: str = ""
    temperature: float = 0.1
    thinking_effort: str = "high"
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    workspace: Path | None = None

    def __post_init__(self) -> None:
        if self.connection is not None:
            return
        provider_id = self.model.split("/", 1)[0] if self.model else "openai"
        object.__setattr__(self, "connection", f"{provider_id}:api-key")


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
        return StubProviderConfig([StubConnection("oauth"), StubConnection("api-key")])


@dataclass(frozen=True)
class StubConnection:
    id: str


@dataclass(frozen=True)
class StubProviderConfig:
    connections: list[StubConnection]


class LegacyDispatchToolRegistry:
    def __init__(self, result: JsonObject) -> None:
        self.result = result

    async def dispatch(
        self,
        name: Any,
        arguments: ToolJsonObject,
        allowed_tools: list[str] | None = None,
    ) -> JsonObject:
        if not isinstance(name, str):
            raise TypeError("legacy dispatch expected string argument")
        assert name == "legacy"
        assert arguments == {"value": "input"}
        assert allowed_tools == ["legacy"]
        return self.result


class StubPrompts:
    def __init__(self) -> None:
        self.agent_for_tools: StubAgent | None = None

    def build_system_prompt(self, agent: StubAgent) -> str:
        return f"System for {agent.id}"

    def provider_tool_definitions(self, agent: StubAgent) -> list[JsonObject]:
        self.agent_for_tools = agent
        return [
            {
                "name": "get_weather",
                "description": "Get weather.",
                "parameters": {"type": "object"},
            }
        ]


@dataclass(frozen=True)
class StubSkill:
    name: str
    description: str
    path: Path


class StubSkills:
    def __init__(self, skills: list[StubSkill]) -> None:
        self._skills = {skill.name: skill for skill in skills}

    def filter_allowed(self, allowed_skills: list[str]) -> list[StubSkill]:
        if "*" in allowed_skills:
            return sorted(self._skills.values(), key=lambda skill: skill.name)
        return [self._skills[name] for name in allowed_skills if name in self._skills]


class StubAdapter:
    def __init__(self, responses: list[Any], *, stream_responses: list[Any] | None = None) -> None:
        self._responses = responses
        self._stream_responses = stream_responses or []
        self.requests: list[JsonObject] = []
        self.stream_requests: list[JsonObject] = []

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        if not self._responses:
            raise AssertionError("unexpected adapter request")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return cast(JsonObject, response)

    def normalize_response(self, response: JsonObject) -> JsonObject:
        return response

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
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


class ClosingStubAdapter(StubAdapter):
    def __init__(self, responses: list[JsonObject]) -> None:
        super().__init__(responses)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class BlockingStubAdapter(StubAdapter):
    def __init__(self) -> None:
        super().__init__([])
        self.request_started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> JsonObject:
        self.requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        self.request_started.set()
        await self.release.wait()
        return {"content": "Late", "tool_calls": None}


class BlockingStreamingStubAdapter(ClosingStubAdapter):
    def __init__(self) -> None:
        super().__init__([])
        self.stream_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "content_delta", "text": "before"}
        self.stream_started.set()
        await self.release.wait()
        yield {"type": "content_delta", "text": "late"}


class StalledStreamingStubAdapter(StubAdapter):
    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.stream_requests.append(
            {"messages": deepcopy(messages), "model_id": model_id, "kwargs": deepcopy(kwargs)}
        )
        yield {"type": "content_delta", "text": "partial"}
        await asyncio.sleep(1)
        yield {"type": "content_delta", "text": "late"}


class StubRuntime:
    def __init__(
        self,
        *,
        data_dir: Path,
        agent: StubAgent,
        adapter: StubAdapter,
        provider_ids: set[str] | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self.agents = StubAgents(agent)
        self.chat_sessions = ChatSessionManager(data_dir)
        self.system_prompts = StubPrompts()
        self.tools = tools or ToolRegistry()
        self.chat_runs = ChatRunManager()
        self.providers = StubProviders(provider_ids or {agent.model.split("/", 1)[0]})
        self.provider_credentials = StubProviderCredentials(
            {f"{agent.model.split('/', 1)[0]}:api-key"}
        )
        self.skills = StubSkills([])
        self.adapter = adapter
        self.adapter_provider_id: str | None = None
        self.adapter_connection_id: str | None = None

    def get_adapter(self, provider_id: str, connection_id: str) -> StubAdapter:
        self.adapter_provider_id = provider_id
        self.adapter_connection_id = connection_id
        return self.adapter


class StubProviderCredentials:
    def __init__(self, usable_connection_ids: set[str]) -> None:
        self._usable_connection_ids = usable_connection_ids

    def has_credentials(self, _provider_id: str, connection_id: str | None = None) -> bool:
        return connection_id in self._usable_connection_ids


@pytest.mark.asyncio
async def test_send_appends_user_and_final_assistant_without_tools(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openrouter/anthropic/claude-sonnet-4", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "reasoning": None, "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    session = runtime.chat_sessions.get("coder", "session-one")
    messages = session.load()
    assert assistant.content == "Hello"
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "Hi"
    assert messages[1].content == "Hello"
    assert runtime.adapter_provider_id == "openrouter"
    assert runtime.adapter_connection_id == "openrouter:api-key"
    assert adapter.requests[0]["model_id"] == "anthropic/claude-sonnet-4"
    assert adapter.requests[0]["kwargs"] == {
        "temperature": 0.1,
        "thinking_effort": "high",
        "tools": [
            {
                "name": "get_weather",
                "description": "Get weather.",
                "parameters": {"type": "object"},
            }
        ],
    }
    assert [message["role"] for message in adapter.requests[0]["messages"]] == ["system", "user"]
    run = next(iter(runtime.chat_runs._runs.values()))
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        "assistant_output",
        "run_completed",
    ]
    assert run.events[1].payload["message"]["content"] == "Hi"
    assert run.events[2].payload["message"]["content"] == "Hello"


@pytest.mark.asyncio
async def test_note_before_user_turn_is_embedded_as_synthetic_user_message(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.add_note("Background job completed")

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert [message["role"] for message in request_messages] == ["system", "user", "user"]
    assert request_messages[1] == {
        "role": "user",
        "content": "<system-reminder>\nBackground job completed\n</system-reminder>",
    }
    assert request_messages[2]["content"] == "Hi"
    assert all(message["role"] != "note" for message in request_messages)


@pytest.mark.asyncio
async def test_multiple_consecutive_notes_are_embedded_as_one_synthetic_user_message(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.add_note("First background event")
    session.add_note("Second background event")

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert [message["role"] for message in request_messages] == ["system", "user", "user"]
    assert request_messages[1] == {
        "role": "user",
        "content": (
            "<system-reminder>\nFirst background event\n</system-reminder>\n"
            "<system-reminder>\nSecond background event\n</system-reminder>"
        ),
    }
    assert all(message["role"] != "note" for message in request_messages)


@pytest.mark.asyncio
async def test_note_added_between_tool_iterations_is_sent_on_next_request(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["record_note"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "record_note", "arguments": {}}],
            },
            {"content": "Saw reminder", "tool_calls": None},
        ]
    )

    def record_note(context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        context.add_note("Tool finished background work")
        return tool_success({"ok": True})

    tools = ToolRegistry()
    tools.register("record_note", "Record note.", {"type": "object"}, record_note)
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await ChatLoop(runtime).send("coder", "Run tool", session_id="session-one")

    second_request_messages = adapter.requests[1]["messages"]
    assert [message["role"] for message in second_request_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert second_request_messages[-1] == {
        "role": "user",
        "content": "<system-reminder>\nTool finished background work\n</system-reminder>",
    }
    assert all(
        message["role"] != "note" for request in adapter.requests for message in request["messages"]
    )


@pytest.mark.asyncio
async def test_request_messages_without_notes_keep_existing_shape(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert [message["role"] for message in request_messages] == ["system", "user"]
    assert request_messages[0]["content"] == "System for coder"
    assert request_messages[1]["content"] == "Hi"
    assert all(message["role"] != "note" for message in request_messages)


@pytest.mark.asyncio
async def test_slash_skill_trigger_activates_before_provider_request(tmp_path: Path) -> None:
    skill_file = _write_test_skill(tmp_path, "debugging")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=["debugging"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("debugging", "Debug failures", skill_file)])

    await ChatLoop(runtime).send("coder", "/debugging fix this", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert request_messages[1]["content"].startswith('<skill_content name="debugging">')
    assert request_messages[2]["content"] == "/debugging fix this"
    request_text = "\n".join(message.get("content", "") or "" for message in request_messages)
    assert "[skill-context]" not in request_text
    assert "<system-reminder>\n[skill-context]" not in request_text


@pytest.mark.asyncio
async def test_skill_context_persists_across_later_sends_without_visible_user_message(
    tmp_path: Path,
) -> None:
    skill_file = _write_test_skill(tmp_path, "debugging")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=["debugging"],
    )
    adapter = StubAdapter(
        [
            {"content": "First", "tool_calls": None},
            {"content": "Second", "tool_calls": None},
        ]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("debugging", "Debug failures", skill_file)])

    await ChatLoop(runtime).send("coder", "/debugging fix this", session_id="session-one")
    await ChatLoop(runtime).send("coder", "continue", session_id="session-one")

    second_request_messages = adapter.requests[1]["messages"]
    assert second_request_messages[1]["content"].startswith('<skill_content name="debugging">')
    assert second_request_messages[-1]["content"] == "continue"
    persisted_messages = runtime.chat_sessions.get("coder", "session-one").load()
    visible_messages = [message for message in persisted_messages if message.role != "note"]
    assert [message.role for message in visible_messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert all(
        not (message.role == "user" and (message.content or "").startswith("<skill_content "))
        for message in visible_messages
    )


@pytest.mark.asyncio
async def test_inline_skill_trigger_preserves_original_message(tmp_path: Path) -> None:
    skill_file = _write_test_skill(tmp_path, "debugging")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=["debugging"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("debugging", "Debug failures", skill_file)])

    await ChatLoop(runtime).send(
        "coder",
        "Please use $debugging on this issue",
        session_id="session-one",
    )

    request_messages = adapter.requests[0]["messages"]
    assert request_messages[1]["content"].startswith('<skill_content name="debugging">')
    assert request_messages[2]["content"] == "Please use $debugging on this issue"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["/debugging fix this", "Please use $debugging on this issue"],
)
async def test_skill_trigger_does_not_activate_when_allowed_skills_empty(
    tmp_path: Path,
    message: str,
) -> None:
    skill_file = _write_test_skill(tmp_path, "debugging")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=[],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("debugging", "Debug failures", skill_file)])

    await ChatLoop(runtime).send("coder", message, session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    request_text = "\n".join(message.get("content", "") or "" for message in request_messages)
    assert '<skill_content name="debugging">' not in request_text
    assert request_messages[1]["content"] == message
    assert "Skill trigger 'debugging' did not match" in request_messages[2]["content"]


@pytest.mark.asyncio
async def test_unknown_skill_trigger_adds_system_reminder(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=[],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "/missing do it", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    assert request_messages[1]["content"] == "/missing do it"
    assert "Skill trigger 'missing' did not match" in request_messages[2]["content"]


@pytest.mark.asyncio
async def test_unknown_skill_trigger_reminder_appears_once_in_first_request(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=[],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "/missing do it", session_id="session-one")

    request_text = "\n".join(
        message.get("content", "") or "" for message in adapter.requests[0]["messages"]
    )
    assert request_text.count("Skill trigger 'missing' did not match") == 1


@pytest.mark.asyncio
async def test_send_closes_adapter_when_aclose_exists(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert adapter.closed is True


@pytest.mark.asyncio
async def test_send_closes_adapter_after_provider_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([ProviderError("provider failed", retryable=False)])  # type: ignore[list-item]
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ProviderError, match="provider failed"):
        await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert adapter.closed is True


@pytest.mark.asyncio
async def test_send_dispatches_tool_and_resends_context_until_final(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning": "Need weather.",
                "reasoning_meta": {"encrypted_content": "opaque-current-turn"},
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Sunny", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await ChatLoop(runtime).send("coder", "Weather?", session_id="session-one")

    persisted = [
        message.to_dict() for message in runtime.chat_sessions.get("coder", "session-one").load()
    ]
    assert assistant.content == "Sunny"
    assert [message["role"] for message in persisted] == ["user", "assistant", "tool", "assistant"]
    assert persisted[1]["reasoning_meta"] == {"encrypted_content": "opaque-current-turn"}
    assert persisted[2]["tool_call_id"] == "call_abc"
    assert json.loads(persisted[2]["content"]) == tool_success({"temp": 22, "city": "Berlin"})
    assert [message["role"] for message in adapter.requests[1]["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert adapter.requests[1]["messages"][2]["reasoning_meta"] == {
        "encrypted_content": "opaque-current-turn"
    }
    assert adapter.requests[1]["messages"][2]["reasoning"] == "Need weather."


@pytest.mark.asyncio
async def test_streaming_mode_emits_deltas_then_final_authoritative_message(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Think"},
                {"type": "content_delta", "text": "Hello"},
                {"type": "content_delta", "text": " world"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder",
        "Hi",
        session_id="session-one",
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert assistant.content == "Hello world"
    assert assistant.reasoning == "Think"
    assert [message.role for message in messages] == ["user", "assistant"]
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        REASONING_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "reasoning",
        "assistant_output",
        "run_completed",
    ]
    assert run.events[2].payload == {"reasoning_delta": "Think"}
    assert run.events[3].payload == {"content_delta": "Hello"}
    assert run.events[6].payload["message"]["content"] == "Hello world"
    assert "reasoning_meta" not in run.events[6].payload["message"]
    assert adapter.requests == []
    assert adapter.stream_requests[0]["kwargs"]["thinking_effort"] == "high"


@pytest.mark.asyncio
async def test_streaming_mode_persists_only_final_messages_and_continues_tool_loop(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="anthropic/claude-sonnet-4", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "reasoning_delta", "text": "Need weather."},
                {"type": "reasoning_meta", "reasoning_meta": {"signature": "opaque"}},
                {
                    "type": "tool_call_delta",
                    "id": "call_abc",
                    "name_delta": "get_weather",
                    "arguments_delta": '{"city":"Ber',
                },
                {
                    "type": "tool_call_delta",
                    "id": "call_abc",
                    "arguments_delta": 'lin"}',
                },
                {"type": "finish", "reason": "tool_calls"},
            ],
            [
                {"type": "content_delta", "text": "Sunny"},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder",
        "Weather?",
        session_id="session-one",
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    persisted = [
        message.to_dict() for message in runtime.chat_sessions.get("coder", "session-one").load()
    ]
    assert assistant.content == "Sunny"
    assert [message["role"] for message in persisted] == ["user", "assistant", "tool", "assistant"]
    assert persisted[1]["reasoning_meta"] == {"signature": "opaque"}
    assert persisted[1]["tool_calls"] == [
        {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
    ]
    assert json.loads(persisted[2]["content"]) == tool_success({"temp": 22, "city": "Berlin"})
    assert adapter.stream_requests[1]["messages"][2]["reasoning_meta"] == {"signature": "opaque"}
    assert [
        event.type
        for event in run.events
        if event.type in {TOOL_CALL_DELTA_EVENT, TOOL_CALL_STARTED_EVENT}
    ] == [
        TOOL_CALL_DELTA_EVENT,
        TOOL_CALL_DELTA_EVENT,
        TOOL_CALL_STARTED_EVENT,
    ]
    tool_started = next(event for event in run.events if event.type == TOOL_CALL_STARTED_EVENT)
    assert tool_started.payload["tool_call"]["arguments"] == {"city": "Berlin"}
    assert tool_started.payload["tool_call"] == {
        "id": "call_abc",
        "index": 0,
        "name": "get_weather",
        "arguments": {"city": "Berlin"},
    }
    tool_result = next(event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT)
    assert tool_result.payload == {
        "tool_call": {"id": "call_abc", "index": 0, "name": "get_weather"},
        "result": tool_success({"temp": 22, "city": "Berlin"}),
    }
    assert all(
        "reasoning_meta" not in event.payload.get("message", {})
        for event in run.events
        if isinstance(event.payload, dict)
    )


@pytest.mark.asyncio
async def test_streaming_mode_falls_back_before_usable_streamed_output(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [{"content": "Fallback answer", "tool_calls": None}],
        stream_responses=[ProviderError("streaming is not supported", retryable=False)],
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder",
        "Hi",
        session_id="session-one",
    )

    run = next(iter(runtime.chat_runs._runs.values()))
    assert assistant.content == "Fallback answer"
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        "assistant_output",
        "run_completed",
    ]
    assert len(adapter.stream_requests) == 1
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_streaming_mode_does_not_fallback_after_visible_delta(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [{"content": "Should not use", "tool_calls": None}],
        stream_responses=[
            [
                {"type": "content_delta", "text": "partial"},
                ProviderError("streaming is not supported", retryable=False),
            ]
        ],
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ProviderError, match="not supported"):
        await ChatLoop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert [message.role for message in messages] == ["user"]
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "run_failed",
    ]
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_streaming_mode_chunk_timeout_fails_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.chat.chat.STREAM_CHUNK_TIMEOUT_SECONDS", 0.01)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StalledStreamingStubAdapter([])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(Exception, match="stalled"):
        await ChatLoop(runtime, streaming=True).send("coder", "Hi", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert run.status == RunStatus.FAILED
    assert [message.role for message in messages] == ["user"]
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "run_failed",
    ]


@pytest.mark.asyncio
async def test_streaming_mode_cancellation_closes_adapter_and_ignores_late_deltas(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStreamingStubAdapter()
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await ChatLoop(runtime, streaming=True).start_run("coder", "Hi", session_id="session-one")
    await adapter.stream_started.wait()
    run.request_cancel()
    await asyncio.sleep(0)

    with pytest.raises(RunCancelledError):
        await run.wait()

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert adapter.closed is True
    assert run.status == RunStatus.CANCELLED
    assert [message.role for message in messages] == ["user"]
    assert [event.type for event in run.events] == [
        "run_started",
        "user_message_persisted",
        ASSISTANT_OUTPUT_DELTA_EVENT,
        "run_cancelled",
    ]


@pytest.mark.asyncio
async def test_fresh_follow_up_omits_old_reasoning_and_reasoning_meta_from_request(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="anthropic/claude-sonnet-4", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Fresh answer", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Previous question"))
    session.append(
        ChatMessage.assistant(
            model="anthropic/claude-sonnet-4",
            content="Previous answer",
            reasoning="Old readable reasoning",
            reasoning_meta={
                "content_blocks": [{"type": "thinking", "thinking": "Old readable reasoning"}]
            },
        )
    )

    await ChatLoop(runtime).send("coder", "Follow up", session_id="session-one")

    assistant_history = adapter.requests[0]["messages"][2]
    persisted = [message.to_dict() for message in session.load()]
    assert assistant_history == {
        "id": persisted[1]["id"],
        "timestamp": persisted[1]["timestamp"],
        "role": "assistant",
        "model": "anthropic/claude-sonnet-4",
        "content": "Previous answer",
    }
    assert persisted[1]["reasoning"] == "Old readable reasoning"
    assert persisted[1]["reasoning_meta"] == {
        "content_blocks": [{"type": "thinking", "thinking": "Old readable reasoning"}]
    }


@pytest.mark.asyncio
async def test_start_run_requires_existing_session(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(Exception, match="session does not exist"):
        await ChatLoop(runtime).start_run("coder", "Hi", session_id="missing-session")

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_start_run_rejects_second_run_for_same_session(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStubAdapter()
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    first_run = await ChatLoop(runtime).start_run("coder", "Hi", session_id="session-one")
    await adapter.request_started.wait()

    with pytest.raises(ActiveRunError, match="active run"):
        await ChatLoop(runtime).start_run("coder", "Again", session_id="session-one")

    first_run.request_cancel()
    adapter.release.set()
    with pytest.raises(RunCancelledError):
        await first_run.wait()


@pytest.mark.asyncio
async def test_start_run_allows_parallel_different_sessions(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    first_adapter = BlockingStubAdapter()
    second_adapter = StubAdapter([{"content": "Second", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=first_adapter)
    adapters = [first_adapter, second_adapter]
    runtime.get_adapter = lambda provider_id, connection_id: adapters.pop(0)  # type: ignore[method-assign]
    runtime.chat_sessions.create("coder", session_id="session-one")
    runtime.chat_sessions.create("coder", session_id="session-two")

    first_run = await ChatLoop(runtime).start_run("coder", "First", session_id="session-one")
    await first_adapter.request_started.wait()
    second_run = await ChatLoop(runtime).start_run("coder", "Second", session_id="session-two")

    second_assistant = await second_run.wait()
    first_run.request_cancel()
    first_adapter.release.set()

    assert second_assistant.content == "Second"
    with pytest.raises(RunCancelledError):
        await first_run.wait()


@pytest.mark.asyncio
async def test_cancelled_run_ignores_late_assistant_output(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStubAdapter()
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.chat_sessions.create("coder", session_id="session-one")

    run = await ChatLoop(runtime).start_run("coder", "Hi", session_id="session-one")
    await adapter.request_started.wait()
    run.request_cancel()
    adapter.release.set()

    with pytest.raises(RunCancelledError):
        await run.wait()

    session_messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert run.status == RunStatus.CANCELLED
    assert [message.role for message in session_messages] == ["user"]
    assert "assistant_output" not in [event.type for event in run.events]


@pytest.mark.asyncio
async def test_disallowed_tool_call_is_blocked_and_persisted_before_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({}),
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await ChatLoop(runtime).send("coder", "Weather?", session_id="session-one")

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]
    assert json.loads(messages[2].content or "{}") == tool_failure(
        "tool_not_allowed",
        "Tool not allowed: get_weather",
    )


@pytest.mark.asyncio
async def test_registered_search_tools_execute_and_persist_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.tools.grep.shutil.which", lambda _command: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "code.py").write_text("print('alpha')\n", encoding="utf-8")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["glob", "grep"],
        workspace=workspace,
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_glob", "name": "glob", "arguments": {"pattern": "**/*.txt"}},
                    {"id": "call_grep", "name": "grep", "arguments": {"pattern": "alpha"}},
                ],
            },
            {"content": "Search complete", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    register_glob_tool(tools)
    register_grep_tool(tools)
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await ChatLoop(runtime).send("coder", "Search files", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    tool_messages = [message for message in messages if message.role == "tool"]
    glob_result = json.loads(tool_messages[0].content or "{}")
    grep_result = json.loads(tool_messages[1].content or "{}")
    assert assistant.content == "Search complete"
    assert [message.name for message in tool_messages] == ["glob", "grep"]
    assert glob_result == tool_success({"content": "notes.txt"})
    assert grep_result == tool_success(
        {"content": "notes.txt:1: alpha\nsrc/code.py:1: print('alpha')"}
    )
    assert [
        event.payload["tool_call"]["name"]
        for event in run.events
        if event.type == TOOL_CALL_STARTED_EVENT
    ] == ["glob", "grep"]
    assert [
        event.payload["result"] for event in run.events if event.type == TOOL_CALL_RESULT_EVENT
    ] == [glob_result, grep_result]


@pytest.mark.asyncio
async def test_registered_search_tools_respect_agent_allowlist(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("alpha\n", encoding="utf-8")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["glob"],
        workspace=workspace,
    )
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_grep", "name": "grep", "arguments": {"pattern": "alpha"}}
                ],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    register_glob_tool(tools)
    register_grep_tool(tools)
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await ChatLoop(runtime).send("coder", "Search files", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    failure = tool_failure("tool_not_allowed", "Tool not allowed: grep")
    assert json.loads(messages[2].content or "{}") == failure
    assert next(event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT).payload == {
        "tool_call": {"id": "call_grep", "index": 0, "name": "grep"},
        "result": failure,
    }


@pytest.mark.asyncio
async def test_same_turn_tool_calls_run_concurrently_and_persist_in_call_order(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["slow"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "slow", "arguments": {"value": "first"}},
                    {"id": "call_2", "name": "slow", "arguments": {"value": "second"}},
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    second_started = asyncio.Event()
    first_can_finish = asyncio.Event()

    async def slow_handler(context: ToolContext, arguments: ToolJsonObject) -> ToolJsonObject:
        if context.tool_call_id == "call_1":
            await second_started.wait()
            first_can_finish.set()
        else:
            second_started.set()
        return tool_success({"value": arguments["value"], "id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register("slow", "Slow tool.", {"type": "object"}, slow_handler)
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await ChatLoop(runtime).send("coder", "Run tools", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    result_events = [event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT]
    assert assistant.content == "Done"
    assert first_can_finish.is_set()
    assert [message.tool_call_id for message in messages if message.role == "tool"] == [
        "call_1",
        "call_2",
    ]
    assert [event.payload["tool_call"]["id"] for event in result_events] == ["call_2", "call_1"]
    assert [
        json.loads(message.content or "{}")["data"]["id"]
        for message in messages
        if message.role == "tool"
    ] == [
        "call_1",
        "call_2",
    ]


@pytest.mark.asyncio
async def test_same_tool_sibling_calls_run_in_parallel(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["same"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "same", "arguments": {}},
                    {"id": "call_2", "name": "same", "arguments": {}},
                ],
            },
            {"content": "Done", "tool_calls": None},
        ]
    )
    active_count = 0
    max_active_count = 0
    release = asyncio.Event()

    async def same_handler(context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        nonlocal active_count, max_active_count
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        if max_active_count == 2:
            release.set()
        await release.wait()
        active_count -= 1
        return tool_success({"id": context.tool_call_id})

    tools = ToolRegistry()
    tools.register("same", "Same tool.", {"type": "object"}, same_handler)
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    await ChatLoop(runtime).send("coder", "Run tools", session_id="session-one")

    assert max_active_count == 2


@pytest.mark.asyncio
async def test_tool_handler_exception_continues_with_failure_envelope(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["explode"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "explode", "arguments": {}}],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )

    def failing_handler(_context: ToolContext, _arguments: ToolJsonObject) -> ToolJsonObject:
        raise RuntimeError("boom")

    tools = ToolRegistry()
    tools.register("explode", "Explode.", {"type": "object"}, failing_handler)
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await ChatLoop(runtime).send("coder", "Run tool", session_id="session-one")

    run = next(iter(runtime.chat_runs._runs.values()))
    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert assistant.content == "Recovered"
    assert run.status == RunStatus.COMPLETED
    assert json.loads(messages[2].content or "{}") == tool_failure("tool_execution_error", "boom")
    assert next(event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT).payload == {
        "tool_call": {"id": "call_1", "index": 0, "name": "explode"},
        "result": tool_failure("tool_execution_error", "boom"),
    }


@pytest.mark.asyncio
async def test_legacy_dispatch_non_envelope_result_is_failure_envelope(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["legacy"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "legacy", "arguments": {"value": "input"}}],
            },
            {"content": "Recovered", "tool_calls": None},
        ]
    )
    runtime = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=cast(ToolRegistry, LegacyDispatchToolRegistry({"content": "not enveloped"})),
    )

    assistant = await ChatLoop(runtime).send("coder", "Run legacy", session_id="session-one")

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    failure = tool_failure(
        "invalid_tool_result",
        "Tool handler must return a valid result envelope: legacy",
    )
    assert assistant.content == "Recovered"
    assert json.loads(messages[2].content or "{}") == failure
    run = next(iter(runtime.chat_runs._runs.values()))
    assert next(event for event in run.events if event.type == TOOL_CALL_RESULT_EVENT).payload == {
        "tool_call": {"id": "call_1", "index": 0, "name": "legacy"},
        "result": failure,
    }


@pytest.mark.asyncio
async def test_max_tool_iteration_stop_raises_chat_error(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            }
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({}),
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    with pytest.raises(ChatError, match="maximum tool iterations"):
        await ChatLoop(runtime, max_tool_iterations=0).send(
            "coder",
            "Weather?",
            session_id="session-one",
        )

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert [message.role for message in messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_provider_errors_propagate_after_user_message_is_persisted(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/unknown-new-model", allowed_tools=["*"])
    adapter = StubAdapter([ProviderError("provider failed", retryable=False)])  # type: ignore[list-item]
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    with pytest.raises(ProviderError, match="provider failed"):
        await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    messages = runtime.chat_sessions.get("coder", "session-one").load()
    assert [message.role for message in messages] == ["user"]
    assert adapter.requests[0]["model_id"] == "unknown-new-model"


@pytest.mark.asyncio
async def test_empty_agent_model_raises_chat_error_before_persisting(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, provider_ids={"openai"})

    with pytest.raises(ChatError, match="no model set"):
        await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.chat_sessions.list("coder") == []


@pytest.mark.asyncio
async def test_chat_loop_uses_connection_from_agent(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        connection="openai:oauth",
        allowed_tools=["*"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id == "openai"
    assert runtime.adapter_connection_id == "openai:oauth"


@pytest.mark.asyncio
async def test_chat_loop_provider_comes_from_connection_id(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        connection="openrouter:api-key",
        allowed_tools=["*"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        provider_ids={"openai", "openrouter"},
    )

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id == "openrouter"
    assert runtime.adapter_connection_id == "openrouter:api-key"
    assert adapter.requests[0]["model_id"] == "gpt-5.2"


@pytest.mark.asyncio
async def test_chat_loop_empty_connection_falls_back_to_first_usable(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        connection="",
        allowed_tools=["*"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.provider_credentials = StubProviderCredentials({"openai:api-key"})

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id == "openai"
    assert runtime.adapter_connection_id == "openai:api-key"


@pytest.mark.asyncio
async def test_chat_loop_empty_connection_prefers_first_usable_in_provider_order(
    tmp_path: Path,
) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        connection="",
        allowed_tools=["*"],
    )
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.provider_credentials = StubProviderCredentials({"openai:oauth", "openai:api-key"})

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id == "openai"
    assert runtime.adapter_connection_id == "openai:oauth"


@pytest.mark.asyncio
async def test_missing_provider_raises_chat_error_before_adapter_request(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="missing/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, provider_ids={"openai"})

    with pytest.raises(ChatError, match="provider not found: missing"):
        await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert runtime.adapter_provider_id is None
    assert runtime.chat_sessions.list("coder") == []


@pytest.mark.asyncio
async def test_non_streaming_response_with_usage_produces_assistant_with_usage(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": "Hello",
                "reasoning": None,
                "tool_calls": None,
                "usage": {"input_tokens": 150, "output_tokens": 12},
            }
        ]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.usage == {"input_tokens": 150, "output_tokens": 12}
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    assert persisted[1].usage == {"input_tokens": 150, "output_tokens": 12}
    run = next(iter(runtime.chat_runs._runs.values()))
    completed = [event for event in run.events if event.type == "run_completed"]
    assert len(completed) == 1
    assert completed[0].payload == {
        "status": "completed",
        "usage": {"input_tokens": 150, "output_tokens": 12},
    }


@pytest.mark.asyncio
async def test_streaming_response_with_usage_delta_produces_assistant_with_usage(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Hello"},
                {"type": "usage", "input_tokens": 200, "output_tokens": 25},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    assert assistant.content == "Hello"
    assert assistant.usage == {"input_tokens": 200, "output_tokens": 25}
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    assert persisted[1].usage == {"input_tokens": 200, "output_tokens": 25}
    run = next(iter(runtime.chat_runs._runs.values()))
    completed = [event for event in run.events if event.type == "run_completed"]
    assert len(completed) == 1
    assert completed[0].payload == {
        "status": "completed",
        "usage": {"input_tokens": 200, "output_tokens": 25},
    }


@pytest.mark.asyncio
async def test_response_without_usage_applies_estimation(
    tmp_path: Path,
) -> None:
    """When the provider doesn't supply usage, the chat loop estimates tokens."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello world", "reasoning": None, "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.usage is not None
    assert assistant.usage["estimated"] is True
    assert assistant.usage == {
        "input_tokens": assistant.usage["input_tokens"],
        "output_tokens": assistant.usage["output_tokens"],
        "estimated": True,
    }
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    assert persisted[1].usage is not None
    assert persisted[1].usage["estimated"] is True
    run = next(iter(runtime.chat_runs._runs.values()))
    completed = [event for event in run.events if event.type == "run_completed"]
    assert len(completed) == 1
    assert completed[0].payload["usage"]["estimated"] is True


def _write_test_skill(tmp_path: Path, name: str) -> Path:
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        f"""---
name: {name}
description: Test skill.
---

# {name}

Use this skill content.
""",
        encoding="utf-8",
    )
    return skill_file


@pytest.mark.asyncio
async def test_estimation_computes_from_request_message_contents(
    tmp_path: Path,
) -> None:
    """Estimation derives input tokens from request messages and output from response content."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello world", "reasoning": None, "tool_calls": None}])
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    # Reconstruct expected estimation from the actual request messages
    request_messages = adapter.requests[0]["messages"]
    input_text = "".join(msg.get("content", "") or "" for msg in request_messages)
    expected_input, _ = estimate_tokens(input_text)
    expected_output, _ = estimate_tokens("Hello world")

    assert assistant.usage == {
        "input_tokens": expected_input,
        "output_tokens": expected_output,
        "estimated": True,
    }


@pytest.mark.asyncio
async def test_provider_usage_preserved_without_estimated_flag(
    tmp_path: Path,
) -> None:
    """When the provider supplies usage, it is kept as-is with no estimated flag."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": "Hello",
                "reasoning": None,
                "tool_calls": None,
                "usage": {"input_tokens": 150, "output_tokens": 12},
            }
        ]
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

    assert assistant.usage == {"input_tokens": 150, "output_tokens": 12}
    assert "estimated" not in assistant.usage
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    assert persisted[1].usage == {"input_tokens": 150, "output_tokens": 12}
    assert "estimated" not in persisted[1].usage


@pytest.mark.asyncio
async def test_streaming_without_usage_applies_estimation(
    tmp_path: Path,
) -> None:
    """Streaming mode also applies estimation when no usage delta is received."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Hello"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)

    assistant = await ChatLoop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    assert assistant.content == "Hello"
    assert assistant.usage is not None
    assert assistant.usage["estimated"] is True
    assert isinstance(assistant.usage["input_tokens"], int)
    assert isinstance(assistant.usage["output_tokens"], int)
    run = next(iter(runtime.chat_runs._runs.values()))
    completed = [event for event in run.events if event.type == "run_completed"]
    assert len(completed) == 1
    assert completed[0].payload["usage"]["estimated"] is True


@pytest.mark.asyncio
async def test_estimation_with_tool_calls_in_history(
    tmp_path: Path,
) -> None:
    """Estimation includes tool call content from previous turns in input tokens."""
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Sunny", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
    )
    runtime = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await ChatLoop(runtime).send("coder", "Weather?", session_id="session-one")

    assert assistant.content == "Sunny"
    assert assistant.usage is not None
    assert assistant.usage["estimated"] is True
    # The second request includes previous assistant + tool messages, so
    # input_tokens should be larger than the first request alone.
    assert assistant.usage["input_tokens"] > 0


class TestMessageToRequestDict:
    """Verify _message_to_request_dict strips vBot-internal fields from provider requests."""

    def test_strips_usage_from_assistant_message(self):
        """Usage must not be sent to providers in follow-up request messages."""
        from core.chat.chat import _message_to_request_dict

        message = ChatMessage.assistant(
            model="openai/gpt-4",
            content="Hello",
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        result = _message_to_request_dict(message)

        assert "usage" not in result
        assert "reasoning" not in result
        assert "reasoning_meta" not in result
        assert result["content"] == "Hello"

    def test_preserves_usage_on_non_assistant_messages(self):
        """User and tool messages never have usage, but the function should not strip it."""
        from core.chat.chat import _message_to_request_dict

        message = ChatMessage.user("What is the weather?")
        result = _message_to_request_dict(message)

        assert "usage" not in result
        assert result["content"] == "What is the weather?"
