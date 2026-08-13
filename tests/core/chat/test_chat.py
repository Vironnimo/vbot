"""Phase 2 chat validation tests."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage, ChatMessageValidationError
from core.chat.chat import _validate_assistant_message
from core.projects import AgentResolutionError, ProjectStore
from core.runs import RunCancelledError
from core.tools import (
    FileReadState,
    ToolContext,
    ToolRegistry,
    register_history_tool,
    register_write_tool,
    tool_success,
)
from tests.core.chat.chat_loop_support import build_chat_loop


def test_validate_assistant_message_allows_reasoning_only() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
        reasoning="thinking only",
    )

    _validate_assistant_message(message)


def test_validate_assistant_message_allows_reasoning_meta_only() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
        reasoning_meta={"provider": "opaque"},
    )

    _validate_assistant_message(message)


def test_validate_assistant_message_rejects_truly_empty_assistant() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
    )

    with pytest.raises(ChatMessageValidationError):
        _validate_assistant_message(message)


@pytest.mark.asyncio
async def test_cancel_during_tool_dispatch_persists_all_sibling_tool_results(
    tmp_path: Path,
) -> None:
    # Arrange: three sibling tool calls. The run is cancelled after the
    # dispatch has returned all three results but before the persist loop
    # yields back to the outer agentic loop. The persist loop must record
    # every sibling result before honoring the cancel, so a later request
    # never sees a dangling tool_calls turn in the session history.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

    siblings_started: set[str] = set()
    all_siblings_started = asyncio.Event()
    release_siblings = asyncio.Event()

    def make_handler(label: str):
        async def handler(_context: ToolContext, _arguments: dict) -> dict:
            siblings_started.add(label)
            if len(siblings_started) == 3:
                all_siblings_started.set()
            await release_siblings.wait()
            return tool_success({"sibling": label})

        return handler

    tools = ToolRegistry()
    tools.register(
        "first_tool",
        "Fast tool.",
        {"type": "object"},
        make_handler("first"),
        parallel_safe=True,
    )
    tools.register(
        "second_tool",
        "Fast tool.",
        {"type": "object"},
        make_handler("second"),
        parallel_safe=True,
    )
    tools.register(
        "third_tool",
        "Fast tool.",
        {"type": "object"},
        make_handler("third"),
        parallel_safe=True,
    )

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_first", "name": "first_tool", "arguments": {}},
                    {"id": "call_second", "name": "second_tool", "arguments": {}},
                    {"id": "call_third", "name": "third_tool", "arguments": {}},
                ],
            }
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="session-one")

    # Act: start the run, then cancel it from a background task. The
    # cancel races with the in-flight tool dispatch; the persist loop
    # must persist all three sibling results before honoring the cancel.
    run = await build_chat_loop(runtime).start_run("coder", "Multi", session_id="session-one")

    async def fire_cancel_after_dispatch() -> None:
        # Flip the flag only after every sibling entered its handler. This keeps
        # the test independent of platform-specific task-startup timing while
        # still exercising cancellation during an in-flight parallel dispatch.
        await all_siblings_started.wait()
        run.cancel_requested = True
        release_siblings.set()

    cancel_task = asyncio.create_task(fire_cancel_after_dispatch())

    with pytest.raises(RunCancelledError):
        await run.wait()
    await cancel_task

    # Assert: all three tool results are persisted even though the run
    # ended cancelled, and the session never carries a dangling tool_calls
    # turn that would brick future provider requests.
    session = runtime.chat_sessions.get("coder", "session-one")
    persisted = session.load()
    tool_results = [message for message in persisted if message.role == "tool"]
    assert len(tool_results) == 3
    assert {message.tool_call_id for message in tool_results} == {
        "call_first",
        "call_second",
        "call_third",
    }
    # Run summary marks the run as cancelled.
    assert persisted[-1].role == "run_summary"
    assert persisted[-1].status == "cancelled"
    activity = runtime.chat_sessions.list_with_metadata("coder")[0]
    assert activity["run_kinds"] == ["user"]
    assert activity["has_unread_completion"] is True
    assert activity["unread_run_id"] == run.id
    assert activity["unread_run_status"] == "cancelled"


def _project_runtime(
    tmp_path: Path,
    *,
    agent: Any,
    adapter: Any,
    tools: Any,
    project_agents: dict[tuple[str, str], Any] | None = None,
    unresolvable_agents: set[tuple[str, str]] | None = None,
) -> Any:
    """Build a StubRuntime with a real ProjectStore wired onto it.

    The chat loop reads ``runtime.projects.get(project_id).cwd`` to resolve a
    project session's tool cwd, so a real store (not a stub) exercises the full
    ``str``-cwd → ``Path`` hand-off end-to-end. ``project_agents`` /
    ``unresolvable_agents`` drive the resolver's config branch so a test can prove
    a project run resolves a config agent (or fails cleanly) through the one seam.
    """
    from tests.core.chat.test_chat_loop import StubRuntime

    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        project_agents=project_agents,
        unresolvable_agents=unresolvable_agents,
    )
    runtime.projects = ProjectStore(tmp_path)
    return runtime


@pytest.mark.asyncio
async def test_skill_catalog_is_pinned_for_the_session(tmp_path: Path) -> None:
    # The catalog snapshot is taken once on a session's first build and reused, so a
    # skill written mid-session never changes the session's pinned catalog.
    from core.chat.chat import PINNED_SKILL_CATALOG_META_KEY
    from tests.core.chat.test_chat_loop import (
        StubAdapter,
        StubAgent,
        StubRuntime,
        StubSkill,
        StubSkills,
    )

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [{"content": "one", "tool_calls": None}, {"content": "two", "tool_calls": None}]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    runtime.chat_sessions.create("coder", session_id="s1")
    loop = build_chat_loop(runtime)

    await loop.send("coder", "hi", session_id="s1")
    pinned_after_first = dict(
        runtime.chat_sessions.get_metadata("coder", "s1")[PINNED_SKILL_CATALOG_META_KEY]
    )

    # A mid-session skill write: the live registry grows by one skill.
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )
    await loop.send("coder", "again", session_id="s1")
    pinned_after_second = runtime.chat_sessions.get_metadata("coder", "s1")[
        PINNED_SKILL_CATALOG_META_KEY
    ]

    # Snapshotted once, reused, and byte-identical despite the new skill.
    assert runtime.system_prompts.render_skill_catalog_calls == 1
    assert pinned_after_first == pinned_after_second
    assert pinned_after_first["catalog_text"] == "catalog:1"


@pytest.mark.asyncio
async def test_new_session_pins_a_fresh_catalog(tmp_path: Path) -> None:
    # A different session pins its own snapshot from the then-current registry, so a
    # skill added before it starts is included.
    from core.chat.chat import PINNED_SKILL_CATALOG_META_KEY
    from tests.core.chat.test_chat_loop import (
        StubAdapter,
        StubAgent,
        StubRuntime,
        StubSkill,
        StubSkills,
    )

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [{"content": "one", "tool_calls": None}, {"content": "two", "tool_calls": None}]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([StubSkill("one", "One.", Path("a"))])
    runtime.chat_sessions.create("coder", session_id="s1")
    runtime.chat_sessions.create("coder", session_id="s2")
    loop = build_chat_loop(runtime)

    await loop.send("coder", "hi", session_id="s1")
    runtime.skills = StubSkills(
        [StubSkill("one", "One.", Path("a")), StubSkill("two", "Two.", Path("b"))]
    )
    await loop.send("coder", "hi", session_id="s2")

    s1_catalog = runtime.chat_sessions.get_metadata("coder", "s1")[PINNED_SKILL_CATALOG_META_KEY]
    s2_catalog = runtime.chat_sessions.get_metadata("coder", "s2")[PINNED_SKILL_CATALOG_META_KEY]
    assert s1_catalog["catalog_text"] == "catalog:1"
    assert s2_catalog["catalog_text"] == "catalog:2"


@pytest.mark.asyncio
async def test_project_session_is_created_and_opened_under_project_anchor(
    tmp_path: Path,
) -> None:
    # Arrange: a project whose anchor lives under projects/<pid>/agents/<id>/.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=ToolRegistry())
    runtime.projects.create("acme", "Acme", repo_dir)

    # Act: run a turn scoped to the project.
    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one", project_id="acme")

    # Assert: the session file was created AND read under the project anchor,
    # never under the global identity layout.
    project_session = (
        tmp_path / "projects" / "acme" / "agents" / "coder" / "sessions" / "session-one.jsonl"
    )
    identity_session = tmp_path / "agents" / "coder" / "sessions" / "session-one.jsonl"
    assert project_session.exists()
    assert not identity_session.exists()
    persisted = runtime.chat_sessions.get("coder", "session-one", "acme").load()
    assert persisted_roles_of(persisted) == ["user", "assistant"]


@pytest.mark.asyncio
async def test_project_run_carries_project_id_and_dedups_per_session(tmp_path: Path) -> None:
    # A run started with a project_id carries it on the Run (for session I/O) and
    # the active-run slot is keyed on (project_id, agent_id, session_id) — the
    # project anchor is part of the key because session.create accepts
    # caller-chosen session ids. The project-scoped lookup finds the run, the
    # identity scope does not, and a second start in the same project session is
    # rejected as already active.
    from core.runs import ActiveRunError
    from tests.core.chat.test_chat_loop import BlockingStubAdapter, StubAgent

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStubAdapter()
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=ToolRegistry())
    runtime.projects.create("acme", "Acme", repo_dir)
    runtime.chat_sessions.create("coder", session_id="session-one", project_id="acme")

    loop = build_chat_loop(runtime)
    project_run = await loop.start_run("coder", "Hi", session_id="session-one", project_id="acme")
    await adapter.request_started.wait()

    # The project anchor rides the Run and scopes the key.
    assert project_run.project_id == "acme"
    assert (
        runtime.chat_run_manager.active_run(
            agent_id="coder", session_id="session-one", project_id="acme"
        )
        is project_run
    )
    # The identity scope must not see the project run (separate anchor).
    assert (
        runtime.chat_run_manager.active_run(
            agent_id="coder", session_id="session-one", project_id=None
        )
        is None
    )
    # A second start on the same project session is rejected as already active.
    with pytest.raises(ActiveRunError):
        await loop.start_run("coder", "Hi", session_id="session-one", project_id="acme")

    adapter.release.set()
    await project_run.wait()


@pytest.mark.asyncio
async def test_project_session_tool_resolves_relative_path_against_project_cwd(
    tmp_path: Path,
) -> None:
    # The plan risk this addresses: a file tool in a project session must write
    # into the repo, not the agent workspace. End-to-end through the chat loop:
    # a write with a relative path lands under the project cwd.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "write",
                        "arguments": {"path": "out.txt", "content": "in-repo"},
                    }
                ],
            },
            {"content": "Wrote the file.", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    register_write_tool(tools, file_state=FileReadState())
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=tools)
    project = runtime.projects.create("acme", "Acme", repo_dir)

    await build_chat_loop(runtime).send(
        "coder", "Write a file", session_id="session-one", project_id="acme"
    )

    # The relative path resolved against the project cwd (the repo), not the
    # agent workspace.
    repo_file = Path(project.cwd) / "out.txt"
    workspace_file = tmp_path / "agents" / "coder" / "workspace" / "out.txt"
    assert repo_file.read_text(encoding="utf-8") == "in-repo"
    assert not workspace_file.exists()


@pytest.mark.asyncio
async def test_project_run_persists_relative_assistant_output_file_reference(
    tmp_path: Path,
) -> None:
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    image = repo_dir / "result.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Done: file:result.png", "tool_calls": None}])
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=ToolRegistry())
    runtime.projects.create("acme", "Acme", repo_dir)

    result = await build_chat_loop(runtime).send(
        "coder",
        "Show the image",
        session_id="session-one",
        project_id="acme",
    )

    assert result.output_files is not None
    assert [reference.to_dict() for reference in result.output_files] == [
        {
            "line_index": 0,
            "path": str(image.resolve()),
            "start_index": 6,
            "end_index": 21,
        }
    ]
    persisted = runtime.chat_sessions.get("coder", "session-one", "acme").load()
    assistant = next(message for message in persisted if message.role == "assistant")
    assert assistant.output_files == result.output_files


@pytest.mark.asyncio
async def test_identity_session_unchanged_path_and_workspace_cwd(tmp_path: Path) -> None:
    # With project_id=None the session keeps the global identity layout and the
    # tool cwd stays the agent workspace — today's behavior, exactly unchanged.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "write",
                        "arguments": {"path": "out.txt", "content": "in-workspace"},
                    }
                ],
            },
            {"content": "Wrote the file.", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    register_write_tool(tools, file_state=FileReadState())
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.projects.create("acme", "Acme", tmp_path / "repo-unused")

    await build_chat_loop(runtime).send("coder", "Write a file", session_id="session-one")

    identity_session = tmp_path / "agents" / "coder" / "sessions" / "session-one.jsonl"
    workspace_file = tmp_path / "agents" / "coder" / "workspace" / "out.txt"
    assert identity_session.exists()
    assert workspace_file.read_text(encoding="utf-8") == "in-workspace"


@pytest.mark.asyncio
async def test_project_run_threads_project_id_to_tool_context(tmp_path: Path) -> None:
    # End-to-end: a run scoped to a project must set ToolContext.project_id on
    # every tool call, so the subagent tool can inherit the parent run's project.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    seen: list[str | None] = []

    def project_probe(context: ToolContext, _arguments: dict) -> dict:
        seen.append(context.project_id)
        return tool_success({"project_id": context.project_id})

    tools = ToolRegistry()
    tools.register("project_probe", "Probe project id.", {"type": "object"}, project_probe)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "project_probe", "arguments": {}}],
            },
            {"content": "Done.", "tool_calls": None},
        ]
    )
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.projects.create("acme", "Acme", repo_dir)

    await build_chat_loop(runtime).send(
        "coder", "Probe", session_id="session-one", project_id="acme"
    )

    assert seen == ["acme"]


@pytest.mark.asyncio
async def test_identity_run_leaves_tool_context_project_id_none(tmp_path: Path) -> None:
    # The identity path (project_id=None) keeps ToolContext.project_id None.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent

    seen: list[str | None] = []

    def project_probe(context: ToolContext, _arguments: dict) -> dict:
        seen.append(context.project_id)
        return tool_success({"project_id": context.project_id})

    tools = ToolRegistry()
    tools.register("project_probe", "Probe project id.", {"type": "object"}, project_probe)
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "project_probe", "arguments": {}}],
            },
            {"content": "Done.", "tool_calls": None},
        ]
    )
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=tools)

    await build_chat_loop(runtime).send("coder", "Probe", session_id="session-one")

    assert seen == [None]


@pytest.mark.asyncio
async def test_project_run_resolves_config_agent_through_resolver(tmp_path: Path) -> None:
    # A project run must resolve the project's config agent (not the identity
    # store agent) through the one resolver seam, and run on its resolved model.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    identity_agent = StubAgent(id="orchestrator", model="openai/gpt-5.2", allowed_tools=["*"])
    # The config agent shares the agent id but carries a distinct resolved model,
    # so the model reaching the wire proves the config profile was used.
    config_agent = StubAgent(id="orchestrator", model="openai/gpt-5.2-config", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello from config agent", "tool_calls": None}])
    runtime = _project_runtime(
        tmp_path,
        agent=identity_agent,
        adapter=adapter,
        tools=ToolRegistry(),
        project_agents={("acme", "orchestrator"): config_agent},
    )
    runtime.projects.create("acme", "Acme", repo_dir)

    await build_chat_loop(runtime).send(
        "orchestrator", "Hi", session_id="session-one", project_id="acme"
    )

    # The resolver was asked for the project agent, and its model reached the wire.
    assert ("acme", "orchestrator") in [
        (project_id, agent_id) for project_id, agent_id in runtime.agent_resolver.calls
    ]
    assert adapter.requests[0]["model_id"] == "gpt-5.2-config"


@pytest.mark.asyncio
async def test_identity_run_resolves_store_agent_unchanged(tmp_path: Path) -> None:
    # The identity path resolves with project_id=None and runs the store agent's
    # model exactly as before — no project profile involved.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Hello", "tool_calls": None}])
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=ToolRegistry())

    await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    # Every resolve on this path is the identity branch (project_id=None); the
    # store agent's model reaches the wire unchanged. (send resolves in both
    # _start_run and _execute_run, hence more than one call.)
    assert runtime.agent_resolver.calls
    assert all(call == (None, "coder") for call in runtime.agent_resolver.calls)
    assert adapter.requests[0]["model_id"] == "gpt-5.2"


@pytest.mark.asyncio
async def test_unresolvable_project_agent_raises_clear_error(tmp_path: Path) -> None:
    # A project agent that the resolver cannot resolve (off-Team / no usable model)
    # surfaces a clear AgentResolutionError instead of crashing the run path.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "unused", "tool_calls": None}])
    runtime = _project_runtime(
        tmp_path,
        agent=agent,
        adapter=adapter,
        tools=ToolRegistry(),
        unresolvable_agents={("acme", "coder")},
    )
    runtime.projects.create("acme", "Acme", repo_dir)

    with pytest.raises(AgentResolutionError):
        await build_chat_loop(runtime).send(
            "coder", "Hi", session_id="session-one", project_id="acme"
        )


@pytest.mark.asyncio
async def test_tool_restriction_denies_at_dispatch_without_changing_definitions(
    tmp_path: Path,
) -> None:
    # A restricted run may only dispatch the restricted tools; every other call
    # fails through the tool_not_allowed path. The provider tool definitions
    # offered on the wire stay byte-identical to an unrestricted run — the
    # restriction is dispatch-only, so the prompt/tool-definition cache is intact.
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

    def make_recording_tool(ran: list[str], label: str):
        async def handler(_context: ToolContext, _arguments: dict) -> dict:
            ran.append(label)
            return tool_success({"tool": label})

        return handler

    def build(ran: list[str], session_id: str) -> tuple[Any, StubAdapter]:
        tools = ToolRegistry()
        tools.register(
            "memory", "Memory stub.", {"type": "object"}, make_recording_tool(ran, "memory")
        )
        tools.register(
            "weather", "Weather stub.", {"type": "object"}, make_recording_tool(ran, "weather")
        )
        agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
        adapter = StubAdapter(
            [
                {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_mem", "name": "memory", "arguments": {}},
                        {"id": "call_weather", "name": "weather", "arguments": {}},
                    ],
                },
                {"content": "done", "tool_calls": []},
            ]
        )
        runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
        runtime.chat_sessions.create("coder", session_id=session_id)
        return runtime, adapter

    # Restricted run: only ``memory`` may dispatch; ``weather`` is denied.
    restricted_ran: list[str] = []
    restricted_runtime, restricted_adapter = build(restricted_ran, "restricted")
    run = await build_chat_loop(restricted_runtime).start_run(
        "coder",
        "Go",
        session_id="restricted",
        tool_restriction=("memory", "skill", "skill_manage"),
    )
    await run.wait()

    persisted = restricted_runtime.chat_sessions.get("coder", "restricted").load()
    results = {m.tool_call_id: json.loads(m.content) for m in persisted if m.role == "tool"}
    assert restricted_ran == ["memory"]
    assert results["call_mem"]["ok"] is True
    assert results["call_weather"]["ok"] is False
    assert results["call_weather"]["error"]["code"] == "tool_not_allowed"

    # Unrestricted run: both tools dispatch, and the offered definitions match.
    unrestricted_ran: list[str] = []
    unrestricted_runtime, unrestricted_adapter = build(unrestricted_ran, "unrestricted")
    run = await build_chat_loop(unrestricted_runtime).start_run(
        "coder", "Go", session_id="unrestricted"
    )
    await run.wait()

    assert sorted(unrestricted_ran) == ["memory", "weather"]
    restricted_request = restricted_adapter.requests[0]
    unrestricted_request = unrestricted_adapter.requests[0]
    assert (
        json.dumps(
            restricted_request["kwargs"]["tools"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        == json.dumps(
            unrestricted_request["kwargs"]["tools"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    assert restricted_request["messages"][0]["content"].encode() == (
        unrestricted_request["messages"][0]["content"].encode()
    )


@pytest.mark.asyncio
async def test_same_scope_fork_reuses_cache_affinity_but_not_session_context(
    tmp_path: Path,
) -> None:
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

    class RequestContextAdapter(StubAdapter):
        def __init__(self) -> None:
            super().__init__(
                [
                    {"content": "Source answer", "tool_calls": None},
                    {"content": "Fork answer", "tool_calls": None},
                ]
            )
            self.context_calls: list[dict[str, Any]] = []

        def request_context_kwargs(
            self,
            *,
            agent_id: str,
            session_id: str,
            project_id: str | None = None,
            prompt_cache_affinity_id: str | None = None,
        ) -> dict[str, Any]:
            context = {
                "agent_id": agent_id,
                "session_id": session_id,
                "project_id": project_id,
                "prompt_cache_affinity_id": prompt_cache_affinity_id,
            }
            self.context_calls.append(context)
            return {
                "transport_probe": session_id,
                "cache_probe": prompt_cache_affinity_id,
            }

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = RequestContextAdapter()
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    source = runtime.chat_sessions.create("coder", session_id="source")
    loop = build_chat_loop(runtime)

    source_run = await loop.start_run("coder", "Build it", session_id=source.id)
    await source_run.wait()
    fork = await runtime.chat_sessions.fork("coder", source.id)
    fork_run = await loop.start_run("coder", "Review it", session_id=fork.id)
    await fork_run.wait()

    assert [call["session_id"] for call in adapter.context_calls] == [source.id, fork.id]
    source_affinity, fork_affinity = [
        call["prompt_cache_affinity_id"] for call in adapter.context_calls
    ]
    assert source_affinity == fork_affinity
    assert adapter.requests[0]["kwargs"]["transport_probe"] == source.id
    assert adapter.requests[1]["kwargs"]["transport_probe"] == fork.id
    assert adapter.requests[0]["kwargs"]["cache_probe"] == source_affinity
    assert adapter.requests[1]["kwargs"]["cache_probe"] == source_affinity


@pytest.mark.asyncio
async def test_skill_catalog_call_uses_stable_definition_during_restriction(
    tmp_path: Path,
) -> None:
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

    def build(session_id: str, *, invoke: bool = False) -> tuple[Any, StubAdapter, list[str]]:
        ran: list[str] = []

        def list_skills(_context: ToolContext, _arguments: dict) -> dict:
            ran.append("skill")
            return tool_success({"skill_groups": [], "count": 0})

        tools = ToolRegistry()
        tools.register(
            "skill",
            "Load one Skill.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
            list_skills,
        )
        agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["skill"])
        responses = (
            [
                {
                    "content": None,
                    "tool_calls": [{"id": "list-call", "name": "skill", "arguments": {}}],
                },
                {"content": "done", "tool_calls": []},
            ]
            if invoke
            else [{"content": "done", "tool_calls": []}]
        )
        adapter = StubAdapter(responses)
        runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)
        runtime.chat_sessions.create("coder", session_id=session_id)
        return runtime, adapter, ran

    normal_runtime, normal_adapter, normal_ran = build("normal")
    normal_run = await build_chat_loop(normal_runtime).start_run(
        "coder", "Continue", session_id="normal"
    )
    await normal_run.wait()

    reflection_runtime, reflection_adapter, reflection_ran = build("reflection", invoke=True)
    reflection_run = await build_chat_loop(reflection_runtime).start_run(
        "coder",
        "Reflect",
        session_id="reflection",
        tool_restriction=("skill",),
    )
    await reflection_run.wait()

    normal_definitions = normal_adapter.requests[0]["kwargs"]["tools"]
    reflection_definitions = reflection_adapter.requests[0]["kwargs"]["tools"]
    assert [definition["name"] for definition in normal_definitions] == ["skill"]
    assert reflection_definitions == normal_definitions
    assert (
        normal_adapter.requests[0]["messages"][0]["content"]
        == (reflection_adapter.requests[0]["messages"][0]["content"])
    )
    assert normal_runtime.system_prompts.effective_tool_name_calls == [("skill",)]
    assert reflection_runtime.system_prompts.effective_tool_name_calls == [("skill",)]
    assert normal_ran == []
    assert reflection_ran == ["skill"]


@pytest.mark.asyncio
async def test_checkpoint_granted_history_stays_advertised_when_run_restricts_dispatch(
    tmp_path: Path,
) -> None:
    from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=[])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "history-call",
                        "name": "history",
                        "arguments": {"action": "overview"},
                    }
                ],
            },
            {"content": "done", "tool_calls": []},
        ]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="restricted-history")
    session.append(ChatMessage.user("Earlier request"))
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Earlier context",
            projection=[],
            compacted_token_count=10,
        )
    )

    run = await build_chat_loop(runtime).start_run(
        "coder",
        "Continue",
        session_id=session.id,
        tool_restriction=("memory",),
    )
    await run.wait()

    tool_names_by_request = [
        [tool["name"] for tool in request["kwargs"]["tools"]] for request in adapter.requests
    ]
    result = next(message for message in session.load() if message.tool_call_id == "history-call")
    assert tool_names_by_request == [["history"], ["history"]]
    assert json.loads(str(result.content))["error"]["code"] == "tool_not_allowed"


def persisted_roles_of(messages: list[ChatMessage]) -> list[str]:
    return [message.role for message in messages if message.role != "run_summary"]
