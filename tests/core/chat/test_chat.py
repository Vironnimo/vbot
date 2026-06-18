"""Phase 2 chat validation tests."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage, ChatMessageValidationError
from core.chat.chat import ChatLoop, _validate_assistant_message
from core.projects import AgentResolutionError, ProjectStore
from core.runs import RunCancelledError
from core.tools import ToolContext, ToolRegistry, register_write_tool, tool_success


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

    with pytest.raises(ChatMessageValidationError, match="content, reasoning, reasoning_meta"):
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

    def make_handler(label: str):
        async def handler(_context: ToolContext, _arguments: dict) -> dict:
            # Yield so the cancel task can race with dispatch. The
            # post-dispatch code path (the persist loop) must then record
            # all sibling results before honoring the cancel.
            await asyncio.sleep(0.5)
            return tool_success({"sibling": label})

        return handler

    tools = ToolRegistry()
    tools.register("first_tool", "Fast tool.", {"type": "object"}, make_handler("first"))
    tools.register("second_tool", "Fast tool.", {"type": "object"}, make_handler("second"))
    tools.register("third_tool", "Fast tool.", {"type": "object"}, make_handler("third"))

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
    run = await ChatLoop(runtime).start_run("coder", "Multi", session_id="session-one")

    async def fire_cancel_after_dispatch() -> None:
        # Yield briefly so the chat loop runs the tool dispatch first.
        # Then flip the cancel flag directly (without cancelling the
        # chat-loop task) — that lets the gather finish and the persist
        # loop record every sibling result before `raise_if_cancelled`
        # is honored at the end of the persist block.
        await asyncio.sleep(0.1)
        run.cancel_requested = True

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
    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one", project_id="acme")

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
async def test_project_run_is_keyed_by_project_id(tmp_path: Path) -> None:
    # The run key carries project_id, so a project run and a global run sharing
    # one (agent_id, session_id) are distinct turn slots. Proof: while a project
    # run is active, a same-id GLOBAL run starts (no ActiveRunError), but a
    # same-id PROJECT run is rejected as already active.
    from core.runs import ActiveRunError
    from tests.core.chat.test_chat_loop import BlockingStubAdapter, StubAgent

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
    adapter = BlockingStubAdapter()
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=ToolRegistry())
    runtime.projects.create("acme", "Acme", repo_dir)
    runtime.chat_sessions.create("coder", session_id="session-one", project_id="acme")
    runtime.chat_sessions.create("coder", session_id="session-one")

    loop = ChatLoop(runtime)
    project_run = await loop.start_run("coder", "Hi", session_id="session-one", project_id="acme")
    await adapter.request_started.wait()

    # Same id under the global key is a different slot — it must start.
    global_run = await loop.start_run("coder", "Hi", session_id="session-one")
    # Same id under the same project key collides — it must be rejected.
    with pytest.raises(ActiveRunError):
        await loop.start_run("coder", "Hi", session_id="session-one", project_id="acme")

    adapter.release.set()
    await project_run.wait()
    await global_run.wait()


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
    register_write_tool(tools)
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=tools)
    project = runtime.projects.create("acme", "Acme", repo_dir)

    await ChatLoop(runtime).send(
        "coder", "Write a file", session_id="session-one", project_id="acme"
    )

    # The relative path resolved against the project cwd (the repo), not the
    # agent workspace.
    repo_file = Path(project.cwd) / "out.txt"
    workspace_file = tmp_path / "workspace-coder" / "out.txt"
    assert repo_file.read_text(encoding="utf-8") == "in-repo"
    assert not workspace_file.exists()


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
    register_write_tool(tools)
    runtime = _project_runtime(tmp_path, agent=agent, adapter=adapter, tools=tools)
    runtime.projects.create("acme", "Acme", tmp_path / "repo-unused")

    await ChatLoop(runtime).send("coder", "Write a file", session_id="session-one")

    identity_session = tmp_path / "agents" / "coder" / "sessions" / "session-one.jsonl"
    workspace_file = tmp_path / "workspace-coder" / "out.txt"
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

    await ChatLoop(runtime).send("coder", "Probe", session_id="session-one", project_id="acme")

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

    await ChatLoop(runtime).send("coder", "Probe", session_id="session-one")

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
    config_agent = StubAgent(
        id="orchestrator", model="openai/gpt-5.2-config", allowed_tools=["*"]
    )
    adapter = StubAdapter([{"content": "Hello from config agent", "tool_calls": None}])
    runtime = _project_runtime(
        tmp_path,
        agent=identity_agent,
        adapter=adapter,
        tools=ToolRegistry(),
        project_agents={("acme", "orchestrator"): config_agent},
    )
    runtime.projects.create("acme", "Acme", repo_dir)

    await ChatLoop(runtime).send(
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

    await ChatLoop(runtime).send("coder", "Hi", session_id="session-one")

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

    with pytest.raises(AgentResolutionError, match="not on project 'acme' team"):
        await ChatLoop(runtime).send(
            "coder", "Hi", session_id="session-one", project_id="acme"
        )


def persisted_roles_of(messages: list[ChatMessage]) -> list[str]:
    return [message.role for message in messages if message.role != "run_summary"]
