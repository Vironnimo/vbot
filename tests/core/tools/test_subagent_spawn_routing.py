"""Sub-agent tool registration, admission, Session creation, and routing tests."""

from __future__ import annotations

from core.projects import ModelConfigurationError
from core.tools.subagent import (
    SUBAGENT_TOOL_DESCRIPTION,
    SUBAGENT_TOOL_PARAMETERS,
)

from .subagent_test_support import (
    SUBAGENT_TOOL_NAME,
    Any,
    ChatMessage,
    FakeAgentResolver,
    FakeAgents,
    FakeChatLoop,
    FakeRunManager,
    JsonObject,
    Path,
    RecordingTriggerService,
    Run,
    SimpleNamespace,
    SubAgentBatchTracker,
    SubAgentCoordinator,
    ToolRegistry,
    _handle_subagent,
    asyncio,
    make_context,
    make_runtime,
    pytest,
    register_subagent_tools,
    subagent_module,
)

pytestmark = pytest.mark.asyncio


async def test_register_subagent_tools_registers_one_flat_public_tool() -> None:
    # Arrange
    registry = ToolRegistry()
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    coordinator = SubAgentCoordinator(SimpleNamespace(), trigger_service, batch_tracker=tracker)

    # Act
    register_subagent_tools(registry, coordinator)

    # Assert
    assert [tool.name for tool in registry.list_tools()] == [SUBAGENT_TOOL_NAME]
    subagent = registry.get(SUBAGENT_TOOL_NAME)
    assert subagent.description == (
        "Run, inspect, or cancel owned Sub-Agent work. Top-level run actions return "
        "immediately and deliver results automatically; run actions made by a Sub-Agent "
        "wait for completion."
    )
    assert subagent.description == SUBAGENT_TOOL_DESCRIPTION
    assert subagent.parameters == SUBAGENT_TOOL_PARAMETERS
    branches = {
        branch["properties"]["action"]["enum"][0]: branch for branch in subagent.parameters["oneOf"]
    }
    assert set(branches) == {"run", "status", "cancel"}
    run_properties = branches["run"]["properties"]
    assert set(run_properties) == {
        "action",
        "content",
        "agent_id",
        "session_id",
        "model",
        "thinking_effort",
    }
    assert branches["run"]["required"] == ["action", "content"]
    assert branches["run"]["if"] == {"required": ["session_id"]}
    assert branches["run"]["then"] == {"required": ["agent_id"]}
    for action in ("status", "cancel"):
        assert set(branches[action]["properties"]) == {"action", "id"}
        assert branches[action]["required"] == ["action", "id"]
    assert all(branch["additionalProperties"] is False for branch in branches.values())
    assert run_properties["action"]["description"] == (
        "run starts new work or continues a Sub-Agent Session, status returns a "
        "non-blocking snapshot for an id, and cancel stops the exact owned work for an id."
    )
    assert branches["status"]["properties"]["id"]["description"] == (
        "Stable id returned by run. Required for status and cancel."
    )
    assert run_properties["content"]["description"] == (
        "Instruction for action run. Without session_id, provide a self-contained task "
        "with the goal, relevant context, scope, constraints, and expected result. With "
        "session_id, provide a continuation message that may rely on that Sub-Agent "
        "Session's existing history."
    )
    assert run_properties["agent_id"]["description"] == (
        "Target Agent id from the allowed values for run. Omit for the calling Agent "
        "when creating a Session; required with session_id."
    )
    assert run_properties["session_id"]["description"] == (
        "Existing Sub-Agent Session id returned by run. With agent_id, continues that "
        "Session instead of creating one."
    )
    assert run_properties["model"]["description"] == (
        "Primary Model override for action run only, in <provider>/<model-id> form. "
        "Applies only to the newly admitted Run and does not modify the target Agent "
        "or Session."
    )
    assert run_properties["thinking_effort"]["enum"] == [
        "",
        "high",
        "low",
        "max",
        "medium",
        "minimal",
        "none",
        "xhigh",
    ]
    assert run_properties["thinking_effort"]["description"] == (
        "Thinking effort override for action run only. Omit to inherit the target Agent; "
        "an empty string selects the Provider default. Applies only to the newly admitted Run."
    )
    assert "background" not in run_properties
    assert "run_id" not in run_properties
    assert "queue_item_id" not in run_properties


async def test_subagent_tool_enforces_depth_limit(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager, {"max_subagent_depth": 2})
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context(nesting_depth=2)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "subagent_depth_exceeded"
    assert manager.started == []


async def test_subagent_tool_enforces_per_turn_limit(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager, {"max_subagents_per_turn": 1})
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    tracker.register((context.agent_id, context.session_id, context.run_id), "worker", "s1", "r1")

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "subagent_limit_exceeded"
    assert manager.started == []


async def test_subagent_tool_validates_target_agent_before_creating_session(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    # Restrict the known set so 'missing' fails resolution; the resolver wraps the
    # same restricted store the tool now validates through.
    restricted_agents = FakeAgents({"parent"})
    runtime.agents = restricted_agents
    runtime.agent_resolver = FakeAgentResolver(restricted_agents)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn", "agent_id": "missing"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "agent_not_found"
    assert manager.started == []
    assert list((tmp_path / "agents").glob("missing")) == []


async def test_subagent_tool_rejects_invalid_thinking_effort_before_creating_session(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    result = await _handle_subagent(
        context,
        {"content": "spawn", "thinking_effort": "extreme"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert manager.started == []
    assert runtime.chat_sessions.list(context.agent_id) == []


async def test_subagent_tool_rejects_unusable_model_before_creating_session(
    tmp_path: Path,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)

    class RejectingModelResolver(FakeAgentResolver):
        def resolve_agent(
            self,
            project_id: str | None,
            agent_id: str,
            *,
            run_overrides: Any | None = None,
        ) -> SimpleNamespace:
            if run_overrides is not None:
                raise ModelConfigurationError("model is not usable in this instance")
            return super().resolve_agent(project_id, agent_id)

    runtime.agent_resolver = RejectingModelResolver(runtime.agents)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    result = await _handle_subagent(
        context,
        {"content": "spawn", "model": "openai/ghost-model"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert "not usable" in result["error"]["message"]
    assert manager.started == []
    assert runtime.chat_sessions.list(context.agent_id) == []


async def test_subagent_tool_creates_new_session_when_no_session_id_provided(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    existing_sessions = runtime.chat_sessions.list(context.agent_id)

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    new_session_id = result["data"]["session_id"]
    assert manager.started[0][1] == new_session_id
    assert len(runtime.chat_sessions.list(context.agent_id)) == len(existing_sessions) + 1


async def test_subagent_tool_marks_created_session_with_parent_metadata(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    metadata = runtime.chat_sessions.get_metadata(
        result["data"]["agent_id"], result["data"]["session_id"]
    )
    assert metadata["is_subagent_session"] is True
    assert metadata["subagent_parent"] == {
        "id": result["data"]["id"],
        "agent_id": context.agent_id,
        "session_id": context.session_id,
        "run_id": context.run_id,
        "tool_call_id": context.tool_call_id,
        "tool_call_index": context.tool_call_index,
        "project_id": None,
    }


async def test_subagent_tool_emits_session_started_before_foreground_result(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    emitted_events: list[tuple[str, JsonObject]] = []
    context = make_context(
        nesting_depth=1,
        emit_hook=lambda event_type, payload: emitted_events.append((event_type, payload)),
    )

    # Act
    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "spawn"},
            runtime=runtime,
            batch_tracker=tracker,
        )
    )
    await asyncio.sleep(0)

    # Assert
    assert manager.started
    session_id = manager.started[0][1]
    run = manager.started[0][3]
    activity_file = emitted_events[0][1]["data"]["activity_file"]
    work_id = emitted_events[0][1]["data"]["id"]
    assert isinstance(activity_file, str)
    assert Path(activity_file).exists()
    assert emitted_events[:2] == [
        (
            subagent_module.SUBAGENT_SESSION_STARTED_EVENT,
            {
                "tool_call": {"id": "tool-call-one", "index": 0, "name": "subagent"},
                "data": {
                    "id": work_id,
                    "agent_id": "parent",
                    "session_id": session_id,
                    "status": "running",
                    "delivery": "inline",
                    "activity_file": activity_file,
                },
            },
        ),
        (
            subagent_module.SUBAGENT_SESSION_STARTED_EVENT,
            {
                "tool_call": {"id": "tool-call-one", "index": 0, "name": "subagent"},
                "data": {
                    "id": work_id,
                    "agent_id": "parent",
                    "session_id": session_id,
                    "run_id": run.id,
                    "status": "running",
                    "delivery": "inline",
                    "activity_file": activity_file,
                },
            },
        ),
    ]

    run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    result = await task
    assert result["ok"] is True
    assert result["data"]["id"] == work_id
    assert result["data"]["delivery"] == "inline"


async def test_subagent_tool_routes_into_existing_session_when_session_id_provided(
    tmp_path: Path,
) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="existing-sub-session")
    runtime.chat_sessions.set_metadata(
        context.agent_id,
        "existing-sub-session",
        {"platform": "telegram"},
    )
    existing_session_ids = [session.id for session in runtime.chat_sessions.list(context.agent_id)]

    # Act
    result = await _handle_subagent(
        context,
        {
            "content": "spawn",
            "agent_id": context.agent_id,
            "session_id": "existing-sub-session",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is True
    assert result["data"]["session_id"] == "existing-sub-session"
    assert manager.started[0][1] == "existing-sub-session"
    assert [
        session.id for session in runtime.chat_sessions.list(context.agent_id)
    ] == existing_session_ids
    metadata = runtime.chat_sessions.get_metadata(context.agent_id, "existing-sub-session")
    assert metadata["platform"] == "telegram"
    assert metadata["is_subagent_session"] is True
    assert metadata["subagent_parent"]["session_id"] == context.session_id


async def test_queued_subagent_runs_capture_independent_overrides(tmp_path: Path) -> None:
    FakeChatLoop.seen_agent_overrides = []
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    session_id = "busy-sub-session"
    runtime.chat_sessions.create(context.agent_id, session_id=session_id)
    manager.busy_sessions[(context.agent_id, session_id)] = Run(
        run_id="busy-run",
        agent_id=context.agent_id,
        session_id=session_id,
    )

    first = await _handle_subagent(
        context,
        {
            "content": "first",
            "agent_id": context.agent_id,
            "session_id": session_id,
            "model": "openai/gpt-mini",
            "thinking_effort": "low",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )
    second = await _handle_subagent(
        context,
        {
            "content": "second",
            "agent_id": context.agent_id,
            "session_id": session_id,
            "model": "openai/gpt-5.2",
            "thinking_effort": "max",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert len(manager.enqueued) == 2
    captured_overrides = [
        overrides for overrides in FakeChatLoop.seen_agent_overrides if overrides is not None
    ]
    assert len(captured_overrides) == 4
    assert [(overrides.model, overrides.thinking_effort) for overrides in captured_overrides] == [
        ("openai/gpt-mini", "low"),
        ("openai/gpt-mini", "low"),
        ("openai/gpt-5.2", "max"),
        ("openai/gpt-5.2", "max"),
    ]
    for record in manager.enqueued:
        record["run"].mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    await asyncio.sleep(0)


async def test_reused_session_gets_a_distinct_activity_file_per_run(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="existing-sub-session")

    first = await _handle_subagent(
        context,
        {
            "content": "first",
            "agent_id": context.agent_id,
            "session_id": "existing-sub-session",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )
    second = await _handle_subagent(
        context,
        {
            "content": "second",
            "agent_id": context.agent_id,
            "session_id": "existing-sub-session",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    first_path = Path(first["data"]["activity_file"])
    second_path = Path(second["data"]["activity_file"])
    assert first_path != second_path
    assert first_path.exists()
    assert second_path.exists()
    for record in manager.started:
        record[3].mark_completed(ChatMessage.assistant(model="test", content="done"))
    await asyncio.sleep(0)


async def test_activity_allocation_failure_does_not_block_subagent_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    def fail_create(_category: str, _suffix: str) -> object:
        raise OSError("disk unavailable")

    monkeypatch.setattr(runtime.storage.temporary_files, "create", fail_create)

    result = await _handle_subagent(
        context,
        {"content": "spawn"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is True
    assert result["data"]["activity_file"] is None
    assert "activity_note" not in result["data"]
    manager.started[0][3].mark_completed(ChatMessage.assistant(model="test", content="done"))
    await asyncio.sleep(0)


async def test_subagent_tool_rejects_nonexistent_session_id(tmp_path: Path) -> None:
    # Arrange
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()

    # Act
    result = await _handle_subagent(
        context,
        {
            "content": "spawn",
            "agent_id": context.agent_id,
            "session_id": "missing-sub-session",
        },
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "session_not_found"
    assert manager.started == []


async def test_subagent_tool_requires_agent_id_to_continue_session(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="existing-sub-session")

    result = await _handle_subagent(
        context,
        {"content": "continue", "session_id": "existing-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    assert result["ok"] is False
    assert result["error"] == {
        "code": "invalid_arguments",
        "message": (
            "agent_id is required with session_id because Sub-Agent Sessions are "
            "Agent-scoped; repeat both values returned by the original subagent call"
        ),
    }
    assert manager.started == []
