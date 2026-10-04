"""A committed checkpoint starts a new prompt epoch with refreshed pinned prompt context."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from core.agents.temporary import TemporaryAgentConfig, TemporaryAgentRegistry
from core.chat import ChatMessage
from core.chat._run_state import RequestBuildInputs, _RunRequest
from core.chat._tool_epoch import ToolEpochPin
from core.chat.wire_shaping import PINNED_IMAGE_RETIREMENT_SLOT
from core.extensions import ExtensionAPI, ExtensionRecord, ExtensionRegistry
from core.extensions.extensions import ExtensionDeclarations
from core.model_tasks import TASK_IMAGE_UNDERSTANDING
from core.prompts.pinned_context import (
    PINNED_MEMORY_FILES_SLOT,
    PINNED_SKILL_CATALOG_SLOT,
    PINNED_SOUL_CONTEXT_SLOT,
    PINNED_TOOL_DEFINITIONS_SLOT,
    PINNED_WORKING_PROJECT_CONTEXT_SLOT,
    pinned_memory_files,
    pinned_skill_catalog,
    pinned_soul_context,
)
from core.runs import Run, RunExecutionOwner
from core.tools import ANALYZE_IMAGE_TOOL_NAME, ToolRegistry, tool_success
from core.tools.availability import ToolAccess
from tests.core.chat.chat_loop_compaction_test_support import (
    auto_compact,
    compact_context,
    compaction_runtime,
    run_context,
    seed_tail,
)
from tests.core.chat.chat_loop_support import (
    ClosingStubAdapter,
    StubAgent,
    StubCompactionService,
    StubProject,
    StubProjects,
    StubSkill,
    StubSkills,
    build_chat_loop,
    persisted_roles,
    session_address,
)


def _grow_skills_after_pinning(runtime: Any, loop: Any, session: Any) -> None:
    """Pin a one-Skill catalog as the first build would, then grow the registry."""
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    pinned_skill_catalog(
        loop._dependencies,
        "coder",
        session.id,
        runtime.agents.get("coder"),
        runtime.skills,
        None,
        skill_project_id=None,
    )
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True], ids=["automatic", "manual"])
async def test_compaction_rescans_skills_into_the_new_epoch(tmp_path: Path, manual: bool) -> None:
    # A registry that grew since the Session was pinned is rescanned; the checkpoint,
    # the new catalog and seen-Skill set, and a new prompt-cache affinity commit together.
    runtime = compaction_runtime(tmp_path, adapter=ClosingStubAdapter([]))
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    _grow_skills_after_pinning(runtime, loop, session)
    renders_before = runtime.system_prompts.render_skill_catalog_calls
    affinity_before = runtime.chat_sessions.prompt_cache_affinity_id(session.address)

    if manual:
        assert await loop.compact_session("coder", session.id) == "Context compacted."
    else:
        await auto_compact(loop, runtime.agents.get("coder"), session, usage={"input_tokens": 90})

    catalog_pin = runtime.chat_sessions.prompt_pin(session.address, PINNED_SKILL_CATALOG_SLOT)
    assert persisted_roles(session.load())[-1] == "compaction_checkpoint"
    assert runtime.system_prompts.render_skill_catalog_calls == renders_before + 1
    assert runtime.refresh_skills_for_calls == [(None, "coder")]
    assert catalog_pin is not None and catalog_pin["catalog_text"] == "catalog:2"
    assert runtime.chat_sessions.seen_skills(session.address) == frozenset({"one", "two"})
    assert runtime.chat_sessions.prompt_cache_affinity_id(session.address) != affinity_before


@pytest.mark.asyncio
async def test_compaction_refresh_failure_keeps_previous_prompt_snapshot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = compaction_runtime(tmp_path)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    _grow_skills_after_pinning(runtime, loop, session)

    def fail_refresh(_project_id: str | None, _agent_id: str | None) -> Any:
        raise RuntimeError("scan failed")

    runtime.refresh_skills_for = fail_refresh

    await auto_compact(loop, runtime.agents.get("coder"), session, usage={"input_tokens": 90})

    # The checkpoint still commits, with the previous epoch's pins.
    catalog_pin = runtime.chat_sessions.prompt_pin(session.address, PINNED_SKILL_CATALOG_SLOT)
    assert catalog_pin is not None and catalog_pin["catalog_text"] == "catalog:1"
    assert persisted_roles(session.load())[-1] == "compaction_checkpoint"
    assert any(
        "Prompt context refresh failed after automatic Compaction" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refresh_fails", "on_fallback"),
    [(False, False), (True, False), (False, True)],
    ids=["refreshed", "refresh-failed", "on-fallback"],
)
async def test_compaction_pins_the_current_tools_for_the_new_epoch(
    tmp_path: Path, refresh_fails: bool, on_fallback: bool
) -> None:
    # The checkpoint drops the notes that announced Tool changes, so the new epoch's
    # Tool pin lists the Tools of now, even when the rest of the refresh failed. It
    # applies the route gates of the Run's primary route, also while a fallback serves
    # the Run: the text-only primary offers analyze_image, the image-viewing fallback
    # would not.
    tools = ToolRegistry()
    for name in ("kept", "dropped", ANALYZE_IMAGE_TOOL_NAME):
        tools.register(name, "Probe.", {"type": "object"}, lambda *_args: tool_success({}))

    def restart() -> Any:
        return compaction_runtime(
            tmp_path, tools=tools, available_task_models={TASK_IMAGE_UNDERSTANDING}
        )

    runtime = restart()
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    context = await run_context(
        loop, Run(run_id="run-1", agent_id="coder", session_id=session.id), session
    )
    old_epoch = context.request_state.tool_epoch.pin.epoch
    retired = {"images": [["tool", "call-1", 0]]}
    runtime.chat_sessions.ensure_prompt_pin(
        session.address, PINNED_IMAGE_RETIREMENT_SLOT, retired, lambda current: current == retired
    )
    tools.unregister("dropped")
    # Keys in the author's order, which is not alphabetical at any level.
    ordered = {
        "type": "object",
        "properties": {"zeta": {"type": "string", "description": "First."}, "alpha": {}},
        "required": ["zeta"],
        "additionalProperties": False,
    }
    tools.register("added", "Probe.", ordered, lambda *_args: tool_success({}))
    if refresh_fails:

        def fail_refresh(_project_id: str | None, _agent_id: str | None) -> Any:
            raise RuntimeError("scan failed")

        runtime.refresh_skills_for = fail_refresh
    fallback = replace(
        context.primary_target,
        input_modalities=frozenset({"text", "image"}),
        wire_media_types=frozenset({"image/png"}),
    )

    rebuilt = await compact_context(loop, context, fallback if on_fallback else None)

    pin = ToolEpochPin.from_payload(
        runtime.chat_sessions.prompt_pin(session.address, PINNED_TOOL_DEFINITIONS_SLOT)
    )
    assert persisted_roles(session.load())[-1] == "compaction_checkpoint"
    assert pin is not None and pin.epoch != old_epoch
    # Compacted history keeps every remaining image until a limit retires it anew.
    assert runtime.chat_sessions.prompt_pin(session.address, PINNED_IMAGE_RETIREMENT_SLOT) is None
    assert pin.names == ("get_weather", "added", ANALYZE_IMAGE_TOOL_NAME, "kept")
    assert rebuilt.tool_epoch.pin == pin
    # The new epoch's first request already sends the bytes a restarted runtime's next
    # Run reads back from the pin, with the author's key order.
    runtime.chat_sessions.close()
    runtime = restart()
    reopened = runtime.chat_sessions.get(session.address)
    next_run = await run_context(
        build_chat_loop(runtime),
        Run(run_id="run-2", agent_id="coder", session_id=session.id),
        reopened,
    )
    sent = json.dumps(rebuilt.tools)
    assert sent == json.dumps(list(pin.definitions)) == json.dumps(next_run.request_state.tools)
    assert json.dumps(rebuilt.tools[1]["parameters"]) == json.dumps(ordered)


@pytest.mark.asyncio
async def test_compaction_refreshes_pinned_soul_and_memory(tmp_path: Path) -> None:
    # SOUL and pinned-memory snapshots re-render from the workspace so on-disk edits
    # reach the active Run and the persisted pins.
    agent = StubAgent(
        id="coder", model="openai/gpt-5.2", allowed_tools=["*"], workspace=tmp_path / "workspace"
    )
    runtime = compaction_runtime(tmp_path, agent=agent)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    pinned_soul_context(loop._dependencies, "coder", "session-one", agent, None)
    pinned_memory_files(loop._dependencies, "coder", "session-one", agent, None)
    context = await run_context(
        loop, Run(run_id="run-1", agent_id="coder", session_id=session.id), session
    )
    runtime.system_prompts.render_soul = lambda *_args, **_kwargs: "NEW_SOUL_SENTINEL"
    runtime.system_prompts.render_memory_files = lambda *_args, **_kwargs: "NEW_MEMORY_SENTINEL"

    await compact_context(loop, context)

    assert context.soul_context == "NEW_SOUL_SENTINEL"
    assert context.memory_files_context == "NEW_MEMORY_SENTINEL"
    await loop._requests.build_request_state(
        agent, session, inputs=RequestBuildInputs.from_context(context, context.primary_target)
    )
    last_pin_build = runtime.system_prompts.build_pin_calls[-1]
    assert last_pin_build["soul_context"] == "NEW_SOUL_SENTINEL"
    assert last_pin_build["memory_files_context"] == "NEW_MEMORY_SENTINEL"
    assert (
        pinned_memory_files(loop._dependencies, "coder", "session-one", agent, None)
        == "NEW_MEMORY_SENTINEL"
    )
    assert runtime.chat_sessions.prompt_pin(session.address, PINNED_SOUL_CONTEXT_SLOT) == {
        "text": "NEW_SOUL_SENTINEL"
    }
    assert runtime.chat_sessions.prompt_pin(session.address, PINNED_MEMORY_FILES_SLOT) == {
        "text": "NEW_MEMORY_SENTINEL",
        "mode": "agent_user",
    }


@pytest.mark.asyncio
async def test_compaction_refreshes_rooted_working_project_files_and_auto_load(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    agents_file = repo / "AGENTS.md"
    agents_file.write_text("Original rules", encoding="utf-8")
    project = StubProject("proj", str(repo), ["AGENTS.md"], display_name="Project")
    runtime = compaction_runtime(
        tmp_path,
        agent=StubAgent(
            id="coder", model="openai/gpt-5.2", allowed_tools=["*"], root_project_id="proj"
        ),
        projects=StubProjects({"proj": project}),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    service = StubCompactionService(should_auto=True, checkpoint=seed_tail(session))
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))
    context = await run_context(
        loop,
        Run(run_id="run-1", agent_id="coder", session_id=session.id, working_project_id="proj"),
        session,
    )
    agents_file.write_text("Updated rules", encoding="utf-8")
    (repo / "CONTEXT.md").write_text("New context", encoding="utf-8")
    project.auto_load.append("CONTEXT.md")

    rebuilt = await compact_context(loop, context)

    system_prompt = str(rebuilt.messages[0]["content"])
    project_pin = runtime.chat_sessions.prompt_pin(
        session.address, PINNED_WORKING_PROJECT_CONTEXT_SLOT
    )
    assert "Updated rules" in system_prompt and "New context" in system_prompt
    assert "Original rules" not in system_prompt
    assert runtime.refresh_skills_for_calls == [("proj", "coder")]
    assert len(runtime.system_prompts.render_working_project_context_calls) == 2
    assert project_pin is not None
    assert "Updated rules" in project_pin["text"] and "New context" in project_pin["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_id", "child"),
    [(None, False), ("proj", False), ("proj", True)],
    ids=["own-session", "own-project-session", "child-project-session"],
)
async def test_temporary_compaction_refreshes_epoch_without_identity_lookup(
    tmp_path: Path, project_id: str | None, child: bool
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rules = repo / "AGENTS.md"
    rules.write_text("OLD_RULES_SENTINEL", encoding="utf-8")
    runtime = compaction_runtime(
        tmp_path,
        agent=StubAgent(id="ordinary", model="openai/gpt-5.2"),
        projects=StubProjects({"proj": StubProject("proj", str(repo), ["AGENTS.md"])}),
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
            epoch="epoch",
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
    context = await run_context(loop, run, session, request)
    old_project_context = context.working_project_context
    rules.write_text("NEW_RULES_SENTINEL", encoding="utf-8")
    runtime.skills = StubSkills([StubSkill("new", "NEW_SKILL_SENTINEL", Path("new"))])

    rebuilt = await compact_context(loop, context)

    address = session_address(run.agent_id, session.id, project_id)
    prompt_pin = runtime.chat_sessions.prompt_pin
    assert persisted_roles(session.load())[-1] == "compaction_checkpoint"
    assert prompt_pin(address, PINNED_SKILL_CATALOG_SLOT) == {
        "catalog_text": "catalog:1",
        "working_project_id": project_id,
    }
    assert runtime.chat_sessions.seen_skills(address) == frozenset({"new"})
    assert runtime.refresh_skills_for_calls == [(project_id, None)]
    assert runtime.agent_resolver.calls == []
    assert context.agent_body == "TEMP_BODY_SENTINEL"
    assert context.soul_context is None and context.memory_files_context is None
    assert prompt_pin(address, PINNED_SOUL_CONTEXT_SLOT) is None
    assert prompt_pin(address, PINNED_MEMORY_FILES_SLOT) is None
    project_pin = prompt_pin(address, PINNED_WORKING_PROJECT_CONTEXT_SLOT)
    if project_id:
        assert project_pin is not None
        assert "OLD_RULES_SENTINEL" in old_project_context
        assert "NEW_RULES_SENTINEL" in project_pin["text"]
        assert project_pin["working_project_id"] == project_id
        assert "NEW_RULES_SENTINEL" in rebuilt.messages[0]["content"]
        assert runtime.file_read_state.check_stale(session.id, rules.resolve()) is None
        assert context.working_project_context == project_pin["text"]
