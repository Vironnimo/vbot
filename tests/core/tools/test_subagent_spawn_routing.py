"""Sub-agent tool registration, admission, Session creation, and routing tests."""

from __future__ import annotations

from .subagent_test_support import (
    SUBAGENT_RESULT_TOOL_NAME,
    SUBAGENT_TOOL_NAME,
    ChatMessage,
    FakeAgentResolver,
    FakeAgents,
    FakeRunManager,
    JsonObject,
    Path,
    RecordingTriggerService,
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


async def test_register_subagent_tools_registers_both_public_tools() -> None:
    # Arrange
    registry = ToolRegistry()
    trigger_service = RecordingTriggerService()
    tracker = SubAgentBatchTracker(trigger_service)
    coordinator = SubAgentCoordinator(SimpleNamespace(), trigger_service, batch_tracker=tracker)

    # Act
    register_subagent_tools(registry, coordinator)

    # Assert
    assert [tool.name for tool in registry.list_tools()] == [
        SUBAGENT_TOOL_NAME,
        SUBAGENT_RESULT_TOOL_NAME,
    ]


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
        emit_hook=lambda event_type, payload: emitted_events.append((event_type, payload))
    )

    # Act
    task = asyncio.create_task(
        _handle_subagent(
            context,
            {"content": "spawn", "background": False},
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
    assert isinstance(activity_file, str)
    assert Path(activity_file).exists()
    assert emitted_events[:2] == [
        (
            subagent_module.SUBAGENT_SESSION_STARTED_EVENT,
            {
                "tool_call": {"id": "tool-call-one", "index": 0, "name": "subagent"},
                "data": {
                    "agent_id": "parent",
                    "session_id": session_id,
                    "status": "running",
                    "activity_file": activity_file,
                },
            },
        ),
        (
            subagent_module.SUBAGENT_SESSION_STARTED_EVENT,
            {
                "tool_call": {"id": "tool-call-one", "index": 0, "name": "subagent"},
                "data": {
                    "agent_id": "parent",
                    "session_id": session_id,
                    "run_id": run.id,
                    "status": "running",
                    "activity_file": activity_file,
                },
            },
        ),
    ]

    run.mark_completed(ChatMessage.assistant(model="openai/gpt-5.2", content="done"))
    result = await task
    assert result["ok"] is True


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
        {"content": "spawn", "session_id": "existing-sub-session"},
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


async def test_reused_session_gets_a_distinct_activity_file_per_run(tmp_path: Path) -> None:
    manager = FakeRunManager()
    runtime = make_runtime(tmp_path, manager)
    tracker = SubAgentBatchTracker(RecordingTriggerService())
    context = make_context()
    runtime.chat_sessions.create(context.agent_id, session_id="existing-sub-session")

    first = await _handle_subagent(
        context,
        {"content": "first", "session_id": "existing-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )
    second = await _handle_subagent(
        context,
        {"content": "second", "session_id": "existing-sub-session"},
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
        {"content": "spawn", "session_id": "missing-sub-session"},
        runtime=runtime,
        batch_tracker=tracker,
    )

    # Assert
    assert result["ok"] is False
    assert result["error"]["code"] == "session_not_found"
    assert manager.started == []
