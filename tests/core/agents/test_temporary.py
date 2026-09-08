import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.agents import TemporaryAgentConfig, TemporaryAgentRegistry
from core.agents.temporary import TemporaryExecutionGroups, TemporaryRunInput
from core.chat import ChatError, ChatMessage, ChatSessionError
from core.runs import (
    ChatRunManager,
    RunAdmission,
    RunAdmissionBlockedError,
    RunExecutionOwner,
    RunNotFoundError,
)
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.format import write_bootstrap_marker
from core.storage import TemporaryFileManager
from core.tools.availability import ToolAccess
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    StubSkill,
    StubSkills,
    build_chat_loop,
)


def install_temporary_fixture_extension(runtime, tmp_path, owner_name):
    from core.extensions import ExtensionAPI, ExtensionRecord, ExtensionRegistry
    from core.extensions.extensions import ExtensionDeclarations
    from core.tools import tool_success

    declarations = ExtensionDeclarations()
    api = ExtensionAPI(owner_name, declarations, config={}, logger=None)
    api.register_session_tool(
        "fixture_private",
        "fixture",
        {"type": "object", "properties": {}, "required": []},
        lambda *_: tool_success({"fixture": True}),
    )
    api.register_session_prompt_block(
        "fixture-orientation", render=lambda _binding: "swarm-orientation"
    )
    runtime.extensions = ExtensionRegistry()
    runtime.extensions._records.append(
        ExtensionRecord(
            owner_name,
            tmp_path,
            tmp_path / "extension.py",
            "loaded",
            declarations=declarations,
        )
    )
    runtime.extensions.apply_tools(runtime.tools)


def test_temporary_agent_create_is_idempotent_without_identity_files(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    registry = TemporaryAgentRegistry(sessions)
    config = TemporaryAgentConfig(
        model="provider/model",
        cwd=tmp_path,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=["*"],
        tools={},
        name="Participant",
        fallback_models=["provider/fallback"],
        instructions="shared",
        prompt_blocks=["core:agent_body", "core:skills"],
    )

    first = registry.create(
        owner_name="extension", group_id="group", participant_id="participant", config=config
    )
    second = registry.create(
        owner_name="extension", group_id="group", participant_id="participant", config=config
    )

    assert second == first
    agent = registry.resolve(first.address, generation_id=first.generation_id)
    assert agent is not None
    assert agent.workspace == ""
    assert agent.fallback_models == ["provider/fallback"]
    assert agent.prompt_blocks == ["core:agent_body", "core:skills"]
    assert not (tmp_path / "agents").exists()
    sessions.close()

    reopened_sessions = ChatSessionManager(tmp_path)
    reopened = TemporaryAgentRegistry(reopened_sessions)
    restored = reopened.create(
        owner_name="extension", group_id="group", participant_id="participant", config=config
    )
    assert restored == first
    restored_agent = reopened.resolve(restored.address, generation_id=restored.generation_id)
    assert restored_agent is not None
    assert restored_agent.prompt_blocks == agent.prompt_blocks
    with pytest.raises(ChatSessionError):
        reopened.create(
            owner_name="extension",
            group_id="group",
            participant_id="participant",
            config=TemporaryAgentConfig(
                model="provider/other",
                cwd=tmp_path,
                tool_access=ToolAccess(mode="selected", allowed=()),
                allowed_skills=["*"],
                tools={},
                name="Participant",
            ),
        )
    reopened_sessions.close()


def test_temporary_agent_creation_is_race_safe_and_canonicalizes_config(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    first_sessions = ChatSessionManager(tmp_path)
    second_sessions = ChatSessionManager(tmp_path)
    config = TemporaryAgentConfig(
        model="provider/model",
        cwd=tmp_path,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=["shared"],
        tools={"nested": {"first": 1, "second": 2}},
        name="Participant",
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            bindings = list(
                pool.map(
                    lambda sessions: TemporaryAgentRegistry(sessions).create(
                        owner_name="extension",
                        group_id="group",
                        participant_id="participant",
                        config=config,
                    ),
                    (first_sessions, second_sessions),
                )
            )
        assert bindings[0] == bindings[1]
        assert (
            first_sessions.temporary_binding_by_participant(
                owner_name="extension", group_id="group", participant_id="participant"
            )
            == bindings[0]
        )
    finally:
        second_sessions.close()
        first_sessions.close()


def test_temporary_agent_config_is_an_immutable_snapshot(tmp_path: Path) -> None:
    tools = {"nested": {"allowed": ["first"]}}
    config = TemporaryAgentConfig(
        model="provider/model",
        cwd=tmp_path,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=["shared"],
        tools=tools,
        name="Participant",
    )
    tools["nested"]["allowed"].append("later")
    assert config.tools == {"nested": {"allowed": ["first"]}}
    assert tools == {"nested": {"allowed": ["first", "later"]}}

    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    try:
        binding = TemporaryAgentRegistry(sessions).create(
            owner_name="extension", group_id="group", participant_id="participant", config=config
        )
        config.tools["nested"]["allowed"].append("later")
        assert binding.config == {**binding.config, "tools": {"nested": {"allowed": ["first"]}}}
    finally:
        sessions.close()


def test_temporary_agent_config_rejects_relative_cwd(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cwd must be absolute"):
        TemporaryAgentConfig(
            model="provider/model",
            cwd=Path("relative"),
            tool_access=ToolAccess(),
            allowed_skills=[],
            tools={},
            name="Participant",
        )


@pytest.mark.asyncio
async def test_temporary_session_runs_only_through_protected_chat_path(tmp_path: Path) -> None:
    runtime = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="ordinary", model="openai/gpt-5.2"),
        adapter=StubAdapter([{"content": "participant result"}]),
    )
    install_temporary_fixture_extension(runtime, tmp_path, "swarm")
    registry = TemporaryAgentRegistry(runtime.chat_sessions)
    runtime.agent_resolver.temporary_agents = registry
    binding = registry.create(
        owner_name="swarm",
        group_id="group",
        participant_id="participant",
        config=TemporaryAgentConfig(
            model="openai/gpt-5.2",
            cwd=tmp_path,
            tool_access=ToolAccess(mode="selected", allowed=()),
            allowed_skills=[],
            tools={},
            name="Participant",
            instructions="Work only on the shared goal.",
        ),
    )
    loop = build_chat_loop(runtime)
    try:
        with pytest.raises(ChatError, match="managed by an Extension"):
            await loop.start_run(
                binding.address.agent_id, "bypass", session_id=binding.address.session_id
            )
        run = await loop.start_temporary_run(binding, "shared goal")
        result = await run.wait()
        assert result.content == "participant result"
        messages = runtime.chat_sessions.get(binding.address).load()
        assert [message.content for message in messages if message.role == "user"] == [
            "shared goal"
        ]
        assert runtime.skills_for_calls[-1] == (None, None)
        assert runtime.system_prompts.render_soul_calls == 0
        assert runtime.system_prompts.render_memory_files_calls == 0
    finally:
        runtime.chat_sessions.close()


@pytest.mark.asyncio
async def test_temporary_self_delegation_uses_parent_configuration_without_private_grants(
    tmp_path: Path,
) -> None:
    """A self-delegated child is a normal child Session under its parent's temporary config."""
    from core.subagents.subagents import SubAgentBatchTracker, _handle_subagent
    from core.tools import tool_success
    from core.tools.tools import ToolContext

    runtime = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="ordinary", model="openai/gpt-5.2"),
        adapter=StubAdapter([{"content": "child result"}]),
    )
    runtime.tools.register(
        "ordinary_tool",
        "Ordinary tool.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda *_args: tool_success({"ordinary": True}),
    )
    install_temporary_fixture_extension(runtime, tmp_path, "swarm")
    registry = TemporaryAgentRegistry(runtime.chat_sessions)
    runtime.agent_resolver.temporary_agents = registry
    binding = registry.create(
        owner_name="swarm",
        group_id="group",
        participant_id="participant",
        config=TemporaryAgentConfig(
            model="openai/gpt-5.2",
            cwd=tmp_path,
            tool_access=ToolAccess(mode="selected", allowed=("ordinary_tool",)),
            allowed_skills=["private-skill"],
            tools={},
            name="Participant",
            instructions="temporary instructions",
        ),
    )
    runtime.skills = StubSkills([StubSkill("private-skill", "private", tmp_path / "skill.md")])
    runtime_any = cast(Any, runtime)
    runtime_any.storage.temporary_files = TemporaryFileManager(tmp_path)
    runtime_any.storage.load_subagent_settings = lambda: {}
    resolved_overrides = []
    original_resolve_temporary = runtime.agent_resolver.resolve_temporary_agent

    def resolve_temporary_with_overrides(address, *, generation_id, run_overrides=None):
        resolved_overrides.append(run_overrides)
        agent = original_resolve_temporary(address, generation_id=generation_id)
        if run_overrides is None:
            return agent
        return replace(
            agent,
            model=run_overrides.model or agent.model,
            thinking_effort=run_overrides.thinking_effort or agent.thinking_effort,
        )

    runtime_any.agent_resolver.resolve_temporary_agent = resolve_temporary_with_overrides
    loop = build_chat_loop(runtime)
    runtime_any.streaming_chat_loop = loop
    owner = RunExecutionOwner("swarm", "group", "participant", binding.generation_id, "epoch")

    class TriggerService:
        def submit_completion(self, *_args, **_kwargs):
            delivered = asyncio.get_running_loop().create_future()
            delivered.set_result(None)
            return delivered

    try:
        result = await _handle_subagent(
            ToolContext(
                agent_id=binding.address.agent_id,
                session_id=binding.address.session_id,
                run_id="parent-run",
                tool_call_id="call",
                tool_name="subagent",
                tool_call_index=0,
                workspace=tmp_path,
                vbot_root=tmp_path,
                data_root=tmp_path,
                execution_owner=owner,
                nesting_depth=1,
            ),
            {
                "action": "run",
                "content": "delegate to yourself",
                "model": "openai/gpt-override",
                "thinking_effort": "low",
            },
            runtime=runtime_any,
            batch_tracker=SubAgentBatchTracker(TriggerService()),
        )

        assert result["ok"] is True
        child_id = result["data"]["session_id"]
        assert child_id != binding.address.session_id
        child_address = SessionAddress(None, binding.address.agent_id, child_id)
        assert runtime.chat_sessions.exists(child_address)
        run_result = runtime.chat_sessions.get(child_address).load_run_result(
            work_id=result["data"]["id"]
        )
        assert run_result is not None
        assert run_result.summary.run_id is not None
        child_run_id = run_result.summary.run_id
        child_run = runtime.chat_runs.get(child_run_id)
        assert child_run.execution_owner == owner
        assert child_run.agent_id == binding.address.agent_id
        assert child_run.session_id == child_id
        assert runtime.chat_sessions.temporary_binding(child_address) is None

        request = runtime.adapter.requests[0]
        assert request["model_id"] == "gpt-override"
        assert resolved_overrides
        assert all(item.model == "openai/gpt-override" for item in resolved_overrides)
        assert all(item.thinking_effort == "low" for item in resolved_overrides)
        assert {tool["name"] for tool in request["kwargs"]["tools"]} == {"ordinary_tool"}
        assert "fixture_private" not in str(request["kwargs"]["tools"])
        assert "swarm-orientation" not in str(request["messages"])
        assert runtime.skills_for_calls == [(None, None)]
        assert runtime.system_prompts.render_soul_calls == 0
        assert runtime.system_prompts.render_memory_files_calls == 0
    finally:
        runtime.chat_sessions.close()


@pytest.mark.asyncio
async def test_temporary_group_rejects_stale_registration_and_epoch(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    registry = TemporaryAgentRegistry(sessions)
    binding = registry.create(
        owner_name="extension",
        group_id="group",
        participant_id="participant",
        config=TemporaryAgentConfig(
            model="provider/model",
            cwd=tmp_path,
            tool_access=ToolAccess(),
            allowed_skills=[],
            tools={},
            name="P",
        ),
    )

    class Identity:
        name = "extension"
        epoch = "registration"

    current = {"value": True}
    groups = TemporaryExecutionGroups(
        registry,
        object(),
        lambda _identity: current["value"],
        Identity(),
        run_manager=ChatRunManager(),
    )
    handle = await groups.open_group("group")
    owner = RunExecutionOwner(
        "extension", "group", "participant", binding.generation_id, handle.epoch
    )
    groups.validate(binding.address, RunAdmission(owner=owner))
    await groups.close_group("group")
    await groups.open_group("group")
    with pytest.raises(RunAdmissionBlockedError):
        groups.validate(binding.address, RunAdmission(owner=owner))
    current["value"] = False
    with pytest.raises(RunAdmissionBlockedError):
        groups.validate(binding.address, RunAdmission(owner=owner))
    sessions.close()


@pytest.mark.asyncio
async def test_temporary_group_receipt_lookup_requires_its_exact_owner_and_generation(
    tmp_path: Path,
) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    registry = TemporaryAgentRegistry(sessions)
    binding = registry.create(
        owner_name="fixture",
        group_id="group",
        participant_id="peer",
        config=group_config(tmp_path),
    )
    sessions.append_messages_with_receipts(
        binding.address,
        generation_id=binding.generation_id,
        owner_name="fixture",
        messages=[ChatMessage.note("receipt carrier")],
        receipts=[(0, "receipt", "hash", "delivery", "note")],
        deduplicate_carrier=True,
    )
    identity = SimpleNamespace(name="fixture", epoch="registration")
    groups = TemporaryExecutionGroups(
        registry,
        object(),
        lambda candidate: candidate is identity,
        identity,
        run_manager=ChatRunManager(),
    )
    foreign_identity = SimpleNamespace(name="foreign", epoch="registration")
    foreign_groups = TemporaryExecutionGroups(
        registry,
        object(),
        lambda candidate: candidate is foreign_identity,
        foreign_identity,
        run_manager=ChatRunManager(),
    )
    try:
        receipt = await groups.delivery_receipt(binding.address, binding.generation_id, "receipt")
        assert receipt is not None
        assert receipt.carrier_location == {"kind": "note", "sequence": 0}
        with pytest.raises(RunNotFoundError, match="no longer available"):
            await groups.delivery_receipt(binding.address, "foreign-generation", "receipt")
        with pytest.raises(RunNotFoundError, match="no longer available"):
            await foreign_groups.delivery_receipt(binding.address, binding.generation_id, "receipt")
    finally:
        await groups.quiesce()
        await foreign_groups.quiesce()
        sessions.close()


class GroupTestChat:
    def __init__(self, sessions, manager):
        self.sessions = sessions
        self.manager = manager

    async def start_temporary_run(
        self, binding, _content, *, owner, input_id, input_already_persisted
    ):
        assert input_already_persisted

        async def execute(run):
            await self.sessions.record_run_owner_async(
                binding.address, run_id=run.id, owner=owner, input_id=input_id
            )
            session = self.sessions.get(binding.address)
            await session.append_async(
                ChatMessage.assistant(model="fixture/model", content="result")
            )
            await session.append_async(
                ChatMessage.run_summary(
                    run_id=run.id,
                    status="completed",
                    iteration_count=1,
                    timing={
                        "started_at": "2026-09-08T00:00:00+00:00",
                        "completed_at": "2026-09-08T00:00:01+00:00",
                        "duration_ms": 1000,
                    },
                )
            )

        return await self.manager.start(
            binding.address, execute, admission=RunAdmission(owner=owner)
        )


def group_config(tmp_path):
    return TemporaryAgentConfig(
        model="fixture/model",
        cwd=tmp_path,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=[],
        tools={},
        name="Peer",
    )


@pytest.mark.asyncio
async def test_group_initial_is_durable_idempotent_across_close_and_reopen(tmp_path):
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    current = SimpleNamespace(name="fixture", epoch="registration")
    manager = ChatRunManager()
    groups = TemporaryExecutionGroups(
        TemporaryAgentRegistry(sessions),
        GroupTestChat(sessions, manager),
        lambda identity: identity is current,
        current,
        run_manager=manager,
    )
    binding = await groups.create("group", "peer", group_config(tmp_path))
    handle = await groups.open_group("group")
    initial = TemporaryRunInput("initial", "goal", "request")
    first = await groups.start(handle, "peer", initial)
    await manager.get(first.run_id).wait()
    duplicate = await groups.start(handle, "peer", initial)
    assert duplicate.existing and duplicate.run_id == first.run_id
    await groups.close_group("group")
    await manager.aclose()
    sessions.close()

    sessions = ChatSessionManager(tmp_path)
    manager = ChatRunManager()
    groups = TemporaryExecutionGroups(
        TemporaryAgentRegistry(sessions),
        GroupTestChat(sessions, manager),
        lambda identity: identity is current,
        current,
        run_manager=manager,
    )
    try:
        handle = await groups.open_group("group")
        reopened = await groups.start(handle, "peer", initial)
        assert reopened.existing and reopened.run_id == first.run_id
        assert not manager.active_runs()
        resumed = await groups.start(
            handle, "peer", TemporaryRunInput("continuation", "resume", "resume-request")
        )
        await manager.get(resumed.run_id).wait()
        messages = sessions.get(binding.address).load()
        assert [message.content for message in messages if message.role == "user"] == ["goal"]
        assert [message.content for message in messages if message.role == "note"] == ["resume"]
        assert len(await groups.list("group")) == 1
        owner = manager.get(resumed.run_id).execution_owner
        acknowledgements = []
        completion_run = await groups.continue_completion(
            binding.address,
            owner,
            "background result",
            ("notice-one",),
            lambda: acknowledgements.append(True),
        )
        await completion_run.wait()
        assert completion_run.execution_owner == owner
        assert acknowledgements == [True]
        assert [
            message.content
            for message in sessions.get(binding.address).load()
            if message.role == "note"
        ] == ["resume", "background result"]
        assert (await groups.owned_run("group", first.run_id)).record.terminal_status == "completed"
        with pytest.raises(RunAdmissionBlockedError):
            await groups.start(
                handle, "peer", TemporaryRunInput("initial", "changed goal", "request")
            )
    finally:
        await groups.quiesce()
        await manager.aclose()
        sessions.close()


@pytest.mark.asyncio
async def test_group_close_during_receipt_lookup_rejects_before_history_write(
    tmp_path, monkeypatch
):
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    manager = ChatRunManager()
    identity = SimpleNamespace(name="fixture", epoch="registration")
    groups = TemporaryExecutionGroups(
        TemporaryAgentRegistry(sessions),
        GroupTestChat(sessions, manager),
        lambda _identity: True,
        identity,
        run_manager=manager,
    )
    binding = await groups.create("group", "peer", group_config(tmp_path))
    handle = await groups.open_group("group")
    entered, release = asyncio.Event(), asyncio.Event()
    original = sessions.lookup_delivery_receipt

    async def blocked_lookup(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(sessions, "lookup_delivery_receipt", blocked_lookup)
    start = asyncio.create_task(
        groups.start(handle, "peer", TemporaryRunInput("initial", "goal", "request"))
    )
    await entered.wait()
    stop = asyncio.create_task(groups.close_group("group"))
    await asyncio.sleep(0)
    assert not stop.done()
    release.set()
    with pytest.raises(RunAdmissionBlockedError):
        await start
    await stop
    assert sessions.get(binding.address).load() == []
    await manager.aclose()
    sessions.close()


@pytest.mark.asyncio
async def test_group_close_cancels_exact_descendant_and_queued_work(tmp_path):
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    identity = SimpleNamespace(name="fixture", epoch="registration")
    groups = None
    manager = ChatRunManager(
        admission_validator=lambda address, admission: groups.validate(address, admission)
    )
    groups = TemporaryExecutionGroups(
        TemporaryAgentRegistry(sessions),
        object(),
        lambda _identity: True,
        identity,
        run_manager=manager,
    )
    binding = await groups.create("group", "peer", group_config(tmp_path))
    handle = await groups.open_group("group")
    owner = RunExecutionOwner("fixture", "group", "peer", binding.generation_id, handle.epoch)
    target = SessionAddress(None, "ordinary", "reused")
    started = asyncio.Event()

    async def pending(_run):
        started.set()
        await asyncio.Event().wait()

    active = await manager.start(target, pending, admission=RunAdmission(owner=owner))
    await started.wait()
    queued = await manager.enqueue(target, pending, admission=RunAdmission(owner=owner))
    unrelated = await manager.start(SessionAddress(None, "ordinary", "other"), pending)
    assert groups.has_descendants("group", "peer", "parent", handle.epoch)
    report = await groups.close_group("group")
    assert active.id in report["run_ids"]
    assert queued.future.cancelled()
    assert active.status.value == "cancelled"
    assert unrelated.status.value == "running"
    with pytest.raises(RunAdmissionBlockedError):
        await manager.start(target, pending, admission=RunAdmission(owner=owner))
    with pytest.raises(RunAdmissionBlockedError):
        await manager.enqueue(target, pending, admission=RunAdmission(owner=owner))
    await manager.aclose()
    sessions.close()


@pytest.mark.asyncio
async def test_quiesce_waits_for_creation_and_permanently_retires_owner(tmp_path, monkeypatch):
    import threading

    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    manager = ChatRunManager()
    registry = TemporaryAgentRegistry(sessions)
    identity = SimpleNamespace(name="fixture", epoch="registration")
    groups = TemporaryExecutionGroups(
        registry,
        object(),
        lambda _identity: True,
        identity,
        run_manager=manager,
    )
    entered, release = threading.Event(), threading.Event()
    original = registry.create

    def blocked_create(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(**kwargs)

    monkeypatch.setattr(registry, "create", blocked_create)
    creation = asyncio.create_task(groups.create("group", "peer", group_config(tmp_path)))
    assert await asyncio.to_thread(entered.wait, 5)
    closing = asyncio.create_task(groups.quiesce())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(RunAdmissionBlockedError):
        await creation
    await closing
    with pytest.raises(RunAdmissionBlockedError):
        await groups.open_group("group")
    with pytest.raises(RunAdmissionBlockedError):
        await groups.create("other", "peer", group_config(tmp_path))
    await manager.aclose()
    sessions.close()


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_forty_temporary_participants_use_actual_chat_and_independent_history(tmp_path):
    # Keep all forty real Runs while allowing for durable writes on Windows CI disks.
    from tests.core.chat.chat_loop_support import (
        StubAdapter,
        StubAgent,
        StubRuntime,
        build_chat_loop,
    )

    adapter = StubAdapter([{"content": "fixture result"} for _ in range(40)])
    runtime = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="ordinary", model="openai/gpt-5.2"),
        adapter=adapter,
    )
    install_temporary_fixture_extension(runtime, tmp_path, "fixture")
    ordinary = runtime.chat_sessions.create("ordinary", session_id="source")
    ordinary.add_note("private-origin-sentinel")
    registry = TemporaryAgentRegistry(runtime.chat_sessions)
    runtime.agent_resolver.temporary_agents = registry
    identity = SimpleNamespace(name="fixture", epoch="registration")
    groups = TemporaryExecutionGroups(
        registry,
        build_chat_loop(runtime),
        lambda _identity: True,
        identity,
        run_manager=runtime.chat_run_manager,
    )
    config = TemporaryAgentConfig(
        model="openai/gpt-5.2",
        cwd=tmp_path,
        tool_access=ToolAccess(mode="selected", allowed=()),
        allowed_skills=[],
        tools={},
        name="Peer",
        instructions="shared-instructions-sentinel",
    )
    bindings = await asyncio.gather(
        *(groups.create("large-group", f"peer-{index}", config) for index in range(40))
    )
    handle = await groups.open_group("large-group")
    try:
        starts = await asyncio.gather(
            *(
                groups.start(
                    handle,
                    binding.participant_id,
                    TemporaryRunInput("initial", "explicit-goal-sentinel", "start"),
                )
                for binding in bindings
            )
        )
        await asyncio.gather(
            *(runtime.chat_run_manager.get(start.run_id).wait() for start in starts)
        )
        assert len({binding.address for binding in bindings}) == 40
        assert len(adapter.requests) == 40
        for binding, start in zip(bindings, starts, strict=True):
            history = runtime.chat_sessions.get(binding.address).load()
            assert [message.content for message in history if message.role == "user"] == [
                "explicit-goal-sentinel"
            ]
            assert (
                await groups.owned_run("large-group", start.run_id)
            ).record.address == binding.address
        for request in adapter.requests:
            assert "private-origin-sentinel" not in str(request["messages"])
            assert {tool["name"] for tool in request["kwargs"]["tools"]} == {"fixture_private"}
        assert runtime.chat_run_manager.all_queued() == []
        assert len(ordinary.load()) == 1
    finally:
        await groups.quiesce()
        await runtime.chat_run_manager.aclose()
        runtime.chat_sessions.close()
