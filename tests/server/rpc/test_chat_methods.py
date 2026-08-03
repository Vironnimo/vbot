"""Tests for project-aware addressing on the chat RPC handlers.

Coverage (AAA):
- ``chat.send`` with a bare agent id starts an identity run (``project_id=None``),
  byte-identical to before,
- ``chat.send`` with ``agent@projekt`` parses the address and runs project-scoped
  (``project_id`` threaded into ``start_run``),
- ``chat.send`` with a malformed address is ``invalid_request`` before any run,
- ``chat.stream`` threads the same ``project_id`` into the streaming loop,
- a ``/handoff agent:orchestrator@vbot`` targets the project: the receiving run
  and the new session are created under that project,
- a bare ``/handoff`` stays in the source scope.
"""

from __future__ import annotations

import asyncio
import json
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
    CommandOutcome,
    ReplySurface,
)
from core.chat.content_blocks import FileMentionBlock, TextBlock
from core.projects import AgentResolutionError, ModelConfigurationError, format_agent_address
from core.runs import ActiveRunError, ChatRunManager, RunAdmissionBlockedError, RunKind
from core.sessions import SESSION_FORK_ALWAYS_STRIP_META_KEYS, SESSION_MOVE_STRIP_META_KEYS
from core.tools.file_state import FileReadState
from core.tools.terminal_manager import TerminalOwner
from server.events import ServerEventBus
from server.rpc.chat_methods import (
    _chat_queue_remove,
    _chat_queue_update,
    _send_chat,
    _stream_chat,
)
from server.rpc.errors import RpcError


class _FakeRun:
    def __init__(self, run_id: str = "run-1") -> None:
        self.id = run_id
        self.agent_id = "builder"
        self.session_id = "s1"
        # ``_run_response`` reads ``status.value`` and ``events``; a finished run
        # with no events is enough for these address-threading assertions.
        self.status = SimpleNamespace(value="completed")
        self.run_kind = RunKind.USER
        self.events: list[Any] = []

    async def wait(self) -> ChatMessage:
        return ChatMessage.assistant(content="handoff text", model="openai/gpt-5.2")


class _RecordingLoop:
    """Records the ``project_id`` each public entry was called with."""

    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []

    async def start_run(self, agent_id: str, content: Any, **kwargs: Any) -> _FakeRun:
        self.start_calls.append({"agent_id": agent_id, "content": content, **kwargs})
        return _FakeRun()


class _NoCommandDispatcher:
    """Treats every message as plain chat (no slash command recognized)."""

    def prepare(self, content: Any) -> None:
        return None


def _core_dispatcher(state: SimpleNamespace) -> CommandDispatcher:
    runtime = state.runtime
    return CommandDispatcher(
        state.chat_runs,
        agent_resolver=getattr(runtime, "agent_resolver", None),
        sessions=getattr(runtime, "chat_sessions", None),
        models=getattr(runtime, "models", None),
        projects=getattr(runtime, "projects", None),
        agents=getattr(runtime, "agents", None),
        trigger_service=getattr(runtime, "trigger_service", None),
        reflection_service=getattr(runtime, "reflection", None),
        storage=getattr(runtime, "storage", None),
        terminal_manager=getattr(runtime, "terminal_manager", None),
    )


async def _execute_core_command(
    state: SimpleNamespace,
    message: str,
    *,
    agent_id: str = "builder",
    session_id: str = "s1",
    project_id: str | None = None,
) -> CommandOutcome:
    dispatcher = _core_dispatcher(state)
    prepared = dispatcher.prepare(message)
    assert prepared is not None
    observed_changes = getattr(state, "_command_changes", None)
    return await dispatcher.execute(
        prepared,
        CommandExecutionContext(
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            reply_surface=ReplySurface.webui(),
            on_change=observed_changes.append if observed_changes is not None else None,
        ),
    )


def _make_state(loop: _RecordingLoop) -> SimpleNamespace:
    # The bridge helper reads the event bus; a no-op namespace is enough since the
    # tests assert on the recorded loop call, not on bridged events.
    event_bus = SimpleNamespace(publish=lambda *a, **k: None)
    runtime = SimpleNamespace()
    return SimpleNamespace(
        chat_loop=loop,
        streaming_chat_loop=loop,
        runtime=runtime,
        event_bus=event_bus,
        chat_runs=SimpleNamespace(),
        command_dispatcher=_NoCommandDispatcher(),
    )


@pytest.mark.asyncio
async def test_send_bare_agent_runs_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["agent_id"] == "builder"
    assert loop.start_calls[0]["project_id"] is None
    assert loop.start_calls[0]["reply_surface"] == ReplySurface.webui()


@pytest.mark.asyncio
async def test_send_qualified_agent_runs_project_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder@vbot", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["agent_id"] == "builder"
    assert loop.start_calls[0]["project_id"] == "vbot"


@pytest.mark.asyncio
async def test_send_expands_file_mentions_into_snapshot_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # An @-mentioned file is snapshotted before the loop sees the content: the
    # string message becomes blocks (original text first, then the snapshot),
    # and the file is stamped as read for the session.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.md").write_text("snapshot body", encoding="utf-8")
    file_state = FileReadState()
    loop = _RecordingLoop()
    state = _make_state(loop)
    state.runtime = SimpleNamespace(
        projects=SimpleNamespace(get=lambda project_id: SimpleNamespace(cwd="")),
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: SimpleNamespace(workspace=str(workspace))
        ),
        storage=SimpleNamespace(data_dir=str(tmp_path)),
        file_read_state=file_state,
    )
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(
        state,
        {
            "agent_id": "builder",
            "session_id": "s1",
            "content": "look at @notes.md",
            "file_mentions": ["notes.md"],
        },
    )

    content = loop.start_calls[0]["content"]
    assert isinstance(content, list)
    assert content[0] == TextBlock(type="text", text="look at @notes.md")
    assert isinstance(content[1], FileMentionBlock)
    assert content[1].status == "inlined"
    assert content[1].text == "snapshot body"
    assert file_state.check_stale("s1", (workspace / "notes.md").resolve()) is None


@pytest.mark.asyncio
async def test_send_without_file_mentions_keeps_string_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["content"] == "hi"


@pytest.mark.asyncio
async def test_send_invalid_address_is_invalid_request() -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)

    with pytest.raises(RpcError) as exc_info:
        await _send_chat(
            state, {"agent_id": "builder@bad project", "session_id": "s1", "content": "hi"}
        )

    assert exc_info.value.code == "invalid_request"
    assert loop.start_calls == []


@pytest.mark.asyncio
async def test_stream_qualified_agent_runs_project_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _RecordingLoop()
    state = _make_state(loop)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _stream_chat(state, {"agent_id": "tester@vbot", "session_id": "s1", "content": "hi"})

    assert loop.start_calls[0]["agent_id"] == "tester"
    assert loop.start_calls[0]["project_id"] == "vbot"
    assert loop.start_calls[0]["reply_surface"] == ReplySurface.webui()


# ---------------------------------------------------------------------------
# Handoff: target address resolution and project-scoped receiving run.
# ---------------------------------------------------------------------------


class _HandoffLoop:
    """Captures the handoff-writing and receiving runs with their project ids."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start_run(self, agent_id: str, content: Any, **kwargs: Any) -> _FakeRun:
        self.calls.append({"agent_id": agent_id, **kwargs})
        return _FakeRun()


class _FakeResolver:
    def __init__(self) -> None:
        self.resolved: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Any:
        self.resolved.append((project_id, agent_id))
        return SimpleNamespace(id=agent_id)


def _fragment_storage() -> SimpleNamespace:
    """Storage stub answering the prompt fragments the briefs are now read from.

    ``/handoff``, ``/learn``, and ``/reflect`` seed their internal run from
    ``handoff.md`` / ``learn.md`` / ``reflect.md`` via ``read_prompt_fragment``;
    the learn brief must mention ``skill_manage`` so the authoring assertion still
    holds, and the reflect brief carries a stable marker phrase.
    """
    fragments = {
        "handoff.md": "Write a handoff for the next agent.",
        "learn.md": (
            "Author a reusable skill via the `skill_manage` tool: "
            "create it, then write support files."
        ),
        "reflect.md": "Review this session and update your memory and skill library.",
    }
    return SimpleNamespace(read_prompt_fragment=lambda name: fragments[name])


def _make_handoff_state(loop: _HandoffLoop, resolver: _FakeResolver) -> SimpleNamespace:
    created_sessions: list[str] = []

    def create_session(agent_id: str, *, session_id: Any = None, project_id: Any = None) -> Any:
        created_sessions.append(f"{agent_id}@{project_id}")
        return SimpleNamespace(id="new-session")

    chat_sessions = SimpleNamespace(create=create_session)

    async def trigger_run(agent_id: str, message: Any, **kwargs: Any) -> _FakeRun:
        loop.calls.append({"agent_id": agent_id, "message": message, **kwargs})
        return _FakeRun()

    runtime = SimpleNamespace(
        agent_resolver=resolver,
        chat_sessions=chat_sessions,
        agents=SimpleNamespace(update=lambda *a, **k: None),
        trigger_service=SimpleNamespace(trigger_run=trigger_run),
        storage=_fragment_storage(),
    )
    state = SimpleNamespace(
        chat_loop=loop,
        streaming_chat_loop=loop,
        runtime=runtime,
        chat_runs=SimpleNamespace(active_run=lambda **k: None),
        event_bus=SimpleNamespace(publish=lambda *a, **k: None),
    )
    state.command_dispatcher = _core_dispatcher(state)
    state._created_sessions = created_sessions  # type: ignore[attr-defined]
    return state


@pytest.mark.asyncio
async def test_handoff_targets_project_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _HandoffLoop()
    resolver = _FakeResolver()
    state = _make_handoff_state(loop, resolver)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(
        state,
        {
            "agent_id": "builder",
            "session_id": "s1",
            "content": "/handoff agent:orchestrator@vbot",
        },
    )

    # The receiving run targets orchestrator under project vbot, and the new
    # session was created under that project anchor.
    assert ("vbot", "orchestrator") in resolver.resolved
    assert loop.calls[-1]["agent_id"] == "orchestrator"
    assert loop.calls[-1]["project_id"] == "vbot"
    assert loop.calls[-1]["reply_surface"] == ReplySurface.webui()
    assert state._created_sessions[-1] == "orchestrator@vbot"


@pytest.mark.asyncio
async def test_handoff_bare_target_stays_in_source_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _HandoffLoop()
    resolver = _FakeResolver()
    state = _make_handoff_state(loop, resolver)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(
        state,
        {"agent_id": "builder@vbot", "session_id": "s1", "content": "/handoff"},
    )

    # No explicit target → receiving run stays in the source (builder, vbot) scope.
    assert loop.calls[-1]["agent_id"] == "builder"
    assert loop.calls[-1]["project_id"] == "vbot"
    assert state._created_sessions[-1] == "builder@vbot"


# ---------------------------------------------------------------------------
# /learn: internal skill-authoring run seeded with the learn brief.
# ---------------------------------------------------------------------------


def _make_learn_state(
    captured: list[dict[str, Any]], *, workspace: str = "/home/agent", active: bool = False
) -> SimpleNamespace:
    async def trigger_run(agent_id: str, message: Any, **kwargs: Any) -> _FakeRun:
        captured.append({"agent_id": agent_id, "message": message, **kwargs})
        return _FakeRun()

    runtime = SimpleNamespace(
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: SimpleNamespace(
                id=agent_id, workspace=workspace
            )
        ),
        trigger_service=SimpleNamespace(trigger_run=trigger_run),
        storage=_fragment_storage(),
    )
    active_run = _FakeRun() if active else None
    state = SimpleNamespace(
        chat_loop=SimpleNamespace(),
        streaming_chat_loop=SimpleNamespace(),
        runtime=runtime,
        chat_runs=SimpleNamespace(active_run=lambda **k: active_run),
        event_bus=SimpleNamespace(publish=lambda *a, **k: None),
    )
    state.command_dispatcher = _core_dispatcher(state)
    return state


@pytest.mark.asyncio
async def test_learn_starts_internal_authoring_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    state = _make_learn_state(captured)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    response = await _send_chat(
        state,
        {"agent_id": "builder", "session_id": "s1", "content": "/learn the deploy steps"},
    )

    assert response["command_handled"] is True
    assert response["reply"] == "handoff text"  # the run's final message content
    assert len(captured) == 1
    assert captured[0]["internal"] is True
    assert captured[0]["reply_surface"] == ReplySurface.webui()
    assert "skill_manage" in captured[0]["message"]
    assert "the deploy steps" in captured[0]["message"]


@pytest.mark.asyncio
async def test_learn_without_argument_still_starts_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    state = _make_learn_state(captured)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "/learn"})

    assert len(captured) == 1
    assert "No request was given" in captured[0]["message"]


@pytest.mark.asyncio
async def test_learn_refused_while_run_active(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    state = _make_learn_state(captured, active=True)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    response = await _send_chat(
        state, {"agent_id": "builder", "session_id": "s1", "content": "/learn x"}
    )

    assert response["command_handled"] is True
    assert "after the current run finishes" in response["reply"]
    assert captured == []


@pytest.mark.asyncio
async def test_learn_refuses_config_agent_without_starting_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    state = _make_learn_state(captured, workspace="")
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    response = await _send_chat(
        state,
        {"agent_id": "builder@vbot", "session_id": "s1", "content": "/learn deploy"},
    )

    assert response["command_handled"] is True
    assert response["reply"] == "Skill authoring needs an identity agent with its own skill home."
    assert captured == []


# ---------------------------------------------------------------------------
# /reflect: fork the session and run a restricted review run in the fork.
# ---------------------------------------------------------------------------


def _make_reflect_state(
    captured: list[dict[str, Any]],
    forked: list[dict[str, Any]],
    *,
    workspace: str = "/home/agent",
    memory_prompt_mode: str = "agent_user",
    active: bool = False,
    titles: list[tuple[str, str]] | None = None,
    metadata_writes: list[tuple[str, dict[str, Any]]] | None = None,
) -> SimpleNamespace:
    """State stub whose runtime carries a REAL ``ReflectionService``.

    The handler delegates fork + review to ``runtime.reflection``, so the test
    wires the genuine service against stubbed sessions/loop/storage — the
    orchestration (fork, fork title, restricted internal run, counter reset)
    is exercised for real while I/O stays captured in the lists.
    """

    async def start_run(agent_id: str, content: Any, **kwargs: Any) -> _FakeRun:
        captured.append({"agent_id": agent_id, "message": content, **kwargs})
        return _FakeRun()

    async def fork(source_agent_id: str, session_id: str, **kwargs: Any) -> Any:
        forked.append({"source_agent_id": source_agent_id, "session_id": session_id, **kwargs})
        return SimpleNamespace(id="fork-1")

    title_log = titles if titles is not None else []
    metadata_log = metadata_writes if metadata_writes is not None else []
    chat_sessions = SimpleNamespace(
        fork=fork,
        get_metadata=lambda agent_id, session_id, project_id=None: {},
        set_metadata=lambda agent_id, session_id, data, project_id=None: metadata_log.append(
            (session_id, data)
        ),
        set_title=lambda agent_id, session_id, title, project_id=None: title_log.append(
            (session_id, title)
        ),
        record_run_kind=lambda agent_id, session_id, run_kind, project_id=None: None,
    )
    runtime = SimpleNamespace(
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: SimpleNamespace(
                id=agent_id,
                workspace=workspace,
                memory_prompt_mode=memory_prompt_mode,
            )
        ),
        chat_sessions=chat_sessions,
        storage=_fragment_storage(),
        streaming_chat_loop=SimpleNamespace(start_run=start_run),
    )
    runtime.reflection = ReflectionService(cast("Any", runtime))
    active_run = _FakeRun() if active else None
    state = SimpleNamespace(
        chat_loop=SimpleNamespace(start_run=start_run),
        streaming_chat_loop=SimpleNamespace(start_run=start_run),
        runtime=runtime,
        chat_runs=SimpleNamespace(active_run=lambda **k: active_run),
        event_bus=SimpleNamespace(publish=lambda *a, **k: None),
    )
    state.command_dispatcher = _core_dispatcher(state)
    return state


@pytest.mark.asyncio
async def test_reflect_forks_and_runs_restricted_review(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    forked: list[dict[str, Any]] = []
    titles: list[tuple[str, str]] = []
    metadata_writes: list[tuple[str, dict[str, Any]]] = []
    state = _make_reflect_state(captured, forked, titles=titles, metadata_writes=metadata_writes)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    response = await _send_chat(
        state,
        {"agent_id": "builder", "session_id": "s1", "content": "/reflect focus on the memory side"},
    )

    # The source session is forked once; the review run targets the NEW fork id.
    assert forked[0]["source_agent_id"] == "builder"
    assert forked[0]["session_id"] == "s1"
    assert forked[0]["strip_meta_keys"] == SESSION_FORK_ALWAYS_STRIP_META_KEYS
    assert len(captured) == 1
    assert captured[0]["session_id"] == "fork-1"
    assert captured[0]["session_id"] != "s1"
    assert captured[0]["internal"] is True
    assert captured[0]["run_kind"] is RunKind.REFLECTION
    assert captured[0]["tool_restriction"] == (
        "memory",
        "skill",
        "skill_list",
        "skill_manage",
    )
    assert "tool_grants" not in captured[0]
    assert captured[0]["reply_surface"] == ReplySurface.webui()
    # The brief carries the fragment marker plus the focus text.
    assert "Review this session" in captured[0]["message"]
    assert "focus on the memory side" in captured[0]["message"]
    # The fork is titled recognizably instead of inheriting the source title.
    assert titles == [("fork-1", "Reflection")]
    # A manual review covers both dimensions, so the cadence counters reset on
    # the SOURCE session.
    assert metadata_writes == [
        (
            "s1",
            {
                REFLECTION_COUNTERS_META_KEY: {
                    "turns_since_memory_review": 0,
                    "model_steps_since_skill_review": 0,
                    COUNTER_GENERATION_KEY: 1,
                }
            },
        )
    ]
    # The reply is the run's final message, and the fork id rides in ``data``.
    assert response["command_handled"] is True
    assert response["reply"] == "handoff text"
    assert response["data"] == {
        "command": "reflect",
        "session_id": "fork-1",
        "agent_id": "builder",
    }


@pytest.mark.asyncio
async def test_reflect_without_focus_uses_bare_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    forked: list[dict[str, Any]] = []
    state = _make_reflect_state(captured, forked)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "/reflect"})

    assert len(captured) == 1
    assert captured[0]["message"] == "Review this session and update your memory and skill library."


@pytest.mark.asyncio
async def test_reflect_refused_while_run_active(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    forked: list[dict[str, Any]] = []
    state = _make_reflect_state(captured, forked, active=True)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    response = await _send_chat(
        state, {"agent_id": "builder", "session_id": "s1", "content": "/reflect"}
    )

    assert "after the current run finishes" in response["reply"]
    # Refused before any fork or run.
    assert forked == []
    assert captured == []


@pytest.mark.asyncio
async def test_reflect_refused_for_config_agent_without_forking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    forked: list[dict[str, Any]] = []
    state = _make_reflect_state(captured, forked, workspace="")
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    response = await _send_chat(
        state, {"agent_id": "builder", "session_id": "s1", "content": "/reflect"}
    )

    assert "identity agent" in response["reply"]
    # An empty-workspace agent never forks and never runs.
    assert forked == []
    assert captured == []


@pytest.mark.asyncio
async def test_reflect_refused_when_memory_tool_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    forked: list[dict[str, Any]] = []
    state = _make_reflect_state(captured, forked, memory_prompt_mode="off")
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    response = await _send_chat(
        state, {"agent_id": "builder", "session_id": "s1", "content": "/reflect"}
    )

    assert "memory Tool" in response["reply"]
    assert forked == []
    assert captured == []


# ---------------------------------------------------------------------------
# /model set: identity vs project routing + usable-model validation.
# ---------------------------------------------------------------------------


class _RecordingAgents:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def update(self, agent_id: str, **changes: Any) -> Any:
        self.updates.append((agent_id, changes))
        return SimpleNamespace(id=agent_id, **changes)


class _RecordingProjects:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str, str, Any]] = []
        self.clear_calls: list[tuple[str, str, str]] = []

    def set_override(self, project_id: str, agent_id: str, field: str, value: Any) -> Any:
        self.set_calls.append((project_id, agent_id, field, value))
        return SimpleNamespace(project_id=project_id)

    def clear_override(self, project_id: str, agent_id: str, field: str) -> Any:
        self.clear_calls.append((project_id, agent_id, field))
        return SimpleNamespace(project_id=project_id)


class _ModelResolver:
    """Model-validation stub: only the configured set is usable."""

    def __init__(self, configured: set[str]) -> None:
        self._configured = configured

    def require_model_configured(self, model: str) -> None:
        if model not in self._configured:
            raise ModelConfigurationError(f"model is not configured: {model}")


def _make_model_state(
    *, configured: set[str], agents: _RecordingAgents, projects: _RecordingProjects, models: Any
) -> SimpleNamespace:
    runtime = SimpleNamespace(
        agent_resolver=_ModelResolver(configured),
        agents=agents,
        projects=projects,
        models=models,
    )
    return SimpleNamespace(runtime=runtime, chat_runs=SimpleNamespace())


@pytest.mark.asyncio
async def test_set_model_identity_updates_agent_model() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured={"openai/gpt-5"}, agents=agents, projects=projects, models=SimpleNamespace()
    )

    result = await _execute_core_command(
        state, "/model openai/gpt-5", agent_id="coder", project_id=None
    )

    # Identity session writes the agent's own model; the project store is untouched.
    assert agents.updates == [("coder", {"model": "openai/gpt-5"})]
    assert projects.set_calls == []
    assert result.facts == {"agent_id": "coder", "model": "openai/gpt-5"}
    assert result.feedback is not None
    assert result.feedback.kind == "notice"


@pytest.mark.asyncio
async def test_set_model_identity_reset_clears_model() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured=set(), agents=agents, projects=projects, models=SimpleNamespace()
    )

    result = await _execute_core_command(state, "/model reset", agent_id="coder", project_id=None)

    # reset writes an empty model (falls to the global default) and skips validation.
    assert agents.updates == [("coder", {"model": ""})]
    assert result.facts["model"] == ""


@pytest.mark.asyncio
async def test_set_model_project_writes_override() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured={"openai/gpt-mini"}, agents=agents, projects=projects, models=SimpleNamespace()
    )

    await _execute_core_command(state, "/model openai/gpt-mini", project_id="vbot")

    # Project session writes a per-agent model override; the identity store is untouched.
    assert projects.set_calls == [("vbot", "builder", "model", "openai/gpt-mini")]
    assert agents.updates == []


# ---------------------------------------------------------------------------
# /rename command -> rename_session action
# ---------------------------------------------------------------------------


class _RecordingTitleSessions:
    def __init__(self) -> None:
        self.renamed: list[tuple[str, str, str, str | None]] = []

    def set_title(
        self, agent_id: str, session_id: str, title: str, project_id: str | None = None
    ) -> str | None:
        self.renamed.append((agent_id, session_id, title, project_id))
        normalized = " ".join(title.split())
        return normalized or None


def _make_rename_state(sessions: _RecordingTitleSessions) -> SimpleNamespace:
    runtime = SimpleNamespace(chat_sessions=sessions)
    return SimpleNamespace(
        runtime=runtime,
        chat_runs=SimpleNamespace(),
        event_bus=ServerEventBus(),
    )


@pytest.mark.asyncio
async def test_rename_command_sets_title_with_toast() -> None:
    sessions = _RecordingTitleSessions()
    state = _make_rename_state(sessions)

    result = await _execute_core_command(
        state, "/rename Release planning", agent_id="coder", project_id=None
    )

    assert sessions.renamed == [("coder", "s1", "Release planning", None)]
    assert result.feedback is not None
    assert result.feedback.kind == "notice"
    assert "Release planning" in result.feedback.text
    assert result.facts == {"session_id": "s1", "title": "Release planning"}


@pytest.mark.asyncio
async def test_rename_command_without_argument_clears() -> None:
    sessions = _RecordingTitleSessions()
    state = _make_rename_state(sessions)

    result = await _execute_core_command(state, "/rename", agent_id="coder", project_id=None)

    # No argument clears: the handler passes "" and reports the cleared name.
    assert sessions.renamed == [("coder", "s1", "", None)]
    assert result.facts["title"] is None
    assert result.feedback is not None
    assert "cleared" in result.feedback.text.lower()


@pytest.mark.asyncio
async def test_rename_command_project_session_scopes_to_project() -> None:
    sessions = _RecordingTitleSessions()
    state = _make_rename_state(sessions)

    await _execute_core_command(state, "/rename Docs", project_id="vbot")

    assert sessions.renamed == [("builder", "s1", "Docs", "vbot")]


@pytest.mark.asyncio
async def test_set_model_project_reset_clears_override() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured=set(), agents=agents, projects=projects, models=SimpleNamespace()
    )

    # The reset token is case-insensitive.
    await _execute_core_command(state, "/model RESET", project_id="vbot")

    assert projects.clear_calls == [("vbot", "builder", "model")]
    assert projects.set_calls == []


@pytest.mark.asyncio
async def test_set_model_rejects_unusable_model() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()
    state = _make_model_state(
        configured={"openai/gpt-5"}, agents=agents, projects=projects, models=SimpleNamespace()
    )

    with pytest.raises(ModelConfigurationError):
        await _execute_core_command(state, "/model openai/ghost", agent_id="coder")

    assert agents.updates == []  # nothing is written when the model is rejected


@pytest.mark.asyncio
async def test_set_model_rejects_forbidden_pinned_connection() -> None:
    agents = _RecordingAgents()
    projects = _RecordingProjects()

    class _Model:
        connections = ["api-key"]

        def allows_connection(self, connection_id: str) -> bool:
            return connection_id in self.connections

    models = SimpleNamespace(get=lambda _provider, _model: _Model())
    state = _make_model_state(
        configured={"openai/gpt-5::subscription"},
        agents=agents,
        projects=projects,
        models=models,
    )

    state.runtime.agent_resolver = _ModelResolver(set())
    with pytest.raises(ModelConfigurationError):
        await _execute_core_command(
            state, "/model openai/gpt-5::subscription", agent_id="coder", project_id=None
        )

    assert agents.updates == []


# ---------------------------------------------------------------------------
# Queue invalidation: RPC send/remove/update publish a scoped queue signal so
# other windows reload the queue live instead of waiting for a terminal event.
# ---------------------------------------------------------------------------


class _QueueOnBusyLoop:
    """``start_run`` reports the session busy; ``queue_run`` returns a queued item.

    ``build_queue_update`` stands in for the streaming loop's queue-update build:
    it returns the *resolved* target session id (which can differ from the raw
    input), letting the update test assert the signal is scoped on the resolved
    id.
    """

    def __init__(
        self,
        resolved_session_id: str = "s1",
        *,
        run_started_during_enqueue: _FakeRun | None = None,
    ) -> None:
        self._resolved_session_id = resolved_session_id
        self._run_started_during_enqueue = run_started_during_enqueue
        self.build_calls: list[dict[str, Any]] = []

    async def start_run(self, agent_id: str, content: Any, **kwargs: Any) -> Any:
        raise ActiveRunError("session already has an active run")

    async def queue_run(self, agent_id: str, content: Any, **kwargs: Any) -> Any:
        future = asyncio.get_running_loop().create_future()
        if self._run_started_during_enqueue is not None:
            future.set_result(self._run_started_during_enqueue)
        return SimpleNamespace(future=future, to_dict=lambda: {"id": "q-1"})

    def build_queue_update(
        self, agent_id: str, session_id: str, content: Any, **kwargs: Any
    ) -> tuple[str, object, str]:
        self.build_calls.append({"agent_id": agent_id, "session_id": session_id, **kwargs})
        return self._resolved_session_id, object(), "display"


class _FakeQueueRuns:
    """Minimal ChatRunManager stand-in for the queue remove/update handlers.

    The queue key carries the project anchor, so the fake records the
    ``project_id`` each call was scoped with (the handlers parse it from the
    agent address).
    """

    def __init__(self, *, editable: bool = True) -> None:
        self.list_project_ids: list[str | None] = []
        self.update_project_ids: list[str | None] = []
        self.editable = editable

    def list_queued(self, agent_id: str, session_id: str, *, project_id: str | None) -> list[Any]:
        self.list_project_ids.append(project_id)
        return [
            SimpleNamespace(
                item_id="q-1",
                internal=False,
                editable=self.editable,
            )
        ]

    def remove_queued(
        self, agent_id: str, session_id: str, item_id: str, *, project_id: str | None
    ) -> bool:
        return True

    def update_queued(self, *args: Any, **kwargs: Any) -> bool:
        self.update_project_ids.append(kwargs.get("project_id"))
        return True


def _make_queue_state(loop: Any) -> SimpleNamespace:
    return SimpleNamespace(
        chat_loop=loop,
        streaming_chat_loop=loop,
        runtime=SimpleNamespace(),
        event_bus=ServerEventBus(),
        chat_runs=_FakeQueueRuns(),
        command_dispatcher=_NoCommandDispatcher(),
    )


def _queue_resource_events(state: SimpleNamespace) -> list[dict[str, Any]]:
    return [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ]


@pytest.mark.asyncio
async def test_send_enqueue_publishes_queue_resource_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_queue_state(_QueueOnBusyLoop())
    monkeypatch.setattr(
        "server.rpc.chat_methods._bridge_queued_item_to_event_bus", lambda *a, **k: None
    )

    result = await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert result["queued"] is True
    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "s1"}}
    ]


@pytest.mark.asyncio
async def test_stream_enqueue_publishes_queue_resource_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_queue_state(_QueueOnBusyLoop())
    monkeypatch.setattr(
        "server.rpc.chat_methods._bridge_queued_item_to_event_bus", lambda *a, **k: None
    )

    result = await _stream_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert result["queued"] is True
    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "s1"}}
    ]


@pytest.mark.asyncio
async def test_send_busy_to_idle_race_returns_started_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_run = _FakeRun("run-race")
    state = _make_queue_state(_QueueOnBusyLoop(run_started_during_enqueue=started_run))
    bridged_runs: list[Any] = []
    monkeypatch.setattr(
        "server.rpc.chat_methods._bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )

    result = await _send_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert result["run_id"] == "run-race"
    assert result["message"]["content"] == "handoff text"
    assert "queued" not in result
    assert bridged_runs == [started_run]
    assert _queue_resource_events(state) == []


@pytest.mark.asyncio
async def test_stream_busy_to_idle_race_returns_started_run_with_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_run = _FakeRun("run-race")
    state = _make_queue_state(_QueueOnBusyLoop(run_started_during_enqueue=started_run))
    bridged_runs: list[Any] = []
    monkeypatch.setattr(
        "server.rpc.chat_methods._bridge_run_to_event_bus",
        lambda _state, run: bridged_runs.append(run),
    )

    result = await _stream_chat(state, {"agent_id": "builder", "session_id": "s1", "content": "hi"})

    assert result["run_id"] == "run-race"
    assert result["sse_url"] == "/api/runs/run-race/events"
    assert "queued" not in result
    assert bridged_runs == [started_run]
    assert _queue_resource_events(state) == []


def test_queue_remove_publishes_queue_resource_changed() -> None:
    state = _make_queue_state(_QueueOnBusyLoop())

    _chat_queue_remove(state, {"agent_id": "builder", "session_id": "s1", "item_id": "q-1"})

    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "s1"}}
    ]


@pytest.mark.asyncio
async def test_queue_update_scopes_on_resolved_session_id() -> None:
    # build_queue_update resolves the target session, which can differ from the
    # raw input; the queue signal must be scoped on the resolved id, not the input.
    loop = _QueueOnBusyLoop(resolved_session_id="resolved-s1")
    state = _make_queue_state(loop)

    await _chat_queue_update(
        state,
        {"agent_id": "builder", "session_id": "s1", "item_id": "q-1", "content": "edit"},
    )

    assert _queue_resource_events(state) == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "resolved-s1"}}
    ]


@pytest.mark.asyncio
async def test_queue_update_rejects_attachment_items() -> None:
    state = _make_queue_state(_QueueOnBusyLoop())
    state.chat_runs = _FakeQueueRuns(editable=False)

    with pytest.raises(RpcError, match="cannot be edited losslessly"):
        await _chat_queue_update(
            state,
            {"agent_id": "builder", "session_id": "s1", "item_id": "q-1", "content": "edit"},
        )


@pytest.mark.asyncio
async def test_queue_update_rebuilds_against_address_project() -> None:
    # The queue key carries the project anchor, so the edit's params name it in the
    # agent address. The rebuild must run against that same anchor; without this, a
    # project session is looked up in the identity anchor and fails.
    loop = _QueueOnBusyLoop()
    state = _make_queue_state(loop)

    await _chat_queue_update(
        state,
        {"agent_id": "builder@vbot", "session_id": "s1", "item_id": "q-1", "content": "edit"},
    )

    assert loop.build_calls[-1]["project_id"] == "vbot"
    assert loop.build_calls[-1]["reply_surface"] == ReplySurface.webui()
    assert state.chat_runs.update_project_ids[-1] == "vbot"
    assert state.chat_runs.list_project_ids[-1] == "vbot"


@pytest.mark.asyncio
async def test_queue_update_identity_item_rebuilds_without_project() -> None:
    # A bare identity address keeps the rebuild and the queue key project-less.
    loop = _QueueOnBusyLoop()
    state = _make_queue_state(loop)

    await _chat_queue_update(
        state,
        {"agent_id": "builder", "session_id": "s1", "item_id": "q-1", "content": "edit"},
    )

    assert loop.build_calls[-1]["project_id"] is None
    assert loop.build_calls[-1]["reply_surface"] == ReplySurface.webui()
    assert state.chat_runs.update_project_ids[-1] is None


# ---------------------------------------------------------------------------
# /agent move: relocate the current session (full history) to another agent.
# ---------------------------------------------------------------------------


class _FakeMovedSession:
    def __init__(self) -> None:
        self.appended: list[ChatMessage] = []
        self.notes: list[str] = []

    def append(self, message: ChatMessage) -> None:
        self.appended.append(message)

    def add_note(self, content: str) -> None:
        self.notes.append(content)


class _FakeWriteLock:
    async def __aenter__(self) -> _FakeWriteLock:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeMoveSessions:
    """Records the move call and serves the relocated session's two writers."""

    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self._metadata = metadata or {}
        self.move_calls: list[dict[str, Any]] = []
        self.destination = _FakeMovedSession()
        self.move_started: asyncio.Event | None = None
        self.move_release: asyncio.Event | None = None

    async def move(
        self,
        source_agent_id: str,
        session_id: str,
        target_agent_id: str,
        *,
        source_project_id: str | None = None,
        target_project_id: str | None = None,
        strip_meta_keys: Any = frozenset(),
    ) -> _FakeMovedSession:
        self.move_calls.append(
            {
                "source_agent_id": source_agent_id,
                "session_id": session_id,
                "target_agent_id": target_agent_id,
                "source_project_id": source_project_id,
                "target_project_id": target_project_id,
                "strip_meta_keys": set(strip_meta_keys),
            }
        )
        if self.move_started is not None:
            self.move_started.set()
        if self.move_release is not None:
            await self.move_release.wait()
        return self.destination

    def get_metadata(self, agent_id: str, session_id: str, project_id: str | None = None) -> dict:
        return dict(self._metadata)

    def write_lock(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> _FakeWriteLock:
        return _FakeWriteLock()

    def get(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> _FakeMovedSession:
        return self.destination


class _FakeMoveAgents:
    def __init__(self) -> None:
        self.reset_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def reset_current_after_session_removed(self, agent_id: str, removed_session_id: str) -> None:
        self.reset_calls.append((agent_id, removed_session_id))

    def update(self, agent_id: str, **changes: Any) -> None:
        self.update_calls.append((agent_id, changes))


class _FakeMoveRuns:
    def __init__(
        self,
        active: Any = None,
        queued: list[Any] | None = None,
        *,
        guard_blocked: bool = False,
    ) -> None:
        self._active = active
        self._queued = queued or []
        self._guard_blocked = guard_blocked
        self.guarded_sessions: list[tuple[tuple[str | None, str, str], ...]] = []

    def active_run(self, *, agent_id: str, session_id: str, project_id: str | None) -> Any:
        return self._active

    def list_queued(self, agent_id: str, session_id: str, *, project_id: str | None) -> list[Any]:
        return list(self._queued)

    def session_admission_guard(self, *session_keys: tuple[str | None, str, str]) -> Any:
        self.guarded_sessions.append(session_keys)
        if self._guard_blocked:
            return _FakeBlockedAdmissionGuard()
        return _FakeWriteLock()


class _FakeBlockedAdmissionGuard:
    async def __aenter__(self) -> None:
        raise RunAdmissionBlockedError("guarded")

    async def __aexit__(self, *args: Any) -> None:
        return None


class _ConfigurableResolver:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.resolved: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Any:
        self.resolved.append((project_id, agent_id))
        if self._error is not None:
            raise self._error
        return SimpleNamespace(id=agent_id)


def _make_move_state(
    *,
    metadata: dict[str, Any] | None = None,
    active: Any = None,
    queued: list[Any] | None = None,
    resolver_error: Exception | None = None,
    guard_blocked: bool = False,
) -> SimpleNamespace:
    sessions = _FakeMoveSessions(metadata)
    agents = _FakeMoveAgents()
    task_loop = _RecordingLoop()  # project-target task run path (chat_loop.start_run)
    trigger_calls: list[dict[str, Any]] = []
    terminal_transfers: list[tuple[TerminalOwner, TerminalOwner]] = []

    async def trigger_run(agent_id: str, message: Any, **kwargs: Any) -> _FakeRun:
        trigger_calls.append({"agent_id": agent_id, "message": message, **kwargs})
        return _FakeRun()

    runtime = SimpleNamespace(
        chat_sessions=sessions,
        agents=agents,
        agent_resolver=_ConfigurableResolver(resolver_error),
        trigger_service=SimpleNamespace(trigger_run=trigger_run),
        terminal_manager=SimpleNamespace(
            transfer_scope=lambda source, target: terminal_transfers.append((source, target))
        ),
    )
    state = SimpleNamespace(
        chat_loop=task_loop,
        streaming_chat_loop=task_loop,
        runtime=runtime,
        chat_runs=_FakeMoveRuns(active, queued, guard_blocked=guard_blocked),
        event_bus=ServerEventBus(),
        command_dispatcher=_NoCommandDispatcher(),
    )
    state._sessions = sessions  # type: ignore[attr-defined]
    state._agents = agents  # type: ignore[attr-defined]
    state._trigger_calls = trigger_calls  # type: ignore[attr-defined]
    state._task_loop = task_loop  # type: ignore[attr-defined]
    state._terminal_transfers = terminal_transfers  # type: ignore[attr-defined]
    state._command_changes = []  # type: ignore[attr-defined]
    return state


@pytest.mark.parametrize(
    ("source_project", "target_address", "target_agent", "target_project", "reset", "update"),
    [
        (None, "planner", "planner", None, True, True),  # identity -> identity
        (None, "planner@vbot", "planner", "vbot", True, False),  # identity -> project
        ("vbot", "assistant", "assistant", None, False, True),  # project -> identity
        ("vbot", "planner@acme", "planner", "acme", False, False),  # project -> project
    ],
)
@pytest.mark.asyncio
async def test_move_directions_relocate_and_re_home_pointers(
    source_project: str | None,
    target_address: str,
    target_agent: str,
    target_project: str | None,
    reset: bool,
    update: bool,
) -> None:
    state = _make_move_state()

    result = await _execute_core_command(
        state, f"/agent {target_address}", project_id=source_project
    )

    move_call = state._sessions.move_calls[0]
    assert move_call["source_agent_id"] == "builder"
    assert move_call["source_project_id"] == source_project
    assert move_call["target_agent_id"] == target_agent
    assert move_call["target_project_id"] == target_project
    # A move is always cross-agent, so the source Agent's pinned Skill catalog and
    # seen-Skills set are stripped; the target re-pins its own catalog.
    assert move_call["strip_meta_keys"] == set(SESSION_MOVE_STRIP_META_KEYS)
    assert {"pinned_skill_catalog", "seen_skills"} <= move_call["strip_meta_keys"]

    # The "current" pointer follows the session on each identity side only.
    assert (state._agents.reset_calls == [("builder", "s1")]) is reset
    if update:
        assert state._agents.update_calls == [(target_agent, {"current_session_id": "s1"})]
    else:
        assert state._agents.update_calls == []

    # The relocation is announced like session.create/delete: a sessions signal
    # for each side's list, plus one agents signal when an identity current
    # pointer was re-aimed on either side.
    resource_events = [
        {"kind": change.kind, **({"scope": dict(change.scope)} if change.scope else {})}
        for change in state._command_changes
    ]
    assert {"kind": "sessions", "scope": {"agent_id": "builder"}} in resource_events
    assert {"kind": "sessions", "scope": {"agent_id": target_agent}} in resource_events
    agents_events = [event for event in resource_events if event["kind"] == "agents"]
    assert (agents_events == [{"kind": "agents"}]) is (reset or update)

    # A visible takeover divider and the silent note are persisted at the destination.
    assert len(state._sessions.destination.appended) == 1
    divider = state._sessions.destination.appended[0]
    assert divider.role == "agent_takeover"
    assert json.loads(divider.content)["to"] == target_address
    assert state._sessions.destination.notes  # silent takeover note added
    assert state._terminal_transfers == [
        (
            TerminalOwner(source_project, "builder", "s1"),
            TerminalOwner(target_project, target_agent, "s1"),
        )
    ]

    # No task → the target waits; payload lands the accessor on the same session.
    assert state._trigger_calls == []
    assert state._task_loop.start_calls == []
    assert result.navigation is not None
    assert result.navigation.kind == "offer_session"
    assert result.facts == {"session_id": "s1", "agent_id": target_address}


@pytest.mark.asyncio
async def test_move_to_same_pair_is_a_no_op_hint() -> None:
    state = _make_move_state()

    result = await _execute_core_command(state, "/agent builder", project_id=None)

    assert result.feedback is not None
    assert "already belongs" in result.feedback.text
    assert state._sessions.move_calls == []
    # A refused move announces nothing — the signals fire only after relocation.
    assert state._command_changes == []


@pytest.mark.asyncio
async def test_move_refused_while_run_active() -> None:
    state = _make_move_state(active=_FakeRun())

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert "current run" in result.feedback.text
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_while_run_queued() -> None:
    state = _make_move_state(queued=[SimpleNamespace(item_id="q-1")])

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert "queued run" in result.feedback.text
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_when_admission_guard_wins_after_idle_check() -> None:
    state = _make_move_state(guard_blocked=True)

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert "source and destination are idle" in result.feedback.text
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_guard_rejects_source_and_destination_runs_during_storage_wait() -> None:
    state = _make_move_state()
    state.chat_runs = ChatRunManager()
    state._sessions.move_started = asyncio.Event()
    state._sessions.move_release = asyncio.Event()

    move_task = asyncio.create_task(
        _execute_core_command(state, "/agent planner@vbot", project_id=None)
    )
    await asyncio.wait_for(state._sessions.move_started.wait(), timeout=1)

    with pytest.raises(RunAdmissionBlockedError):
        await state.chat_runs.start(
            agent_id="builder",
            session_id="s1",
            executor=lambda _run: asyncio.sleep(0),
            project_id=None,
        )
    with pytest.raises(RunAdmissionBlockedError):
        await state.chat_runs.start(
            agent_id="planner",
            session_id="s1",
            executor=lambda _run: asyncio.sleep(0),
            project_id="vbot",
        )

    state._sessions.move_release.set()
    result = await asyncio.wait_for(move_task, timeout=1)
    assert result.facts == {"session_id": "s1", "agent_id": "planner@vbot"}


@pytest.mark.asyncio
async def test_move_refused_for_unknown_target() -> None:
    state = _make_move_state(resolver_error=AgentResolutionError("no such agent"))

    result = await _execute_core_command(state, "/agent ghost@vbot", project_id=None)

    assert result.feedback is not None
    assert "unknown agent" in result.feedback.text
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_refused_for_invalid_address() -> None:
    state = _make_move_state()

    result = await _execute_core_command(state, "/agent agent:planner", project_id=None)

    assert result.feedback is not None
    assert "invalid agent address" in result.feedback.text
    assert state._sessions.move_calls == []


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_channel_id": "telegram-1"},
        {"is_subagent_session": True},
        {"subagent_parent": "parent-run-1"},
    ],
)
@pytest.mark.asyncio
async def test_move_refused_for_excluded_sessions(metadata: dict[str, Any]) -> None:
    state = _make_move_state(metadata=metadata)

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert "cannot be moved" in result.feedback.text
    assert state._sessions.move_calls == []


@pytest.mark.asyncio
async def test_move_with_task_auto_runs_identity_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_move_state()
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    result = await _execute_core_command(state, "/agent planner do the thing", project_id=None)

    # The task rides as the receiving agent's first visible turn (identity → trigger).
    assert state._trigger_calls == [
        {
            "agent_id": "planner",
            "message": "do the thing",
            "session_id": "s1",
            "project_id": None,
            "internal": False,
            "reply_surface": ReplySurface.webui(),
        }
    ]
    assert result.feedback is not None
    assert "running your task" in result.feedback.text


@pytest.mark.asyncio
async def test_move_with_task_auto_runs_project_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_move_state()
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    await _execute_core_command(state, "/agent planner@vbot ship it", project_id=None)

    # Core uses the same trigger seam for identity and project targets.
    call = state._trigger_calls[-1]
    assert call["agent_id"] == "planner"
    assert call["message"] == "ship it"
    assert call["project_id"] == "vbot"
    assert call["internal"] is False
    assert call["reply_surface"] == ReplySurface.webui()
    assert state._task_loop.start_calls == []


@pytest.mark.asyncio
async def test_move_without_task_waits() -> None:
    state = _make_move_state()

    result = await _execute_core_command(state, "/agent planner", project_id=None)

    assert result.feedback is not None
    assert "waiting" in result.feedback.text
    assert state._trigger_calls == []
    assert state._task_loop.start_calls == []


@pytest.mark.asyncio
async def test_move_divider_and_note_carry_both_addresses() -> None:
    state = _make_move_state()

    await _execute_core_command(state, "/agent planner@vbot", project_id="acme")

    divider = state._sessions.destination.appended[0]
    assert json.loads(divider.content) == {
        "from": format_agent_address("builder", "acme"),
        "to": format_agent_address("planner", "vbot"),
    }
    # The silent note names the source so the receiver knows who it took over from.
    assert "builder@acme" in state._sessions.destination.notes[0]
