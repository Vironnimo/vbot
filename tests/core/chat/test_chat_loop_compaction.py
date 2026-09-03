"""Chat-loop tests grouped by compaction."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import (
    ChatLoop,
    ChatMessage,
    ToolCall,
)
from core.chat.chat import (
    SEEN_SKILLS_META_KEY,
    RequestBuildInputs,
    _RequestState,
    _restore_in_run_tool_result_content,
    _RunRequest,
)
from core.chat.continuation import (
    ContinuationTracker,
    inject_continuation_reminder,
    recover_continuation,
    render_continuation_reminder,
)
from core.chat.messages import HISTORY_COMPACTION_GUIDANCE
from core.compaction import (
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    TOOL_RESULT_COMPACTED_FIELD,
    CompactionService,
)
from core.compaction.compaction import (
    COMPACTION_REFERENCE_PREFIX,
    COMPACTION_SUMMARY_END_MARKER,
)
from core.prompts.pinned_context import (
    PINNED_MEMORY_FILES_META_KEY,
    PINNED_SKILL_CATALOG_META_KEY,
    PINNED_SOUL_CONTEXT_META_KEY,
    PINNED_WORKING_PROJECT_CONTEXT_META_KEY,
    pinned_memory_files,
    pinned_skill_catalog,
    pinned_soul_context,
)
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.runs import (
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    COMPACTION_STARTED_EVENT,
    Run,
)
from core.tools import (
    HISTORY_TOOL_NAME,
    ToolRegistry,
    register_history_tool,
    tool_success,
)
from core.utils.tokens import estimate_request_input_tokens
from tests.core.chat.chat_loop_support import (
    ClosingStubAdapter,
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubModels,
    StubProject,
    StubProjects,
    StubProviderCredentials,
    StubRuntime,
    StubSkill,
    StubSkills,
    StubStorage,
    build_chat_loop,
    persisted_roles,
    session_address,
)

JsonObject = dict[str, Any]
_ASYNC_COORDINATION_TIMEOUT_SECONDS = 10.0


class _RealCompactionAdapter(StubAdapter):
    """Exercise real Agent and Compaction requests through one recording adapter."""

    def __init__(self, responses: list[Any], *, summaries: list[str]) -> None:
        super().__init__(
            responses,
            stream_responses=[
                [
                    {"type": "content_delta", "text": summary},
                    {"type": "finish", "reason": "stop"},
                ]
                for summary in summaries
            ],
        )
        self.events: list[str] = []

    async def send(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> JsonObject:
        self.events.append("agent")
        return await super().send(messages, model_id=model_id, **kwargs)

    async def stream(
        self,
        messages: list[JsonObject],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> Any:
        self.events.append("compaction")
        async for delta in super().stream(messages, model_id=model_id, **kwargs):
            yield delta

    def normalize_response(
        self,
        response: JsonObject,
        *,
        model_id: str | None = None,
    ) -> JsonObject:
        del model_id
        if "choices" not in response:
            return response
        choices = cast(list[JsonObject], response["choices"])
        message = cast(JsonObject, choices[0]["message"])
        return {"content": message.get("content"), "usage": response.get("usage")}


class _RealCompactionStorage(StubStorage):
    def __init__(self, compaction_settings: JsonObject, *, data_dir: Path) -> None:
        super().__init__(compaction_settings, data_dir=data_dir)
        self.prompt_fragment_reads: list[str] = []

    def read_prompt_fragment(self, name: str) -> str:
        self.prompt_fragment_reads.append(name)
        assert name in {
            "compaction.md",
            "compaction-manual.md",
            "compaction-continuation.md",
            "compaction-continuation-manual.md",
        }
        return "Summarize the earlier Context and preserve unfinished work."


def test_context_window_uses_the_selected_provider_connection(tmp_path: Path) -> None:
    model_key = ("openai", "gpt-5.4")
    models = StubModels(
        {model_key: 272_000},
        connection_context_windows={model_key: {"api-key": 1_050_000, "subscription": 272_000}},
    )

    api_agent = StubAgent(id="api", model="openai/gpt-5.4", allowed_tools=["*"])
    api_data_dir = tmp_path / "api"
    api_data_dir.mkdir()
    api_runtime = StubRuntime(
        data_dir=api_data_dir,
        agent=api_agent,
        adapter=StubAdapter([]),
        models=models,
    )
    api_loop = build_chat_loop(api_runtime)
    assert api_loop.resolve_context_window(api_agent) == 1_050_000
    subscription_target = SimpleNamespace(
        provider_id="openai",
        connection_id="openai:subscription",
        model_id="gpt-5.4",
    )
    assert api_loop.resolve_context_window(api_agent, cast(Any, subscription_target)) == 272_000

    subscription_agent = StubAgent(
        id="subscription",
        model="openai/gpt-5.4::subscription",
        allowed_tools=["*"],
    )
    subscription_data_dir = tmp_path / "subscription"
    subscription_data_dir.mkdir()
    subscription_runtime = StubRuntime(
        data_dir=subscription_data_dir,
        agent=subscription_agent,
        adapter=StubAdapter([]),
        adapters_by_connection={"openai:subscription": StubAdapter([])},
        models=models,
    )
    assert (
        build_chat_loop(subscription_runtime).resolve_context_window(subscription_agent) == 272_000
    )


class _BlockingOnceCompactionService:
    """Pause one successful Compaction so a concurrent Session append can race it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.attempted = False
        self.checks = 0

    def has_new_compactable_context(
        self,
        _messages: list[ChatMessage],
        _settings: Any,
    ) -> bool:
        return True

    def should_auto_compact(
        self,
        _input_tokens: int,
        _context_window: int,
        _threshold: float,
        **_kwargs: Any,
    ) -> bool:
        self.checks += 1
        return self.checks == 2 and not self.attempted

    async def compact(
        self,
        messages: list[ChatMessage],
        **_kwargs: Any,
    ) -> ChatMessage:
        self.attempted = True
        self.started.set()
        await self.release.wait()
        return ChatMessage.compaction_checkpoint(
            summary="Compacted snapshot.",
            projection=messages,
            compacted_token_count=20,
        )


async def _maybe_auto_compact(
    loop: ChatLoop,
    agent: Any,
    adapter: Any,
    model_id: str,
    session: Any,
    messages: list[JsonObject],
    usage: JsonObject | None,
    *,
    run: Run,
    continuation_tracker: ContinuationTracker | None = None,
    continuation_reminder: str | None = None,
) -> list[JsonObject]:
    """Build the same Run context used by production before probing Compaction."""
    del agent, model_id
    prior_continuation = await recover_continuation(session) if continuation_reminder else None
    context = await loop._create_run_execution_context(
        run,
        _RunRequest(content="test"),
        session=session,
        prior_continuation=prior_continuation,
        continuation_reminder=continuation_reminder,
        continuation_tracker=continuation_tracker,
    )
    assert context.primary_target.adapter is adapter
    context.request_state = _RequestState(messages, [], (), ())
    state = await loop._compaction_runs.maybe_auto_compact_state(
        context,
        context.primary_target,
        usage,
    )
    return state.messages


def test_compaction_latest_checkpoint_helper_returns_last_checkpoint() -> None:
    from core.chat.chat import _latest_compaction_checkpoint

    first_user = ChatMessage.user("first")
    second_user = ChatMessage.user("second")
    first_checkpoint = ChatMessage.compaction_checkpoint(
        summary="checkpoint one",
        projection=[first_user],
        compacted_token_count=10,
    )
    second_checkpoint = ChatMessage.compaction_checkpoint(
        summary="checkpoint two",
        projection=[second_user],
        compacted_token_count=20,
    )

    latest = _latest_compaction_checkpoint(
        [first_user, first_checkpoint, second_user, second_checkpoint]
    )

    assert latest is second_checkpoint


def test_compaction_latest_checkpoint_helper_returns_none_when_absent() -> None:
    from core.chat.chat import _latest_compaction_checkpoint

    assert _latest_compaction_checkpoint([ChatMessage.user("only")]) is None


def test_effective_compaction_messages_use_checkpoint_projection() -> None:
    from core.chat.messages import _effective_compaction_messages

    first = ChatMessage.user("first")
    second = ChatMessage.assistant(model="openai/gpt-5.2", content="second")
    third = ChatMessage.user("third")
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="s", projection=[second, third], compacted_token_count=1
    )

    effective = _effective_compaction_messages([first, second, third, checkpoint])

    assert [message.role for message in effective] == ["note", "assistant", "user"]
    assert effective[1:] == [second, third]


def test_effective_compaction_messages_append_newer_messages() -> None:
    from core.chat.messages import _effective_compaction_messages

    older = ChatMessage.user("older")
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="s", projection=[older], compacted_token_count=1
    )
    newer = ChatMessage.user("newer")

    effective = _effective_compaction_messages([older, checkpoint, newer])

    assert [message.role for message in effective] == ["note", "user", "user"]
    assert effective[1:] == [older, newer]


def test_compaction_build_request_messages_without_checkpoint_keeps_existing_path(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    session.append(ChatMessage.assistant(model=agent.model, content="Hello"))

    request_messages = asyncio.run(build_chat_loop(runtime)._build_request_messages(agent, session))

    assert [message["role"] for message in request_messages] == ["system", "user", "assistant"]
    assert request_messages[1]["content"] == "Hi"
    assert request_messages[2]["content"] == "Hello"


def test_compaction_build_request_messages_with_checkpoint_uses_summary_and_tail_only(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")

    session.append(ChatMessage.user("Old question"))
    session.append(ChatMessage.assistant(model=agent.model, content="Old answer"))
    tail_user = ChatMessage.user("Tail question")
    tail_assistant = ChatMessage.assistant(model=agent.model, content="Tail answer")
    session.append(tail_user)
    session.append(tail_assistant)
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted historical context.",
            projection=[tail_user, tail_assistant],
            compacted_token_count=123,
        )
    )

    request_messages = asyncio.run(build_chat_loop(runtime)._build_request_messages(agent, session))
    request_text = "\n".join(message.get("content", "") or "" for message in request_messages)

    assert [message["role"] for message in request_messages] == [
        "system",
        "user",
        "user",
        "assistant",
    ]
    assert request_messages[1]["content"] == (
        "<system-reminder>\nCompacted historical context.\n</system-reminder>"
    )
    assert request_messages[2]["content"] == "Tail question"
    assert request_messages[3]["content"] == "Tail answer"
    assert "Old question" not in request_text
    assert all(message["role"] != "compaction_checkpoint" for message in request_messages)


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_skips_when_auto_disabled(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="unused",
        projection=[ChatMessage.user("unused")],
        compacted_token_count=1,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": False,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    messages = await build_chat_loop(runtime)._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    loop = build_chat_loop(
        runtime,
        compaction_service=cast(Any, compaction_service),
    )
    result = await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert result == messages
    assert compaction_service.should_auto_calls == []
    assert compaction_service.compact_calls == []


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_skips_when_threshold_not_reached(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2::subscription", allowed_tools=["*"])
    adapter = StubAdapter([])
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="unused",
        projection=[ChatMessage.user("unused")],
        compacted_token_count=1,
    )
    compaction_service = StubCompactionService(should_auto=False, checkpoint=checkpoint)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.95,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    messages = await build_chat_loop(runtime)._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    loop = build_chat_loop(
        runtime,
        compaction_service=cast(Any, compaction_service),
    )
    result = await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 20},
        run=run,
    )

    assert result == messages
    assert compaction_service.should_auto_calls == [(20, 100, 0.95)]
    assert compaction_service.compact_calls == []


@pytest.mark.asyncio
async def test_compaction_uses_larger_of_provider_anchor_and_wire_estimate(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])

    class HighEstimateAdapter(StubAdapter):
        def estimate_request_input_tokens(
            self,
            _messages: list[JsonObject],
            *,
            model_id: str,
            tools: list[JsonObject] | None = None,
        ) -> int:
            del model_id, tools
            return 95

    adapter = HighEstimateAdapter([])
    compaction_service = StubCompactionService(
        should_auto=False,
        estimated_tokens=95,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    messages = await build_chat_loop(runtime)._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        build_chat_loop(runtime, compaction_service=cast(Any, compaction_service)),
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 20},
        run=run,
    )

    assert compaction_service.should_auto_calls == [(95, 100, 0.8)]
    assert compaction_service.estimate_calls == []


@pytest.mark.asyncio
async def test_compaction_preflight_uses_durable_projection_instead_of_generic_request_estimate(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])

    class WireEstimateAdapter(StubAdapter):
        def estimate_request_input_tokens(
            self,
            _messages: list[JsonObject],
            *,
            model_id: str,
            tools: list[JsonObject] | None = None,
        ) -> int:
            del model_id, tools
            return 95

    adapter = WireEstimateAdapter([])
    compaction_service = StubCompactionService(should_auto=False)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Earlier"))
    session.append(
        ChatMessage.assistant(
            model=agent.model,
            content="x" * 50_000,
            usage={"input_tokens": 20, "output_tokens": 0},
        )
    )
    session.append(ChatMessage.user("Current"))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage=None,
        run=run,
    )

    generic_tokens, _ = estimate_request_input_tokens(messages)
    assert generic_tokens > 95
    assert compaction_service.should_auto_calls == [(95, 100, 0.8)]


@pytest.mark.asyncio
async def test_compaction_records_post_projection_with_selected_wire_estimator(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])

    class SequencedEstimateAdapter(StubAdapter):
        def __init__(self) -> None:
            super().__init__([])
            self.estimates = iter((95, 37))

        def estimate_request_input_tokens(
            self,
            _messages: list[JsonObject],
            *,
            model_id: str,
            tools: list[JsonObject] | None = None,
        ) -> int:
            del model_id, tools
            return next(self.estimates)

    adapter = SequencedEstimateAdapter()
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted snapshot.",
        projection=[ChatMessage.user("Tail")],
        compacted_token_count=50,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Head"))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    persisted_checkpoint = session.load()[-1]
    assert persisted_checkpoint.role == "compaction_checkpoint"
    assert persisted_checkpoint.usage is not None
    assert persisted_checkpoint.usage["context_tokens_before"] == 95
    assert persisted_checkpoint.usage["context_tokens_after"] == 37
    started_event = next(event for event in run.events if event.type == COMPACTION_STARTED_EVENT)
    assert started_event.payload["context_tokens_before"] == 95
    assert started_event.payload["context_usage"] == {
        "tokens": 95,
        "estimated": True,
        "provider_input_tokens": 90,
        "provider_output_tokens": 0,
    }


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_skips_without_new_compactable_context(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    compaction_service = StubCompactionService(
        should_auto=True,
        has_compactable_context=False,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Keep working in this same turn"))
    messages = await build_chat_loop(runtime)._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    result = await _maybe_auto_compact(
        build_chat_loop(
            runtime,
            compaction_service=cast(Any, compaction_service),
        ),
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert result == messages
    assert len(compaction_service.compactable_context_calls) == 1
    assert compaction_service.should_auto_calls == [(90, 100, 0.8)]
    assert compaction_service.compact_calls == []


@pytest.mark.asyncio
async def test_summary_tail_waits_until_a_loaded_skill_result_is_consumed(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Use the document workflow"))
    session.append(
        ChatMessage.assistant(
            model=agent.model,
            content=None,
            tool_calls=[ToolCall(id="call-skill", name="skill", arguments={"name": "docx"})],
        )
    )
    session.append(
        ChatMessage.tool(
            tool_call_id="call-skill",
            name="skill",
            content=json.dumps(
                tool_success({"name": "docx", "status": "loaded", "content": "Instructions"}),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    )
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted after consumption.",
        projection=[ChatMessage.user("Tail")],
        compacted_token_count=20,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)

    first = await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=Run(run_id="run-1", agent_id=agent.id, session_id=session.id),
    )

    assert first == messages
    assert compaction_service.compact_calls == []
    assert compaction_service.compactable_context_calls == []

    session.append(ChatMessage.assistant(model=agent.model, content="Skill result consumed"))
    consumed_messages = await loop._build_request_messages(agent, session)
    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        consumed_messages,
        usage={"input_tokens": 90},
        run=Run(run_id="run-2", agent_id=agent.id, session_id=session.id),
    )

    assert len(compaction_service.compact_calls) == 1


@pytest.mark.asyncio
async def test_compaction_resolves_floor_for_null_window_model(tmp_path: Path) -> None:
    # A model with no context window (None) must still drive auto-compaction:
    # the read-side default chain resolves the global floor so should_auto_compact
    # is called with a usable positive window instead of silently disabling.
    from core.providers.providers import GLOBAL_CONTEXT_WINDOW_FLOOR

    agent = StubAgent(id="coder", model="openai/gpt-5.2::subscription", allowed_tools=["*"])
    adapter = StubAdapter([])
    compaction_service = StubCompactionService(should_auto=False)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): None}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    messages = await build_chat_loop(runtime)._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    loop = build_chat_loop(
        runtime,
        compaction_service=cast(Any, compaction_service),
    )
    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 20},
        run=run,
    )

    assert compaction_service.should_auto_calls == [(20, GLOBAL_CONTEXT_WINDOW_FLOOR, 0.8)]


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_appends_checkpoint_and_rebuilds_messages(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(
        should_auto=True,
        checkpoint=checkpoint,
    )
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
    affinity_before = runtime.chat_sessions.prompt_cache_affinity_id(
        session_address("coder", session.id)
    )

    compaction_logger = logging.getLogger("vbot.compaction.coordination")
    compaction_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level("INFO", logger=compaction_logger.name):
            rebuilt = await _maybe_auto_compact(
                loop,
                agent,
                adapter,
                "gpt-5.2",
                session,
                messages,
                usage={"input_tokens": 90},
                run=run,
            )
    finally:
        compaction_logger.removeHandler(caplog.handler)

    log_messages = [record.getMessage() for record in caplog.records]
    triggered_line = next(
        message for message in log_messages if message.startswith("Auto-compaction triggered")
    )
    assert "input_tokens=90" in triggered_line
    assert "context_window=100" in triggered_line
    completed_line = next(
        message for message in log_messages if message.startswith("Auto-compaction completed")
    )
    assert "session=session-one" in completed_line
    assert "estimated_tokens_after=" in completed_line
    assert persisted_roles(session.load()) == [
        "user",
        "assistant",
        "compaction_checkpoint",
    ]
    assert (
        runtime.chat_sessions.prompt_cache_affinity_id(session_address("coder", session.id))
        != affinity_before
    )
    assert len(compaction_service.compact_calls) == 1
    assert compaction_service.compact_calls[0]["summary_model_id"] == "gpt-5.2"
    assert compaction_service.compact_calls[0]["summary_adapter"] is adapter
    assert compaction_service.compact_calls[0]["request_messages"] == messages
    assert compaction_service.compact_calls[0]["summary_temperature"] is None
    assert compaction_service.compact_calls[0]["active_temperature"] is None
    assert (
        compaction_service.compact_calls[0]["minimum_reclaim_tokens"]
        == MIN_AUTO_COMPACTION_RECLAIM_TOKENS
    )
    assert [message["role"] for message in rebuilt] == ["system", "user", "user", "assistant"]
    reminder = rebuilt[1]["content"]
    assert reminder.startswith("<system-reminder>\n")
    assert reminder.endswith("\n</system-reminder>")
    assert "Compacted tail context." in reminder
    assert rebuilt[2]["content"] == "Tail user"
    assert rebuilt[3]["content"] == "Tail assistant"
    post_compaction_tools = runtime.system_prompts.provider_tool_definitions(
        agent,
        session_tool_grants=(HISTORY_TOOL_NAME,),
    )
    expected_context_tokens_after, _ = estimate_request_input_tokens(
        rebuilt,
        post_compaction_tools,
    )
    compaction_events = [
        event
        for event in run.events
        if event.type in {COMPACTION_STARTED_EVENT, COMPACTION_COMPLETED_EVENT}
    ]
    assert [event.type for event in compaction_events] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_COMPLETED_EVENT,
    ]
    assert compaction_events[0].payload == {
        "context_tokens_before": 90,
        "context_usage": {
            "tokens": 90,
            "estimated": False,
            "provider_input_tokens": 90,
            "provider_output_tokens": 0,
        },
    }
    compaction_event = next(
        event for event in run.events if event.type == COMPACTION_COMPLETED_EVENT
    )
    assert compaction_event.payload["checkpoint"] == 1
    assert compaction_event.payload["checkpoint_id"] == checkpoint.id
    assert compaction_event.payload["history_available"] is True
    assert compaction_event.payload["context_tokens_before"] == 90
    assert compaction_event.payload["context_tokens_after"] == expected_context_tokens_after
    assert compaction_event.payload["context_usage"] == {
        "tokens": expected_context_tokens_after,
        "estimated": True,
    }
    final_usage = dict(session.load()[-1].usage or {})
    compaction_duration_ms = final_usage.pop("compaction_duration_ms")
    assert final_usage == {
        "compacted_token_count": 42,
        "context_tokens_before": 90,
        "context_tokens_after": expected_context_tokens_after,
    }
    assert isinstance(compaction_duration_ms, int)
    assert compaction_duration_ms >= 0


@pytest.mark.asyncio
async def test_compaction_resolves_model_recommended_temperatures_for_both_targets(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="ollama-cloud/glm-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": "ollama-cloud/qwen3",
            }
        ),
        models=StubModels(
            {("ollama-cloud", "glm-5.2"): 100, ("ollama-cloud", "qwen3"): 100},
            recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
        ),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Tail user"))
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "glm-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert compaction_service.compact_calls[0]["summary_temperature"] is None
    assert compaction_service.compact_calls[0]["active_temperature"] == 1.0


@pytest.mark.asyncio
async def test_compaction_allows_repeated_automatic_checkpoints_in_one_run(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    tail_assistant = ChatMessage.assistant(model=agent.model, content="Tail assistant")
    session.append(tail_user)
    session.append(tail_assistant)
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=[tail_user, tail_assistant],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)
    context = await loop._create_run_execution_context(
        run,
        _RunRequest(content="test"),
        session=session,
        prior_continuation=None,
        continuation_reminder=None,
        continuation_tracker=None,
    )
    context.request_state = await loop.build_request_state(
        agent,
        session,
        inputs=RequestBuildInputs.from_context(context, context.primary_target),
    )

    requested_compactions = 5
    affinity_epochs = [context.prompt_cache_affinity_id]
    for _ in range(requested_compactions):
        context.request_state = await loop._compaction_runs.maybe_auto_compact_state(
            context,
            context.primary_target,
            usage={"input_tokens": 90},
        )
        affinity_epochs.append(context.prompt_cache_affinity_id)

    assert len(compaction_service.compact_calls) == requested_compactions
    assert persisted_roles(session.load()).count("compaction_checkpoint") == requested_compactions
    assert len(set(affinity_epochs)) == requested_compactions + 1
    assert context.prompt_cache_affinity_id == runtime.chat_sessions.prompt_cache_affinity_id(
        session_address("coder", session.id)
    )


@pytest.mark.asyncio
async def test_real_compaction_repeats_between_complete_tool_iterations(
    tmp_path: Path,
) -> None:
    current_user = "CURRENT_USER_MARKER " + ("current task " * 5_000)
    second_payload = "SECOND_TOOL_PAYLOAD " + ("beta " * 8_000)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["word_count"])
    adapter = _RealCompactionAdapter(
        [
            {
                "content": None,
                "usage": {"input_tokens": 50_000, "output_tokens": 10},
                "tool_calls": [
                    {
                        "id": "call-one",
                        "name": "word_count",
                        "arguments": {"text": "alpha words"},
                    }
                ],
            },
            {
                "content": None,
                "usage": {"input_tokens": 50_000, "output_tokens": 10},
                "tool_calls": [
                    {
                        "id": "call-two",
                        "name": "word_count",
                        "arguments": {"text": second_payload},
                    }
                ],
            },
            {
                "content": "AUTO_DONE",
                "usage": {"input_tokens": 50_000, "output_tokens": 2},
                "tool_calls": None,
            },
        ],
        summaries=["SUMMARY ONE", "SUMMARY TWO", "SUMMARY THREE"],
    )
    tools = ToolRegistry()
    tools.register(
        "word_count",
        "Count words.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        lambda _context, arguments: tool_success({"words": len(str(arguments["text"]).split())}),
    )
    storage = _RealCompactionStorage(
        {
            "enabled": True,
            "trigger": {"type": "input_tokens", "tokens": 40_000},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 1_000,
                "summary_model": None,
            },
        },
        data_dir=tmp_path,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        storage=storage,
        models=StubModels({("openai", "gpt-5.2"): 1_000_000}),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD_CONTEXT_MARKER " + ("old context " * 5_000)))
    session.append(ChatMessage.assistant(model=agent.model, content="old answer " * 5_000))

    assistant = await build_chat_loop(
        runtime,
        compaction_service=CompactionService(),
    ).send("coder", current_user, session_id=session.id)

    persisted = session.load()
    checkpoints = [message for message in persisted if message.role == "compaction_checkpoint"]
    assert assistant.content == "AUTO_DONE"
    assert adapter.events == [
        "agent",
        "compaction",
        "agent",
        "compaction",
        "agent",
        "compaction",
    ]
    assert storage.prompt_fragment_reads == ["compaction.md"] * 3
    assert len(checkpoints) == 3
    assert persisted_roles(persisted)[-8:] == [
        "assistant",
        "tool",
        "compaction_checkpoint",
        "assistant",
        "tool",
        "compaction_checkpoint",
        "assistant",
        "compaction_checkpoint",
    ]

    for ordinal, checkpoint_message in enumerate(checkpoints, start=1):
        projection = checkpoint_message.projection
        assert projection is not None
        assert (
            sum(
                COMPACTION_SUMMARY_END_MARKER in str(message.get("content") or "")
                for message in projection
            )
            == 1
        )
        summary_content = next(
            str(message.get("content") or "")
            for message in projection
            if COMPACTION_SUMMARY_END_MARKER in str(message.get("content") or "")
        )
        assert summary_content.endswith(COMPACTION_SUMMARY_END_MARKER)
        assert summary_content.index(HISTORY_COMPACTION_GUIDANCE.format(ordinal=ordinal)) < (
            summary_content.index(COMPACTION_SUMMARY_END_MARKER)
        )
        for index, message in enumerate(projection):
            if message["role"] != "tool":
                continue
            carrier = projection[index - 1]
            assert carrier["role"] == "assistant"
            assert message["tool_call_id"] in {call["id"] for call in carrier["tool_calls"]}

    compaction_requests = [json.dumps(call["messages"]) for call in adapter.stream_requests]
    assert all("<retained_tail>" not in request for request in compaction_requests)
    assert "CURRENT_USER_MARKER" not in compaction_requests[0]
    assert "SECOND_TOOL_PAYLOAD" not in compaction_requests[1]
    assert "SECOND_TOOL_PAYLOAD" not in compaction_requests[2]

    third_agent_request = json.dumps(adapter.requests[2]["messages"])
    assert "CURRENT_USER_MARKER" not in third_agent_request
    assert "SECOND_TOOL_PAYLOAD" in third_agent_request
    assert "continue that iteration normally" in third_agent_request
    assert [message["role"] for message in adapter.requests[2]["messages"]][-3:] == [
        "user",
        "assistant",
        "tool",
    ]
    final_projection = json.dumps(checkpoints[-1].projection)
    assert "SUMMARY THREE" in final_projection
    assert "SUMMARY ONE" not in final_projection
    assert "SUMMARY TWO" not in final_projection


@pytest.mark.asyncio
async def test_continuation_compacts_before_first_request_and_after_complete_tool_results(
    tmp_path: Path,
) -> None:
    current_user = "CURRENT_CONTINUATION_USER " + ("current work " * 5_000)
    tool_payload = "alpha beta " * 5_000
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["word_count"])
    adapter = _RealCompactionAdapter(
        [
            {
                "content": None,
                "usage": {"input_tokens": 50_000, "output_tokens": 10},
                "tool_calls": [
                    {
                        "id": "call-one",
                        "name": "word_count",
                        "arguments": {"text": tool_payload},
                    }
                ],
            },
            {
                "content": "CONTINUATION_DONE",
                "usage": {"input_tokens": 50_000, "output_tokens": 2},
                "tool_calls": None,
            },
        ],
        summaries=["PREFLIGHT CHECKPOINT", "TOOL CHECKPOINT"],
    )
    tools = ToolRegistry()
    tools.register(
        "word_count",
        "Count words.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        lambda _context, arguments: tool_success({"words": len(str(arguments["text"]).split())}),
    )
    storage = _RealCompactionStorage(
        {
            "enabled": True,
            "trigger": {"type": "input_tokens", "tokens": 1},
            "strategy": {"type": "continuation"},
        },
        data_dir=tmp_path,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        storage=storage,
        models=StubModels({("openai", "gpt-5.2"): 1_000_000}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD CONTEXT " + ("old work " * 5_000)))
    session.append(ChatMessage.assistant(model=agent.model, content="old answer " * 5_000))

    assistant = await build_chat_loop(
        runtime,
        compaction_service=CompactionService(),
    ).send("coder", current_user, session_id=session.id)

    assert assistant.content == "CONTINUATION_DONE"
    assert adapter.events == ["compaction", "agent", "compaction", "agent"]
    assert storage.prompt_fragment_reads == ["compaction-continuation.md"] * 2
    assert len(adapter.stream_requests) == 2
    assert current_user in json.dumps(adapter.stream_requests[0]["messages"])
    second_compaction_roles = [
        message["role"] for message in adapter.stream_requests[1]["messages"]
    ]
    assert second_compaction_roles[-3:] == ["assistant", "tool", "user"]
    checkpoints = [message for message in session.load() if message.role == "compaction_checkpoint"]
    assert [message.content for message in checkpoints] == [
        "PREFLIGHT CHECKPOINT",
        "TOOL CHECKPOINT",
    ]


def test_compaction_does_not_restore_rich_content_for_aged_tool_result() -> None:
    call_id = "call-image"
    aged_content = json.dumps(
        {
            TOOL_RESULT_COMPACTED_FIELD: True,
            "tool": "read",
            "original_chars": 50_000,
            "outcome": {"ok": True},
        }
    )
    rebuilt = [{"role": "tool", "tool_call_id": call_id, "content": aged_content}]
    live = [
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": '{"ok":true}',
            TOOL_RESULT_CONTENT_BLOCKS_FIELD: [{"type": "text", "text": "rich"}],
        }
    ]

    restored = _restore_in_run_tool_result_content(rebuilt, live)

    assert TOOL_RESULT_CONTENT_BLOCKS_FIELD not in restored[0]


@pytest.mark.asyncio
async def test_final_assistant_compaction_activates_history_on_next_run(tmp_path: Path) -> None:
    class CompactOnce:
        def __init__(self) -> None:
            self.compacted = False
            self.checks = 0

        def estimate_messages_tokens(self, _messages: list[JsonObject]) -> int:
            return 90

        def has_new_compactable_context(
            self,
            _messages: list[ChatMessage],
            _settings: Any,
        ) -> bool:
            return True

        def should_auto_compact(
            self,
            _input_tokens: int,
            _context_window: int,
            _threshold: float,
            **_kwargs: Any,
        ) -> bool:
            self.checks += 1
            return self.checks == 2 and not self.compacted

        async def compact(self, messages: list[ChatMessage], **_kwargs: Any) -> ChatMessage:
            self.compacted = True
            return ChatMessage.compaction_checkpoint(
                summary="Compacted finished turn.",
                projection=messages[-2:],
                compacted_token_count=20,
            )

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = StubAdapter(
        [
            {
                "content": "First answer",
                "reasoning": "Provider-owned final-turn reasoning",
                "reasoning_meta": {"encrypted_content": "opaque-final-turn"},
                "tool_calls": None,
            },
            {"content": "Second answer", "tool_calls": None},
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, CompactOnce()))

    await loop.send("coder", "First", session_id="session-one")
    session = runtime.chat_sessions.get(session_address("coder", "session-one"))
    checkpoint = next(
        message for message in session.load() if message.role == "compaction_checkpoint"
    )
    post_compaction_state = await loop.build_request_state(
        agent, session, inputs=RequestBuildInputs()
    )
    expected_context_tokens_after, _ = estimate_request_input_tokens(
        post_compaction_state.messages,
        post_compaction_state.tools,
    )

    assert checkpoint.usage is not None
    assert checkpoint.usage["context_tokens_after"] == expected_context_tokens_after
    messages_only_tokens, _ = estimate_request_input_tokens(post_compaction_state.messages)
    assert expected_context_tokens_after > messages_only_tokens

    await loop.send("coder", "Second", session_id="session-one")

    first_names = [tool["name"] for tool in adapter.requests[0]["kwargs"]["tools"]]
    second_names = [tool["name"] for tool in adapter.requests[1]["kwargs"]["tools"]]
    assert HISTORY_TOOL_NAME not in first_names
    assert second_names == [HISTORY_TOOL_NAME]


@pytest.mark.asyncio
async def test_final_assistant_compaction_releases_session_lock_during_model_call(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = StubAdapter(
        [
            {
                "content": "Finished",
                "tool_calls": None,
                "usage": {"input_tokens": 90, "output_tokens": 5},
            }
        ]
    )
    compaction_service = _BlockingOnceCompactionService()
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    run = await loop.start_run("coder", "Finish", session_id="session-one")
    await asyncio.wait_for(
        compaction_service.started.wait(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS
    )

    async def append_background_note() -> None:
        async with runtime.chat_sessions.write_lock(session_address("coder", "session-one")):
            runtime.chat_sessions.get(session_address("coder", "session-one")).add_note(
                "Background completed"
            )

    note_task = asyncio.create_task(append_background_note())
    try:
        await asyncio.wait_for(note_task, timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
    finally:
        compaction_service.release.set()

    result = await run.wait()
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()

    assert result.content == "Finished"
    assert persisted_roles(messages) == ["user", "assistant", "note"]
    assert messages[-2].content == "Background completed"
    compaction_events = [
        event
        for event in run.events
        if event.type in {COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT}
    ]
    assert [event.type for event in compaction_events] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_ABORTED_EVENT,
    ]
    assert compaction_events[-1].payload == {"reason": "stale_context"}


@pytest.mark.asyncio
async def test_mid_tool_stale_compaction_rebuilds_request_with_concurrent_note(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call-weather", "name": "get_weather", "arguments": {}}],
                "usage": {"input_tokens": 1_800, "output_tokens": 50},
            },
            {
                "content": "Sunny",
                "tool_calls": None,
                "usage": {"input_tokens": 200, "output_tokens": 50},
            },
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"weather": "sunny"}),
    )
    compaction_service = _BlockingOnceCompactionService()
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 2_000}),
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    run = await loop.start_run("coder", "Weather?", session_id="session-one")
    await asyncio.wait_for(
        compaction_service.started.wait(), timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS
    )

    async def append_background_note() -> None:
        async with runtime.chat_sessions.write_lock(session_address("coder", "session-one")):
            runtime.chat_sessions.get(session_address("coder", "session-one")).add_note(
                "Background completed"
            )

    note_task = asyncio.create_task(append_background_note())
    try:
        await asyncio.wait_for(note_task, timeout=_ASYNC_COORDINATION_TIMEOUT_SECONDS)
    finally:
        compaction_service.release.set()

    result = await run.wait()
    messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
    second_request_text = "\n".join(
        str(message.get("content", "")) for message in adapter.requests[1]["messages"]
    )

    assert result.content == "Sunny"
    assert persisted_roles(messages) == ["user", "assistant", "tool", "note", "assistant"]
    assert "<system-reminder>\nBackground completed\n</system-reminder>" in second_request_text
    assert [
        event.type
        for event in run.events
        if event.type in {COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT}
    ] == [COMPACTION_STARTED_EVENT, COMPACTION_ABORTED_EVENT]


@pytest.mark.asyncio
async def test_compaction_reinjects_the_active_continuation_checkpoint(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Original work"))
    session.append(ChatMessage.assistant(model=agent.model, content="Partial"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load(),
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    interrupted_tracker = ContinuationTracker(
        session,
        run_id="run-one",
        request="Original work",
    )
    interrupted_tracker.record_stream_delta(reasoning="Keep this plan")
    await interrupted_tracker.interrupt("network")
    prior = await recover_continuation(session)
    assert prior is not None
    session.append(ChatMessage.user("Keep going"))
    active_tracker = ContinuationTracker(
        session,
        run_id="run-two",
        request="Keep going",
        prior_state=prior,
    )
    reminder = render_continuation_reminder(prior, context_window=100)
    messages = inject_continuation_reminder(
        await loop._build_request_messages(agent, session),
        reminder,
    )
    run = Run(run_id="run-two", agent_id=agent.id, session_id=session.id)

    rebuilt = await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
        continuation_tracker=active_tracker,
        continuation_reminder=reminder,
    )

    reminder_messages = [
        message
        for message in rebuilt
        if "<continuation-checkpoint" in str(message.get("content") or "")
    ]
    assert len(reminder_messages) == 1
    assert "Keep this plan" in reminder_messages[0]["content"]
    await active_tracker.interrupt("network")


@pytest.mark.asyncio
async def test_compaction_refreshes_pinned_skill_catalog(tmp_path: Path) -> None:
    # Compaction starts a new prompt epoch: a registry that grew since the Session
    # was pinned must be rescanned and replace both the catalog and seen-skill set.
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    # Pin the session's catalog (as the first build would), then grow the registry.
    pinned_skill_catalog(loop._dependencies, "coder", "session-one", agent, runtime.skills, None)
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )
    calls_before = runtime.system_prompts.render_skill_catalog_calls

    messages = await loop._build_request_messages(agent, session)
    await _maybe_auto_compact(
        loop, agent, adapter, "gpt-5.2", session, messages, usage={"input_tokens": 90}, run=run
    )

    metadata = runtime.chat_sessions.get_metadata(session_address("coder", "session-one"))
    assert runtime.system_prompts.render_skill_catalog_calls == calls_before + 1
    assert runtime.refresh_skills_for_calls == [(None, "coder")]
    assert metadata[PINNED_SKILL_CATALOG_META_KEY] == {"catalog_text": "catalog:2"}
    assert metadata[SEEN_SKILLS_META_KEY] == ["one", "two"]


@pytest.mark.asyncio
async def test_compaction_refreshes_pinned_soul_and_memory(tmp_path: Path) -> None:
    # Compaction starts a new prompt epoch: SOUL and pinned-memory snapshots are
    # re-rendered from the workspace so on-disk edits become visible to the model.
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        workspace=tmp_path / "workspace",
    )
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    # Pin the epoch's texts (as the first build would), then observe the refresh.
    pinned_soul_context(loop._dependencies, "coder", "session-one", agent, None)
    pinned_memory_files(loop._dependencies, "coder", "session-one", agent, None)
    soul_calls_before = runtime.system_prompts.render_soul_calls
    memory_calls_before = runtime.system_prompts.render_memory_files_calls

    messages = await loop._build_request_messages(agent, session)
    await _maybe_auto_compact(
        loop, agent, adapter, "gpt-5.2", session, messages, usage={"input_tokens": 90}, run=run
    )

    metadata = runtime.chat_sessions.get_metadata(session_address("coder", "session-one"))
    assert runtime.system_prompts.render_soul_calls == soul_calls_before + 1
    assert runtime.system_prompts.render_memory_files_calls == memory_calls_before + 1
    assert metadata[PINNED_SOUL_CONTEXT_META_KEY] == {"text": "Soul of coder"}
    assert metadata[PINNED_MEMORY_FILES_META_KEY] == {"text": "Memory of coder"}


@pytest.mark.asyncio
async def test_compaction_refresh_failure_keeps_previous_prompt_snapshot(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    loop = build_chat_loop(
        runtime,
        compaction_service=cast(
            Any,
            StubCompactionService(should_auto=True, checkpoint=checkpoint),
        ),
    )
    pinned_skill_catalog(loop._dependencies, "coder", "session-one", agent, runtime.skills, None)

    def fail_refresh(_project_id: str | None, _agent_id: str | None) -> Any:
        raise RuntimeError("scan failed")

    runtime.refresh_skills_for = fail_refresh
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    metadata = runtime.chat_sessions.get_metadata(session_address("coder", "session-one"))
    assert metadata[PINNED_SKILL_CATALOG_META_KEY] == {"catalog_text": "catalog:1"}
    assert persisted_roles(session.load())[-1] == "compaction_checkpoint"
    assert any(
        "Prompt context refresh failed after automatic Compaction" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_compaction_refreshes_rooted_working_project_files_and_auto_load(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    agents_file = repo / "AGENTS.md"
    agents_file.write_text("Original rules", encoding="utf-8")
    project = StubProject("proj", str(repo), ["AGENTS.md"], display_name="Project")
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        root_project_id="proj",
    )
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        projects=StubProjects({"proj": project}),
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    loop = build_chat_loop(
        runtime,
        compaction_service=cast(
            Any,
            StubCompactionService(should_auto=True, checkpoint=checkpoint),
        ),
    )
    run = Run(
        run_id="run-1",
        agent_id=agent.id,
        session_id=session.id,
        working_project_id="proj",
    )
    context = await loop._create_run_execution_context(
        run,
        _RunRequest(content="test"),
        session=session,
        prior_continuation=None,
        continuation_reminder=None,
        continuation_tracker=None,
    )
    context.request_state = await loop.build_request_state(
        agent,
        session,
        inputs=RequestBuildInputs.from_context(context, context.primary_target),
    )

    agents_file.write_text("Updated rules", encoding="utf-8")
    (repo / "CONTEXT.md").write_text("New context", encoding="utf-8")
    project.auto_load.append("CONTEXT.md")

    rebuilt = await loop._compaction_runs.maybe_auto_compact_state(
        context,
        context.primary_target,
        {"input_tokens": 90},
    )

    system_prompt = str(rebuilt.messages[0]["content"])
    metadata = runtime.chat_sessions.get_metadata(session_address("coder", "session-one"))
    assert "Updated rules" in system_prompt
    assert "New context" in system_prompt
    assert "Original rules" not in system_prompt
    assert runtime.refresh_skills_for_calls == [("proj", "coder")]
    assert len(runtime.system_prompts.render_working_project_context_calls) == 2
    assert "Updated rules" in metadata[PINNED_WORKING_PROJECT_CONTEXT_META_KEY]["text"]
    assert "New context" in metadata[PINNED_WORKING_PROJECT_CONTEXT_META_KEY]["text"]


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_falls_back_when_summary_model_malformed(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": "malformed-summary-model",
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert len(compaction_service.compact_calls) == 1
    assert compaction_service.compact_calls[0]["summary_model_id"] == "gpt-5.2"
    assert compaction_service.compact_calls[0]["summary_adapter"] is adapter


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_falls_back_when_summary_adapter_lookup_fails(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        raise_on_connection={"missing-provider:api-key": KeyError("missing-provider:api-key")},
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": "missing-provider/gpt-5.2::api-key",
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted tail context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    await _maybe_auto_compact(
        loop,
        agent,
        adapter,
        "gpt-5.2",
        session,
        messages,
        usage={"input_tokens": 90},
        run=run,
    )

    assert runtime.adapter_provider_id == "missing-provider"
    assert runtime.adapter_connection_id == "missing-provider:api-key"
    assert len(compaction_service.compact_calls) == 1
    assert compaction_service.compact_calls[0]["summary_model_id"] == "gpt-5.2"
    assert compaction_service.compact_calls[0]["summary_adapter"] is adapter


@pytest.mark.asyncio
async def test_compaction_maybe_auto_compact_logs_warning_when_compaction_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    compaction_service = StubCompactionService(
        should_auto=True,
        compact_error=RuntimeError("compaction broke"),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    session.append(ChatMessage.assistant(model=agent.model, content="Hello"))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    with caplog.at_level("WARNING"):
        result = await _maybe_auto_compact(
            loop,
            agent,
            adapter,
            "gpt-5.2",
            session,
            messages,
            usage={"input_tokens": 90},
            run=run,
        )

    assert result == messages
    assert persisted_roles(session.load()) == ["user", "assistant"]
    assert any(
        "Compaction failed; continuing without compaction" in record.message
        for record in caplog.records
    )
    assert [event.type for event in run.events] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_ABORTED_EVENT,
    ]
    assert run.events[-1].payload == {"reason": "failed"}


@pytest.mark.asyncio
async def test_compaction_projection_failure_does_not_persist_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    session.append(ChatMessage.assistant(model=agent.model, content="Hello"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load(),
        compacted_token_count=42,
    )
    loop = build_chat_loop(
        runtime,
        compaction_service=cast(
            Any,
            StubCompactionService(should_auto=True, checkpoint=checkpoint),
        ),
    )
    messages = await loop._build_request_messages(agent, session)
    run = Run(run_id="run-1", agent_id=agent.id, session_id=session.id)

    async def fail_projected_request(*_args: Any, **_kwargs: Any) -> _RequestState:
        raise RuntimeError("projected request broke")

    monkeypatch.setattr(loop, "build_request_state", fail_projected_request)

    with caplog.at_level("WARNING"):
        result = await _maybe_auto_compact(
            loop,
            agent,
            adapter,
            "gpt-5.2",
            session,
            messages,
            usage={"input_tokens": 90},
            run=run,
        )

    assert result == messages
    assert persisted_roles(session.load()) == ["user", "assistant"]
    assert [event.type for event in run.events] == [
        COMPACTION_STARTED_EVENT,
        COMPACTION_ABORTED_EVENT,
    ]
    assert any(
        "Post-compaction request projection failed" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_compact_session_reports_unavailable_without_compaction_service(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
    runtime.chat_sessions.create("coder", session_id="session-one")

    reply = await build_chat_loop(runtime).compact_session("coder", "session-one")

    assert reply == "Compaction is not available."


@pytest.mark.asyncio
async def test_compact_session_refuses_while_run_is_active(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="unused",
        projection=[ChatMessage.user("unused")],
        compacted_token_count=1,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=StubAdapter([]),
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    release = asyncio.Event()

    async def blocked_executor(run: Run) -> str:
        await release.wait()
        return "done"

    active_run = await runtime.chat_runs.start(
        session_address("coder", "session-one"),
        blocked_executor,
    )
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    reply = await loop.compact_session("coder", "session-one")
    release.set()
    await active_run.wait()

    assert reply == "Cannot compact while a run is active for this session."
    assert compaction_service.compact_calls == []


@pytest.mark.asyncio
async def test_compact_session_appends_checkpoint_and_closes_adapter(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))
    affinity_before = runtime.chat_sessions.prompt_cache_affinity_id(
        session_address("coder", session.id)
    )

    reply = await loop.compact_session("coder", "session-one")

    assert reply == "Context compacted."
    assert persisted_roles(session.load()) == ["user", "assistant", "compaction_checkpoint"]
    assert len(compaction_service.compact_calls) == 1
    assert (
        runtime.chat_sessions.prompt_cache_affinity_id(session_address("coder", session.id))
        != affinity_before
    )
    assert compaction_service.compact_calls[0]["summary_model_id"] == "gpt-5.2"
    assert compaction_service.compact_calls[0]["summary_adapter"] is adapter
    assert compaction_service.compact_calls[0]["storage"] is runtime.storage
    assert compaction_service.compact_calls[0]["instruction"] is None
    assert adapter.closed is True
    persisted_checkpoint = session.load()[-1]
    assert persisted_checkpoint.projection is not None
    assert str(persisted_checkpoint.projection[0]["content"]).startswith("[compaction-summary]")


@pytest.mark.asyncio
async def test_real_manual_compaction_after_completed_run_does_not_continue_agent(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = _RealCompactionAdapter(
        [
            {
                "content": "MANUAL_READY",
                "usage": {"input_tokens": 50_000, "output_tokens": 2},
                "tool_calls": None,
            }
        ],
        summaries=["MANUAL SUMMARY"],
    )
    storage = _RealCompactionStorage(
        {
            "enabled": False,
            "trigger": {"type": "input_tokens", "tokens": 1},
            "strategy": {
                "type": "summary_tail",
                "tail_tokens": 1_000,
                "summary_model": None,
            },
        },
        data_dir=tmp_path,
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=storage,
        models=StubModels({("openai", "gpt-5.2"): 1_000_000}),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD_MANUAL_CONTEXT " + ("older " * 8_000)))
    session.append(ChatMessage.assistant(model=agent.model, content="old manual answer " * 5_000))
    loop = build_chat_loop(runtime, compaction_service=CompactionService())

    completed = await loop.send("coder", "MANUAL_USER_MARKER", session_id=session.id)

    assert completed.content == "MANUAL_READY"
    assert adapter.events == ["agent"]
    assert not any(message.role == "compaction_checkpoint" for message in session.load())

    reply = await loop.compact_session("coder", session.id)

    checkpoints = [message for message in session.load() if message.role == "compaction_checkpoint"]
    assert reply == "Context compacted."
    assert adapter.events == ["agent", "compaction"]
    assert len(adapter.requests) == 1
    assert len(checkpoints) == 1
    assert storage.prompt_fragment_reads == ["compaction-manual.md"]
    assert "MANUAL_USER_MARKER" not in json.dumps(adapter.stream_requests[0]["messages"])
    projection_text = json.dumps(checkpoints[0].projection)
    assert isinstance(checkpoints[0].content, str)
    assert checkpoints[0].content.startswith(COMPACTION_REFERENCE_PREFIX)
    assert "MANUAL_USER_MARKER" in projection_text
    assert "MANUAL_READY" in projection_text


@pytest.mark.asyncio
async def test_compact_session_keeps_configured_summary_model(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    active_adapter = ClosingStubAdapter([])
    summary_adapter = ClosingStubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=active_adapter,
        adapters_by_connection={"anthropic:api-key": summary_adapter},
        provider_ids={"openai", "anthropic"},
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": "anthropic/claude-summary",
            }
        ),
        models=StubModels(
            {
                ("openai", "gpt-5.2"): 100,
                ("anthropic", "claude-summary"): 100,
            }
        ),
    )
    runtime.provider_credentials = StubProviderCredentials({"openai:api-key", "anthropic:api-key"})
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Tail user"))
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)

    reply = await build_chat_loop(
        runtime,
        compaction_service=cast(Any, compaction_service),
    ).compact_session("coder", "session-one")

    compact_call = compaction_service.compact_calls[0]
    assert reply == "Context compacted."
    assert compact_call["summary_adapter"] is summary_adapter
    assert compact_call["summary_model_id"] == "claude-summary"
    assert active_adapter.closed is True
    assert summary_adapter.closed is True


@pytest.mark.asyncio
async def test_manual_compaction_refreshes_skill_catalog_snapshot(tmp_path: Path) -> None:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        allowed_skills=["*"],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=ClosingStubAdapter([]),
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
    )
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    loop = build_chat_loop(
        runtime,
        compaction_service=cast(
            Any,
            StubCompactionService(should_auto=True, checkpoint=checkpoint),
        ),
    )
    pinned_skill_catalog(loop._dependencies, "coder", "session-one", agent, runtime.skills, None)
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )

    reply = await loop.compact_session("coder", "session-one")

    metadata = runtime.chat_sessions.get_metadata(session_address("coder", "session-one"))
    assert reply == "Context compacted."
    assert runtime.refresh_skills_for_calls == [(None, "coder")]
    assert metadata[PINNED_SKILL_CATALOG_META_KEY] == {"catalog_text": "catalog:2"}
    assert metadata[SEEN_SKILLS_META_KEY] == ["one", "two"]


@pytest.mark.asyncio
async def test_compact_session_scopes_to_project_session_and_agent(tmp_path: Path) -> None:
    # A /compact issued in a project chat must compact the project session and
    # resolve the project agent — never silently fall back to the identity session.
    identity_agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    project_agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=identity_agent,
        adapter=adapter,
        project_agents={("proj", "coder"): project_agent},
        projects=StubProjects({"proj": StubProject("proj", str(tmp_path), [])}),
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15_000, "summary_model": None}
        ),
    )
    # Same session id in both scopes, distinct content: the identity session must
    # stay untouched, proving the project scope is the one that was loaded.
    identity_session = runtime.chat_sessions.create("coder", session_id="session-one")
    identity_session.append(ChatMessage.user("identity tail"))
    project_session = runtime.chat_sessions.create(
        "coder", session_id="session-one", project_id="proj"
    )
    project_tail = ChatMessage.user("project tail")
    project_session.append(project_tail)
    project_session.append(ChatMessage.assistant(model=project_agent.model, content="project a"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=project_session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    reply = await loop.compact_session("coder", "session-one", project_id="proj")

    assert reply == "Context compacted."
    # Resolved the project agent, never the identity fallback.
    assert ("proj", "coder") in runtime.agent_resolver.calls
    assert (None, "coder") not in runtime.agent_resolver.calls
    # The project session got the checkpoint; the identity session is untouched.
    assert persisted_roles(project_session.load()) == [
        "user",
        "assistant",
        "compaction_checkpoint",
    ]
    assert persisted_roles(identity_session.load()) == ["user"]


@pytest.mark.asyncio
async def test_compact_session_forwards_instruction_to_service(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = ClosingStubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    tail_user = ChatMessage.user("Tail user")
    session.append(tail_user)
    session.append(ChatMessage.assistant(model=agent.model, content="Tail assistant"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted context.",
        projection=session.load()[-2:],
        compacted_token_count=42,
    )
    compaction_service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    reply = await loop.compact_session("coder", "session-one", "keep the API design")

    assert reply == "Context compacted."
    assert compaction_service.compact_calls[0]["instruction"] == "keep the API design"


@pytest.mark.asyncio
async def test_compact_session_converts_compaction_failure_into_reply(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    compaction_service = StubCompactionService(
        should_auto=True,
        compact_error=RuntimeError("compaction broke"),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=StubAdapter([]),
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, compaction_service))

    reply = await loop.compact_session("coder", "session-one")

    assert reply == "Compaction failed: compaction broke"
    assert persisted_roles(session.load()) == ["user"]
    assert runtime.refresh_skills_for_calls == []
    request_state = await loop.build_request_state(agent, session, inputs=RequestBuildInputs())
    assert HISTORY_TOOL_NAME not in [tool["name"] for tool in request_state.tools]
