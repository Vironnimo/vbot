"""Tests for chat loop compaction manual."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat._request_builder import (
    SEEN_SKILLS_META_KEY,
)
from core.chat._run_state import (
    RequestBuildInputs,
)
from core.compaction import (
    CompactionService,
)
from core.compaction.compaction import (
    COMPACTION_REFERENCE_PREFIX,
)
from core.prompts.pinned_context import (
    PINNED_SKILL_CATALOG_META_KEY,
    pinned_skill_catalog,
)
from core.runs import (
    Run,
)
from core.tools import (
    HISTORY_TOOL_NAME,
    register_history_tool,
)
from tests.core.chat.chat_loop_compaction_test_support import (
    _RealCompactionAdapter,
    _RealCompactionStorage,
)
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
    request_state = await loop._requests.build_request_state(
        agent, session, inputs=RequestBuildInputs()
    )
    assert HISTORY_TOOL_NAME not in [tool["name"] for tool in request_state.tools]
