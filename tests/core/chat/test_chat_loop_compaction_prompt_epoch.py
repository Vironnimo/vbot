"""Tests for chat loop compaction prompt epoch."""

from __future__ import annotations

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
    _RunRequest,
    create_run_execution_context,
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
from core.runs import (
    Run,
)
from core.tools import (
    tool_success,
)
from tests.core.chat.chat_loop_compaction_test_support import (
    _maybe_auto_compact,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubModels,
    StubProject,
    StubProjects,
    StubRuntime,
    StubSkill,
    StubSkills,
    StubStorage,
    build_chat_loop,
    persisted_roles,
    session_address,
)


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

    messages = await loop._requests._build_request_messages(agent, session)
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

    context = await create_run_execution_context(
        loop._dependencies,
        loop._requests,
        run,
        _RunRequest(content="test"),
        session=session,
        prior_continuation=None,
        continuation_reminder=None,
        continuation_tracker=None,
    )
    context.request_state = await loop._requests.build_request_state(
        agent,
        session,
        inputs=RequestBuildInputs.from_context(context, context.primary_target),
    )
    runtime.system_prompts.render_soul = lambda *_args, **_kwargs: "NEW_SOUL_SENTINEL"
    runtime.system_prompts.render_memory_files = lambda *_args, **_kwargs: "NEW_MEMORY_SENTINEL"
    await loop._compaction_runs.maybe_auto_compact_state(
        context,
        context.primary_target,
        {"input_tokens": 90},
    )
    assert context.soul_context == "NEW_SOUL_SENTINEL"
    assert context.memory_files_context == "NEW_MEMORY_SENTINEL"
    await loop._requests.build_request_state(
        agent,
        session,
        inputs=RequestBuildInputs.from_context(context, context.primary_target),
    )
    assert runtime.system_prompts.build_pin_calls[-1]["soul_context"] == "NEW_SOUL_SENTINEL"
    assert (
        runtime.system_prompts.build_pin_calls[-1]["memory_files_context"] == "NEW_MEMORY_SENTINEL"
    )
    assert (
        pinned_memory_files(loop._dependencies, "coder", "session-one", agent, None)
        == "NEW_MEMORY_SENTINEL"
    )

    metadata = runtime.chat_sessions.get_metadata(session_address("coder", "session-one"))
    assert metadata[PINNED_SOUL_CONTEXT_META_KEY] == {"text": "NEW_SOUL_SENTINEL"}
    assert metadata[PINNED_MEMORY_FILES_META_KEY] == {
        "text": "NEW_MEMORY_SENTINEL",
        "mode": "agent_user",
    }


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
    messages = await loop._requests._build_request_messages(agent, session)
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
    context = await create_run_execution_context(
        loop._dependencies,
        loop._requests,
        run,
        _RunRequest(content="test"),
        session=session,
        prior_continuation=None,
        continuation_reminder=None,
        continuation_tracker=None,
    )
    context.request_state = await loop._requests.build_request_state(
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
@pytest.mark.parametrize("project_id", [None, "proj"])
@pytest.mark.parametrize("child", [False, True])
async def test_temporary_compaction_refreshes_epoch_without_identity_lookup(
    tmp_path, project_id, child
):
    from core.agents.temporary import TemporaryAgentConfig, TemporaryAgentRegistry
    from core.extensions import ExtensionAPI, ExtensionRecord, ExtensionRegistry
    from core.extensions.extensions import ExtensionDeclarations
    from core.runs import RunExecutionOwner
    from core.tools.availability import ToolAccess

    repo = tmp_path / "repo"
    repo.mkdir()
    rules = repo / "AGENTS.md"
    rules.write_text("OLD_RULES_SENTINEL", encoding="utf-8")
    adapter = StubAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="ordinary", model="openai/gpt-5.2"),
        adapter=adapter,
        projects=StubProjects({"proj": StubProject("proj", str(repo), ["AGENTS.md"])}),
        storage=StubStorage(
            {"auto": True, "threshold": 0.8, "tail_tokens": 15000, "summary_model": None}
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    declarations = ExtensionDeclarations()
    api = ExtensionAPI("test", declarations, config={}, logger=None)
    api.register_session_tool(
        "test_private",
        "TEST_TOOL_SENTINEL",
        {"type": "object", "properties": {}},
        lambda *_args, **_kwargs: tool_success({}),
    )
    api.register_session_runtime(
        before_request=lambda *_args, **_kwargs: None,
        run_finished=lambda *_args, **_kwargs: None,
        quiesce=lambda: None,
    )
    extensions = ExtensionRegistry()
    extensions._records.append(
        ExtensionRecord(
            "test", tmp_path, tmp_path / "extension.py", "loaded", declarations=declarations
        )
    )
    extensions.apply_tools(runtime.tools)
    runtime.extensions = extensions
    registry = TemporaryAgentRegistry(runtime.chat_sessions)
    runtime.agent_resolver.temporary_agents = registry
    binding = registry.create(
        owner_name="test",
        group_id="group",
        participant_id="participant",
        project_id=project_id,
        config=TemporaryAgentConfig(
            model="openai/gpt-5.2",
            cwd=repo,
            tool_access=ToolAccess(mode="none"),
            allowed_skills=["*"],
            tools={},
            name="Test",
            instructions="TEMP_BODY_SENTINEL",
        ),
    )
    session = (
        runtime.chat_sessions.create(
            binding.address.agent_id, session_id="child", project_id=project_id
        )
        if child
        else runtime.chat_sessions.get(binding.address)
    )
    session.append(ChatMessage.user("Tail user"))
    session.append(ChatMessage.assistant(model="openai/gpt-5.2", content="Tail answer"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="SUMMARY_SENTINEL", projection=session.load(), compacted_token_count=42
    )
    loop = build_chat_loop(
        runtime,
        compaction_service=cast(
            Any, StubCompactionService(should_auto=True, checkpoint=checkpoint)
        ),
    )
    run = Run(
        run_id="run-test",
        agent_id=binding.address.agent_id,
        session_id=session.id,
        project_id=project_id,
        working_project_id=project_id,
        execution_owner=RunExecutionOwner(
            epoch=1,
            extension="test",
            group_id="group",
            participant_id="participant",
            generation_id=binding.generation_id,
        ),
    )
    request = (
        _RunRequest(content="test", temporary_parent_binding=binding)
        if child
        else _RunRequest(content="test", temporary_binding=binding)
    )
    context = await create_run_execution_context(
        loop._dependencies,
        loop._requests,
        run,
        request,
        session=session,
        prior_continuation=None,
        continuation_reminder=None,
        continuation_tracker=None,
    )
    context.request_state = await loop._requests.build_request_state(
        context.agent,
        session,
        inputs=RequestBuildInputs.from_context(context, context.primary_target),
    )
    old_project_context = context.working_project_context
    rules.write_text("NEW_RULES_SENTINEL", encoding="utf-8")
    runtime.skills = StubSkills([StubSkill("new", "NEW_SKILL_SENTINEL", Path("new"))])
    rebuilt = await loop._compaction_runs.maybe_auto_compact_state(
        context, context.primary_target, {"input_tokens": 90}
    )
    metadata = runtime.chat_sessions.get_metadata(
        session_address(run.agent_id, session.id, project_id)
    )
    assert persisted_roles(session.load())[-1] == "compaction_checkpoint"
    assert metadata[PINNED_SKILL_CATALOG_META_KEY] == {"catalog_text": "catalog:1"}
    assert metadata[SEEN_SKILLS_META_KEY] == ["new"]
    assert runtime.refresh_skills_for_calls == [(project_id, None)]
    assert runtime.agent_resolver.calls == []
    assert context.agent_body == "TEMP_BODY_SENTINEL"
    assert context.soul_context is None and context.memory_files_context is None
    assert PINNED_SOUL_CONTEXT_META_KEY not in metadata
    assert PINNED_MEMORY_FILES_META_KEY not in metadata
    if project_id:
        assert "OLD_RULES_SENTINEL" in old_project_context
        assert "NEW_RULES_SENTINEL" in metadata[PINNED_WORKING_PROJECT_CONTEXT_META_KEY]["text"]
        assert "NEW_RULES_SENTINEL" in rebuilt.messages[0]["content"]
        assert runtime.file_read_state.check_stale(session.id, rules.resolve()) is None
        assert (
            context.working_project_context
            == metadata[PINNED_WORKING_PROJECT_CONTEXT_META_KEY]["text"]
        )
