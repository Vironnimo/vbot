"""Manual ``/compact``: admission, targets, replies and the dedicated Compaction Run."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatMessage
from core.compaction import CompactionService
from core.compaction.compaction import COMPACTION_REFERENCE_PREFIX, CompactionError
from core.prompts.pinned_context import PINNED_SKILL_CATALOG_SLOT, pinned_skill_catalog
from core.runs import COMPACTION_ABORTED_EVENT, Run
from tests.core.chat.chat_loop_compaction_test_support import (
    CompactOnceService,
    RecordingCompactionAdapter,
    append_note_while_compacting,
    compaction_runtime,
    real_compaction_runtime,
    seed_tail,
)
from tests.core.chat.chat_loop_support import (
    ClosingStubAdapter,
    StubAgent,
    StubCompactionService,
    StubModels,
    StubProject,
    StubProjects,
    StubProviderCredentials,
    StubSkill,
    StubSkills,
    build_chat_loop,
    persisted_roles,
    session_address,
)


@pytest.mark.asyncio
async def test_compact_session_reports_unavailable_without_compaction_service(
    tmp_path: Path,
) -> None:
    runtime = compaction_runtime(tmp_path)
    runtime.chat_sessions.create("coder", session_id="session-one")

    reply = await build_chat_loop(runtime).compact_session("coder", "session-one")

    assert reply == "Compaction is not available."


@pytest.mark.asyncio
async def test_compact_session_refuses_while_run_is_active(tmp_path: Path) -> None:
    runtime = compaction_runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))
    release = asyncio.Event()

    async def blocked_executor(_run: Run) -> str:
        await release.wait()
        return "done"

    active_run = await runtime.chat_runs.start(session.address, blocked_executor)
    try:
        reply = await build_chat_loop(
            runtime, compaction_service=cast(Any, service)
        ).compact_session("coder", "session-one")
    finally:
        release.set()
        await active_run.wait()

    assert reply == "Cannot compact while a run is active for this session."
    assert service.compact_calls == []


@pytest.mark.asyncio
async def test_compact_session_commits_the_checkpoint_and_closes_the_adapter(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = ClosingStubAdapter([])
    runtime = compaction_runtime(tmp_path, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))
    caplog.set_level(logging.INFO, logger="vbot.compaction.coordination")

    reply = await build_chat_loop(runtime, compaction_service=cast(Any, service)).compact_session(
        "coder", "session-one", "keep the API design"
    )

    assert reply == "Context compacted."
    [completed] = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.compaction.coordination" and record.levelno >= logging.INFO
    ]
    assert all(field in completed for field in ("trigger=manual", "tokens_after=", "duration_ms="))
    assert persisted_roles(session.load()) == ["user", "assistant", "compaction_checkpoint"]
    [call] = service.compact_calls
    assert call["summary_model_id"] == "gpt-5.2"
    assert call["summary_adapter"] is adapter
    assert call["storage"] is runtime.storage
    assert call["instruction"] == "keep the API design"
    assert adapter.closed is True
    [checkpoint] = [
        message for message in session.load() if message.role == "compaction_checkpoint"
    ]
    assert checkpoint.projection is not None
    assert str(checkpoint.projection[0]["content"]).startswith("[compaction-summary]")


@pytest.mark.asyncio
async def test_real_manual_compaction_after_completed_run_does_not_continue_agent(
    tmp_path: Path,
) -> None:
    adapter = RecordingCompactionAdapter(
        [
            {
                "content": "MANUAL_READY",
                "usage": {"input_tokens": 50_000, "output_tokens": 2},
                "tool_calls": None,
            }
        ],
        summaries=["MANUAL SUMMARY"],
    )
    runtime = real_compaction_runtime(
        tmp_path,
        adapter,
        {
            "enabled": False,
            "trigger": {"type": "input_tokens", "tokens": 1},
            "strategy": {"type": "summary_tail", "tail_tokens": 1_000, "summary_model": None},
        },
        agent=StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[]),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("OLD_MANUAL_CONTEXT " + ("older " * 8_000)))
    session.append(
        ChatMessage.assistant(model="openai/gpt-5.2", content="old manual answer " * 5_000)
    )
    loop = build_chat_loop(runtime, compaction_service=CompactionService())

    completed = await loop.send("coder", "MANUAL_USER_MARKER", session_id=session.id)

    assert completed.content == "MANUAL_READY"
    assert adapter.events == ["agent"]
    assert "compaction_checkpoint" not in persisted_roles(session.load())

    reply = await loop.compact_session("coder", session.id)

    [checkpoint] = [
        message for message in session.load() if message.role == "compaction_checkpoint"
    ]
    assert reply == "Context compacted."
    assert adapter.events == ["agent", "compaction"]
    assert runtime.storage.prompt_fragment_reads == ["compaction-manual.md"]
    assert "MANUAL_USER_MARKER" not in json.dumps(adapter.stream_requests[0]["messages"])
    assert isinstance(checkpoint.content, str)
    assert checkpoint.content.startswith(COMPACTION_REFERENCE_PREFIX)
    projection_text = json.dumps(checkpoint.projection)
    assert "MANUAL_USER_MARKER" in projection_text and "MANUAL_READY" in projection_text


@pytest.mark.asyncio
async def test_compact_session_keeps_configured_summary_model(tmp_path: Path) -> None:
    active_adapter = ClosingStubAdapter([])
    summary_adapter = ClosingStubAdapter([])
    runtime = compaction_runtime(
        tmp_path,
        adapter=active_adapter,
        adapters_by_connection={"anthropic:api-key": summary_adapter},
        provider_ids={"openai", "anthropic"},
        settings={"summary_model": "anthropic/claude-summary"},
        models=StubModels({("openai", "gpt-5.2"): 100, ("anthropic", "claude-summary"): 100}),
    )
    runtime.provider_credentials = StubProviderCredentials({"openai:api-key", "anthropic:api-key"})
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))

    reply = await build_chat_loop(runtime, compaction_service=cast(Any, service)).compact_session(
        "coder", "session-one"
    )

    [call] = service.compact_calls
    assert reply == "Context compacted."
    assert call["summary_adapter"] is summary_adapter
    assert call["summary_model_id"] == "claude-summary"
    assert call["instruction"] is None
    assert active_adapter.closed is True
    assert summary_adapter.closed is True


@pytest.mark.asyncio
async def test_compact_session_scopes_to_project_session_and_agent(tmp_path: Path) -> None:
    # A /compact issued in a project chat must compact the project session and
    # resolve the project agent — never silently fall back to the identity session.
    project_agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    runtime = compaction_runtime(
        tmp_path,
        adapter=ClosingStubAdapter([]),
        project_agents={("proj", "coder"): project_agent},
        projects=StubProjects({"proj": StubProject("proj", str(tmp_path), [])}),
    )
    # Same session id in both scopes: the identity session must stay untouched.
    identity_session = runtime.chat_sessions.create("coder", session_id="session-one")
    identity_session.append(ChatMessage.user("identity tail"))
    project_session = runtime.chat_sessions.create(
        "coder", session_id="session-one", project_id="proj"
    )
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(project_session))

    reply = await build_chat_loop(runtime, compaction_service=cast(Any, service)).compact_session(
        "coder", "session-one", project_id="proj"
    )

    assert reply == "Context compacted."
    assert ("proj", "coder") in runtime.agent_resolver.calls
    assert (None, "coder") not in runtime.agent_resolver.calls
    assert persisted_roles(project_session.load()) == [
        "user",
        "assistant",
        "compaction_checkpoint",
    ]
    assert persisted_roles(identity_session.load()) == ["user"]


@pytest.mark.asyncio
async def test_compact_session_converts_compaction_failure_into_reply(tmp_path: Path) -> None:
    runtime = compaction_runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    service = StubCompactionService(
        should_auto=True, compact_error=RuntimeError("compaction broke")
    )
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))

    reply = await loop.compact_session("coder", "session-one")

    assert reply == "Compaction failed: compaction broke"
    assert persisted_roles(session.load()) == ["user"]
    assert runtime.refresh_skills_for_calls == []


@pytest.mark.asyncio
async def test_manual_compaction_preserves_note_appended_during_summary(tmp_path: Path) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    runtime = compaction_runtime(tmp_path, agent=agent)
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Earlier context"))
    service = CompactOnceService(block=True)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    pinned_skill_catalog(
        loop._dependencies, "coder", session.id, agent, runtime.skills, None, skill_project_id=None
    )
    # The refused commit's prompt epoch would pin this grown registry.
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )
    affinity = runtime.chat_sessions.prompt_cache_affinity_id(session.address)

    run = await loop.start_compaction_run("coder", session.id)
    await append_note_while_compacting(
        runtime, service, session_address("coder", "session-one"), "BACKGROUND_RESULT_SENTINEL"
    )

    # A stale manual result fails its Run with retry guidance and writes nothing.
    with pytest.raises(CompactionError):
        await run.wait()
    messages = session.load()
    assert [message.role for message in messages] == ["user", "note", "run_summary"]
    assert messages[-2].content == "BACKGROUND_RESULT_SENTINEL"
    assert runtime.refresh_skills_for_calls == [(None, "coder")]
    assert runtime.chat_sessions.prompt_cache_affinity_id(session.address) == affinity
    catalog_pin = runtime.chat_sessions.prompt_pin(session.address, PINNED_SKILL_CATALOG_SLOT)
    assert catalog_pin is not None and catalog_pin["catalog_text"] == "catalog:1"
    assert runtime.chat_sessions.seen_skills(session.address) is None
    assert any(
        event.type == COMPACTION_ABORTED_EVENT for event in await runtime.timelines.events(run)
    )
