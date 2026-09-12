"""Tests for chat methods authoring."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.automation.reflection import (
    COUNTER_GENERATION_KEY,
    REFLECTION_COUNTERS_META_KEY,
    ReflectionService,
)
from core.chat import (
    ReplySurface,
)
from core.runs import RunKind
from core.sessions import (
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    SessionAddress,
)
from server.rpc.chat_methods import (
    _send_chat,
)
from tests.server.rpc.chat_methods_test_support import (
    _core_dispatcher,
    _FakeRun,
)


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
    assert captured[0]["message"].strip()


@pytest.mark.asyncio
async def test_learn_refused_while_run_active(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    state = _make_learn_state(captured, active=True)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    response = await _send_chat(
        state, {"agent_id": "builder", "session_id": "s1", "content": "/learn x"}
    )

    assert response["command_handled"] is True
    assert response["reply"].strip()
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
    assert response["reply"].strip()
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

    async def fork(source: SessionAddress, **kwargs: Any) -> Any:
        forked.append(
            {"source_agent_id": source.agent_id, "session_id": source.session_id, **kwargs}
        )
        return SimpleNamespace(id="fork-1")

    title_log = titles if titles is not None else []
    metadata_log = metadata_writes if metadata_writes is not None else []

    def mutate_metadata(address: SessionAddress, mutation: Any) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        mutation(metadata)
        metadata_log.append((address.session_id, metadata))
        return metadata

    chat_sessions = SimpleNamespace(
        fork=fork,
        get_metadata=lambda address: {},
        set_metadata=lambda address, data: metadata_log.append((address.session_id, data)),
        mutate_metadata=mutate_metadata,
        set_title=lambda address, title: title_log.append((address.session_id, title)),
        record_run_kind=lambda address, run_kind: None,
    )
    runtime = SimpleNamespace(
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: SimpleNamespace(
                id=agent_id,
                name="Builder",
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
        "skill_manage",
    )
    assert "tool_grants" not in captured[0]
    assert captured[0]["reply_surface"] == ReplySurface.webui()
    # The caller-provided focus survives prompt construction unchanged.
    assert "focus on the memory side" in captured[0]["message"]
    # The fork is titled with the agent's display name instead of inheriting
    # the source title.
    assert titles == [("fork-1", "Builder")]
    # A manual review covers both dimensions, so the cadence counters reset on
    # the SOURCE session.
    assert metadata_writes == [
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
    assert captured[0]["message"].strip()


@pytest.mark.asyncio
async def test_reflect_refused_while_run_active(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    forked: list[dict[str, Any]] = []
    state = _make_reflect_state(captured, forked, active=True)
    monkeypatch.setattr("server.rpc.chat_methods._bridge_run_to_event_bus", lambda *a, **k: None)

    response = await _send_chat(
        state, {"agent_id": "builder", "session_id": "s1", "content": "/reflect"}
    )

    assert response["reply"].strip()
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

    assert response["reply"].strip()
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

    assert response["reply"].strip()
    assert forked == []
    assert captured == []
