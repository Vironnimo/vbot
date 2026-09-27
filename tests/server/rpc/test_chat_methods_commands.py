"""Slash commands over ``chat.send`` / ``chat.stream``.

The RPC layer only routes a prepared command to Chat's command dispatcher and
projects the neutral outcome (reply, output hint, data, navigation, resource
changes, or a primary Run). Command workflows themselves belong to Chat; the
tests below drive them through the RPC because that is the path accessors use.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.automation.reflection import (
    COUNTER_GENERATION_KEY,
    REFLECTION_COUNTERS_META_KEY,
    ReflectionService,
)
from core.chat import (
    ChatMessage,
    CommandDispatcher,
    CommandExecutionContext,
    CommandFeedback,
    CommandNavigation,
    CommandOutcome,
    CommandResourceChange,
    ExtensionCommandContext,
    PreparedCommand,
    ReplySurface,
)
from core.chat.errors import ChatError
from core.runs import ChatRunManager, RunKind
from core.sessions import SessionAddress
from tests.server.rpc.chat_methods_test_support import (
    _core_dispatcher,
    _FakeRun,
    _RecordingLoop,
    call,
    chat_state,
    resource_changes,
)
from tests.server.rpc_test_support import (
    JsonObject,
    RecordingCompactionService,
    StubAdapter,
    make_state,
)

WEBUI = ReplySurface.webui()


def test_transport_layers_do_not_own_command_workflows() -> None:
    root = Path(__file__).parents[3]
    chat_source = (root / "server" / "rpc" / "chat_methods.py").read_text(encoding="utf-8")
    channel_source = (root / "core" / "channels" / "engine.py").read_text(encoding="utf-8")
    combined = f"{chat_source}\n{channel_source}"

    for forbidden in (
        "Command" + "Action",
        "Command" + "Handled",
        "Dispatch" + "Result",
        "_handle_command_" + "action",
        "unsupported command " + "action",
    ):
        assert forbidden not in combined
    for server_owned_workflow in (
        "HANDOFF_FRAGMENT_NAME",
        "LEARN_FRAGMENT_NAME",
        "AGENT_TAKEOVER_NOTE",
        "_build_handoff_prompt",
        "_build_learn_prompt",
        "_session_move_block_reason",
    ):
        assert server_owned_workflow not in chat_source
    for command in ("compact", "handoff", "learn", "reflect", "new", "rename", "model"):
        assert f'case "{command}"' not in combined


# ---------------------------------------------------------------------------
# Routing and outcome projection
# ---------------------------------------------------------------------------


class _NoticeDispatcher(CommandDispatcher):
    def __init__(self) -> None:
        super().__init__(ChatRunManager())
        self.calls: list[tuple[str, str, str, ReplySurface]] = []

    async def execute(
        self, prepared: PreparedCommand, context: CommandExecutionContext
    ) -> CommandOutcome:
        self.calls.append(
            (context.agent_id, context.session_id, f"/{prepared.name}", context.reply_surface)
        )
        return CommandOutcome(
            command=prepared.name, feedback=CommandFeedback(kind="notice", text="Run cancelled.")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_slash_command_is_executed_instead_of_starting_a_run(method: str) -> None:
    loop = _RecordingLoop()
    state = chat_state(loop)
    state.command_dispatcher = _NoticeDispatcher()

    response = await call(
        state, method, agent_id="agent-1", session_id="session-1", content="/stop"
    )

    assert response == {
        "ok": True,
        "result": {"command_handled": True, "reply": "Run cancelled.", "output": "toast"},
    }
    assert state.command_dispatcher.calls == [("agent-1", "session-1", "/stop", WEBUI)]
    assert loop.start_calls == []


def _page_outcome(context: ExtensionCommandContext, argument: str | None) -> CommandOutcome:
    return CommandOutcome(
        command="workflow",
        feedback=CommandFeedback(kind="notice", text="Workflow is ready."),
        navigation=CommandNavigation(
            kind="open_extension_page", extension="fixture", page="overview", route="items/one"
        ),
    )


async def _detail_outcome(context: ExtensionCommandContext, argument: str | None) -> CommandOutcome:
    # A change reported while running and again in the outcome is published once.
    # (Async so the report reaches the bus on the Event Loop, not from a worker.)
    change = CommandResourceChange(kind="commands")
    context.report_change(change)
    return CommandOutcome(
        command="workflow",
        feedback=CommandFeedback(kind="detail", text=f"Workflow {argument} ready."),
        resource_changes=(change,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "result", "changes"),
    [
        pytest.param(
            _detail_outcome,
            {"command_handled": True, "reply": "Workflow review ready.", "output": "transient"},
            [{"kind": "commands"}],
            id="detail",
        ),
        pytest.param(
            _page_outcome,
            {
                "command_handled": True,
                "reply": "Workflow is ready.",
                "output": "action",
                "data": {
                    "command": "workflow",
                    "navigation": {
                        "kind": "open_extension_page",
                        "extension": "fixture",
                        "page": "overview",
                        "route": "items/one",
                    },
                },
            },
            [],
            id="extension-page",
        ),
    ],
)
async def test_command_outcome_is_projected_generically(
    handler: Any, result: JsonObject, changes: list[JsonObject]
) -> None:
    loop = _RecordingLoop()
    state = chat_state(loop)
    state.command_dispatcher.register_extension_command(
        "fixture",
        name="workflow",
        description="Start the workflow.",
        handler=handler,
        page_ids=frozenset({"overview"}),
    )

    response = await call(
        state, "chat.stream", agent_id="agent-1", session_id="session-1", content="/workflow review"
    )

    assert response == {"ok": True, "result": result}
    assert resource_changes(state, "commands") == changes
    assert loop.start_calls == []


# ---------------------------------------------------------------------------
# Built-in commands on the real stack
# ---------------------------------------------------------------------------


async def _occupy(state: SimpleNamespace, session_id: str = "session-one") -> Any:
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_run: Any) -> str:
        started.set()
        await release.wait()
        return "done"

    run = await state.chat_runs.start(
        SessionAddress(project_id=None, agent_id="coder", session_id=session_id), execute
    )
    await started.wait()
    return run, release


@pytest.mark.asyncio
async def test_new_command_opens_and_selects_a_fresh_session(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await call(
        state, "chat.send", agent_id="coder", session_id="session-one", content="/new"
    )

    result = response["result"]
    assert result["command_handled"] is True
    assert result["data"]["command"] == "new"
    new_session_id = result["data"]["session_id"]
    assert new_session_id != "session-one"
    assert state.runtime.agents.get("coder").current_session_id == new_session_id
    address = SessionAddress(project_id=None, agent_id="coder", session_id=new_session_id)
    assert state.runtime.chat_sessions.get(address).load() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("session_busy", [True, False], ids=["run-active", "no-service"])
async def test_compact_command_is_refused_with_a_toast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_busy: bool
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter()
    compaction_service = RecordingCompactionService() if session_busy else None
    state = make_state(tmp_path, adapter, compaction_service=compaction_service)
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    occupant = await _occupy(state) if session_busy else None
    try:
        response = await call(
            state, "chat.send", agent_id="coder", session_id="session-one", content=" /COMPACT "
        )
    finally:
        if occupant is not None:
            run, release = occupant
            release.set()
            await run.wait()

    result = response["result"]
    assert result["command_handled"] is True
    assert result["output"] == "toast"
    assert result["reply"]
    assert compaction_service is None or compaction_service.calls == 0
    assert adapter.requests == []
    assert adapter.stream_requests == []


@pytest.mark.asyncio
async def test_compact_command_returns_its_compaction_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter(), compaction_service=RecordingCompactionService())
    session = state.runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Keep this context"))

    response = await call(
        state,
        "chat.stream",
        agent_id="coder",
        session_id="session-one",
        content="/compact keep the API design",
    )

    result = response["result"]
    assert "command_handled" not in result
    assert result["sse_url"] == f"/api/runs/{result['run_id']}/events"
    run = state.chat_runs.get(result["run_id"])
    checkpoint = await run.wait()
    assert checkpoint.role == "compaction_checkpoint"
    assert [event.type for event in run.events] == [
        "run_started",
        "compaction_started",
        "compaction_completed",
        "run_completed",
    ]
    completed = run.events[-2]
    assert completed.payload["message"]["content"] == "Compacted context"
    assert checkpoint.usage is not None
    assert completed.payload["context_tokens_before"] == checkpoint.usage["context_tokens_before"]
    assert completed.payload["context_tokens_after"] == checkpoint.usage["context_tokens_after"]
    assert completed.payload["context_tokens_after"] > 0
    assert completed.payload["checkpoint_id"] == checkpoint.id


@pytest.mark.asyncio
async def test_failed_compaction_run_fails_the_stream_run_and_the_send_request(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter()
    compaction_service = RecordingCompactionService()
    state = make_state(tmp_path, adapter, compaction_service=compaction_service)
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    state.runtime.agents.update("coder", model="")
    params = {"agent_id": "coder", "session_id": "session-one", "content": " /COMPACT "}

    streamed = await call(state, "chat.stream", **params)
    result = streamed["result"]
    assert result["status"] == "running"
    run = state.chat_runs.get(result["run_id"])
    with pytest.raises(ChatError):
        await run.wait()
    assert [event.type for event in run.events] == [
        "run_started",
        "compaction_started",
        "compaction_aborted",
        "run_failed",
    ]

    sent = await call(state, "chat.send", **params)
    assert sent["ok"] is False
    assert sent["error"]["code"] == "domain_error"
    assert compaction_service.calls == 0
    assert adapter.requests == []
    assert adapter.stream_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "target", "instruction"),
    [
        pytest.param("/handoff", "coder", None, id="same-agent"),
        pytest.param(
            "/handoff agent:reviewer don't forget the plates!",
            "reviewer",
            "don't forget the plates!",
            id="other-agent-with-instruction",
        ),
    ],
)
async def test_handoff_starts_the_target_in_a_new_current_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    target: str,
    instruction: str | None,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.create("reviewer", name="Reviewer")
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    started_runs: list[Any] = []
    state.chat_runs.add_run_started_callback(started_runs.append)

    response = await call(
        state, "chat.send", agent_id="coder", session_id="session-one", content=content
    )

    result = response["result"]
    assert result["command_handled"] is True
    assert result["reply"]
    assert result["data"]["command"] == "handoff"
    assert result["data"]["agent_id"] == target
    new_session_id = result["data"]["session_id"]
    assert new_session_id != "session-one"
    assert state.runtime.agents.get(target).current_session_id == new_session_id
    new_scope = {"kind": "sessions", "scope": {"agent_id": target, "session_id": new_session_id}}
    assert resource_changes(state, "sessions").count(new_scope) == 1
    # The Run-start seam observes the receiving Run without command-specific bridging.
    target_runs = [run for run in started_runs if run.session_id == new_session_id]
    assert [run.agent_id for run in target_runs] == [target]
    await target_runs[0].wait()
    new_history = state.runtime.chat_sessions.get(
        SessionAddress(project_id=None, agent_id=target, session_id=new_session_id)
    ).load()
    assert [message.content for message in new_history if message.role == "user"] == ["OK"]
    # The handoff-writing Run ran as an internal note on the source Session.
    source_notes = [
        str(message.content)
        for message in state.runtime.chat_sessions.get(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
        ).load()
        if message.role == "note"
    ]
    assert source_notes
    if instruction is not None:
        assert any(instruction in note for note in source_notes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "session_busy"),
    [("/handoff agent:ghost", False), ("/handoff", True)],
    ids=["unknown-target", "run-active"],
)
async def test_handoff_is_refused_without_creating_a_session(
    tmp_path: Path, content: str, session_busy: bool
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    state.runtime.agents.update("coder", current_session_id="session-one")
    occupant = await _occupy(state) if session_busy else None
    try:
        response = await call(
            state, "chat.send", agent_id="coder", session_id="session-one", content=content
        )
    finally:
        if occupant is not None:
            run, release = occupant
            release.set()
            await run.wait()

    result = response["result"]
    assert result["command_handled"] is True
    assert result["output"] == "toast"
    assert "data" not in result
    if not session_busy:
        assert "ghost" in result["reply"]
    sessions = state.runtime.chat_sessions.list_summaries("coder")
    assert [session["id"] for session in sessions] == ["session-one"]
    assert state.runtime.agents.get("coder").current_session_id == "session-one"


# ---------------------------------------------------------------------------
# /handoff, /learn and /reflect against recorded collaborators
# ---------------------------------------------------------------------------


def _fragment_storage() -> SimpleNamespace:
    """Prompt fragments the command briefs are read from."""
    fragments = {
        # The trailing newline must not leak into the handoff-writing prompt.
        "handoff.md": "Write a handoff for the next agent.\n",
        "learn.md": (
            "Author a reusable skill via the `skill_manage` tool: "
            "create it, then write support files."
        ),
        "reflect.md": "Review this session and update your memory and skill library.",
    }
    return SimpleNamespace(read_prompt_fragment=lambda name: fragments[name])


def _command_state(runtime: SimpleNamespace, *, active: bool = False) -> SimpleNamespace:
    active_run = _FakeRun() if active else None
    state = SimpleNamespace(
        chat_loop=_RecordingLoop(),
        streaming_chat_loop=_RecordingLoop(),
        runtime=runtime,
        chat_runs=SimpleNamespace(active_run=lambda **_kwargs: active_run),
        event_bus=None,
    )
    state.command_dispatcher = _core_dispatcher(state)
    return state


def _recording_trigger(calls: list[JsonObject]) -> SimpleNamespace:
    async def trigger_run(agent_id: str, message: Any, **kwargs: Any) -> _FakeRun:
        calls.append({"agent_id": agent_id, "message": message, **kwargs})
        return _FakeRun()

    return SimpleNamespace(trigger_run=trigger_run)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_address", "content", "target", "project_id", "instruction"),
    [
        pytest.param(
            "builder",
            "/handoff agent:orchestrator@vbot",
            "orchestrator",
            "vbot",
            None,
            id="project-target",
        ),
        pytest.param(
            "builder@vbot",
            "/handoff keep the deployment notes",
            "builder",
            "vbot",
            "keep the deployment notes",
            id="bare-stays-in-source-scope",
        ),
    ],
)
async def test_handoff_resolves_the_target_address_and_weaves_the_instruction(
    agent_address: str, content: str, target: str, project_id: str, instruction: str | None
) -> None:
    runs: list[JsonObject] = []
    resolved: list[tuple[str | None, str]] = []
    created_sessions: list[str] = []

    def resolve_agent(project: str | None, agent_id: str) -> SimpleNamespace:
        resolved.append((project, agent_id))
        return SimpleNamespace(id=agent_id)

    def create_session(agent_id: str, *, session_id: Any = None, project_id: Any = None) -> Any:
        created_sessions.append(f"{agent_id}@{project_id}")
        return SimpleNamespace(id="new-session")

    state = _command_state(
        SimpleNamespace(
            agent_resolver=SimpleNamespace(resolve_agent=resolve_agent),
            chat_sessions=SimpleNamespace(create=create_session),
            agents=SimpleNamespace(update=lambda *_args, **_kwargs: None),
            trigger_service=_recording_trigger(runs),
            storage=_fragment_storage(),
        )
    )

    response = await call(
        state, "chat.send", agent_id=agent_address, session_id="s1", content=content
    )

    assert response["result"]["data"]["session_id"] == "new-session"
    writer, receiver = runs
    assert writer["internal"] is True
    if instruction is None:
        assert writer["message"] == "Write a handoff for the next agent."
        assert (project_id, target) in resolved
    else:
        assert writer["message"].startswith("Write a handoff for the next agent.\n\n")
        assert writer["message"].endswith(instruction)
    assert receiver["agent_id"] == target
    assert receiver["project_id"] == project_id
    assert receiver["reply_surface"] == WEBUI
    assert created_sessions == [f"{target}@{project_id}"]


def _learn_runtime(runs: list[JsonObject], *, workspace: str = "/home/agent") -> SimpleNamespace:
    return SimpleNamespace(
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: SimpleNamespace(
                id=agent_id, workspace=workspace
            )
        ),
        trigger_service=_recording_trigger(runs),
        storage=_fragment_storage(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["the deploy steps", None])
async def test_learn_starts_an_internal_skill_authoring_run(argument: str | None) -> None:
    runs: list[JsonObject] = []
    state = _command_state(_learn_runtime(runs))
    content = "/learn" if argument is None else f"/learn {argument}"

    response = await call(state, "chat.send", agent_id="builder", session_id="s1", content=content)

    # The reply is the authoring Run's final answer.
    assert response["result"]["command_handled"] is True
    assert response["result"]["reply"] == "handoff text"
    [run] = runs
    assert run["internal"] is True
    assert run["reply_surface"] == WEBUI
    assert "skill_manage" in run["message"]
    if argument is not None:
        assert argument in run["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_address", "workspace", "active"),
    [("builder", "/home/agent", True), ("builder@vbot", "", False)],
    ids=["run-active", "config-agent"],
)
async def test_learn_is_refused_without_starting_a_run(
    agent_address: str, workspace: str, active: bool
) -> None:
    runs: list[JsonObject] = []
    state = _command_state(_learn_runtime(runs, workspace=workspace), active=active)

    response = await call(
        state, "chat.send", agent_id=agent_address, session_id="s1", content="/learn deploy"
    )

    assert response["result"]["command_handled"] is True
    assert response["result"]["reply"].strip()
    assert runs == []


class _ReflectSessions:
    """Session store double for ``/reflect``: records forks, titles and metadata."""

    def __init__(self) -> None:
        self.forks: list[JsonObject] = []
        self.titles: list[tuple[str, str]] = []
        self.metadata_writes: list[tuple[str, JsonObject]] = []

    async def fork(
        self,
        source: SessionAddress,
        *,
        target_agent_id: str | None = None,
        target_project_id: str | None = None,
        title: str | None = None,
        run_kind: RunKind | None = None,
    ) -> Any:
        self.forks.append(
            {
                "source_agent_id": source.agent_id,
                "session_id": source.session_id,
                "target_agent_id": target_agent_id,
                "target_project_id": target_project_id,
                "run_kind": run_kind,
            }
        )
        if title is not None:
            self.titles.append(("fork-1", title))
        return SimpleNamespace(id="fork-1")

    async def metadata_value_async(self, address: SessionAddress, key: str) -> Any:
        return None

    def get_metadata(self, address: SessionAddress) -> JsonObject:
        return {}

    def set_metadata(self, address: SessionAddress, data: JsonObject) -> None:
        self.metadata_writes.append((address.session_id, data))

    def mutate_metadata(self, address: SessionAddress, mutation: Any) -> JsonObject:
        metadata: JsonObject = {}
        mutation(metadata)
        self.metadata_writes.append((address.session_id, metadata))
        return metadata


def _reflect_state(
    runs: list[JsonObject],
    sessions: _ReflectSessions,
    *,
    workspace: str = "/home/agent",
    memory_prompt_mode: str = "agent_user",
    active: bool = False,
) -> SimpleNamespace:
    """State whose runtime carries a real ``ReflectionService`` over recorded I/O."""

    async def start_run(agent_id: str, content: Any, **kwargs: Any) -> _FakeRun:
        runs.append({"agent_id": agent_id, "message": content, **kwargs})
        return _FakeRun()

    agent = SimpleNamespace(
        name="Builder", workspace=workspace, memory_prompt_mode=memory_prompt_mode
    )

    async def resolve_agent_async(project_id: str | None, agent_id: str) -> SimpleNamespace:
        return agent

    runtime = SimpleNamespace(
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: agent,
            resolve_agent_async=resolve_agent_async,
        ),
        chat_sessions=sessions,
        storage=_fragment_storage(),
        streaming_chat_loop=SimpleNamespace(start_run=start_run),
    )
    runtime.reflection = ReflectionService(cast("Any", runtime))
    return _command_state(runtime, active=active)


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", ["focus on the memory side", None])
async def test_reflect_reviews_a_fork_with_a_restricted_run(focus: str | None) -> None:
    runs: list[JsonObject] = []
    sessions = _ReflectSessions()
    state = _reflect_state(runs, sessions)
    content = "/reflect" if focus is None else f"/reflect {focus}"

    response = await call(state, "chat.send", agent_id="builder", session_id="s1", content=content)

    # The fork stays in the source's scope and is classified as a reflection in
    # the same write that creates it; it is titled with the agent's name.
    assert sessions.forks == [
        {
            "source_agent_id": "builder",
            "session_id": "s1",
            "target_agent_id": None,
            "target_project_id": None,
            "run_kind": RunKind.REFLECTION,
        }
    ]
    assert sessions.titles == [("fork-1", "Builder")]
    [run] = runs
    assert run["session_id"] == "fork-1"
    assert run["internal"] is True
    assert run["run_kind"] is RunKind.REFLECTION
    assert run["tool_restriction"] == ("memory", "skill", "skill_manage")
    assert "tool_grants" not in run
    assert run["reply_surface"] == WEBUI
    assert run["message"].strip()
    if focus is not None:
        assert focus in run["message"]
    # A manual review covers both dimensions: the SOURCE Session's counters reset.
    assert sessions.metadata_writes == [
        (
            "s1",
            {
                REFLECTION_COUNTERS_META_KEY: {
                    "turns_since_memory_review": 0,
                    "iterations_since_skill_review": 0,
                    COUNTER_GENERATION_KEY: 1,
                }
            },
        )
    ]
    assert response["result"]["command_handled"] is True
    assert response["result"]["reply"] == "handoff text"
    assert response["result"]["data"] == {
        "command": "reflect",
        "session_id": "fork-1",
        "agent_id": "builder",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [{"active": True}, {"workspace": ""}, {"memory_prompt_mode": "off"}],
    ids=["run-active", "config-agent", "memory-inactive"],
)
async def test_reflect_is_refused_before_forking(refusal: dict[str, Any]) -> None:
    runs: list[JsonObject] = []
    sessions = _ReflectSessions()
    state = _reflect_state(runs, sessions, **refusal)

    response = await call(
        state, "chat.send", agent_id="builder", session_id="s1", content="/reflect"
    )

    assert response["result"]["reply"].strip()
    assert sessions.forks == []
    assert runs == []
