"""Tests for the background reflection service (cadence + fork review orchestration)."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.automation.reflection import (
    COUNTER_GENERATION_KEY,
    MEMORY_REFLECTION_TOOL_RESTRICTION,
    REFLECTION_COUNTERS_META_KEY,
    REFLECTION_TOOL_RESTRICTION,
    SKILL_REFLECTION_TOOL_RESTRICTION,
    ReflectionService,
    ReflectionUnavailableError,
)
from core.chat import ChatMessage
from core.prompts.briefs import learn_brief, reflection_brief
from core.runs import RunKind
from core.sessions import ChatSessionManager, SessionAddress
from core.storage import StorageManager
from core.tools.availability import ToolAccess
from core.tools.memory import MEMORY_TOOL_PARAMETERS
from core.tools.skill import SKILL_TOOL_PARAMETERS
from core.tools.skill_manage import SKILL_MANAGE_TOOL_PARAMETERS
from tests.core.sessions.history_fixtures import admit_run

# Registered Tools, with the activation the Tools domain declares for them.
_TOOLS = (
    SimpleNamespace(name="read"),
    SimpleNamespace(name="memory", activation="memory_mode", constraints=("identity_agent",)),
    SimpleNamespace(name="skill"),
    SimpleNamespace(name="skill_manage", constraints=("identity_agent",)),
)


class _FakeRun:
    """Minimal stand-in for a chat Run: identity fields plus a final message."""

    def __init__(
        self,
        *,
        agent_id: str = "main",
        session_id: str = "s1",
        project_id: str | None = None,
        iteration_count: int = 0,
        cancel_reason: str | None = None,
        tool_call_names: set[str] | None = None,
        final_content: str = "Saved a memory about the user.",
    ) -> None:
        self.id = "run_test"
        self.agent_id = agent_id
        self.session_id = session_id
        self.project_id = project_id
        self.iteration_count = iteration_count
        self.cancel_reason = cancel_reason
        self.tool_call_names = set(tool_call_names or ())
        self._final_content = final_content
        self.wait_gate: asyncio.Event | None = None

    async def wait(self) -> Any:
        if self.wait_gate is not None:
            await self.wait_gate.wait()
        return SimpleNamespace(content=self._final_content)


class _FakeSessions:
    """In-memory session manager stub: metadata sidecars, titles, forks."""

    def __init__(self) -> None:
        self.metadata: dict[str, dict[str, Any]] = {}
        self.titles: list[tuple[str, str]] = []
        self.forks: list[dict[str, Any]] = []
        self.fork_counter = 0
        self.mutation_threads: list[int] = []

    def get_metadata(self, address: SessionAddress) -> dict[str, Any]:
        return dict(self.metadata.get(address.session_id, {}))

    async def metadata_value_async(self, address: SessionAddress, key: str) -> Any:
        return self.get_metadata(address).get(key)

    def set_metadata(self, address: SessionAddress, data: dict[str, Any]) -> None:
        self.metadata[address.session_id] = dict(data)

    def mutate_metadata(self, address: SessionAddress, mutation: Any) -> dict[str, Any]:
        self.mutation_threads.append(threading.get_ident())
        metadata = self.get_metadata(address)
        mutation(metadata)
        self.set_metadata(address, metadata)
        return metadata

    async def fork(
        self,
        source: SessionAddress,
        *,
        title: str | None = None,
        run_kind: RunKind | None = None,
        **kwargs: Any,
    ) -> Any:
        # Title and run kind commit with the fork; there is no follow-up write.
        self.fork_counter += 1
        fork_id = f"fork-{self.fork_counter}"
        self.forks.append(
            {
                "source_agent_id": source.agent_id,
                "session_id": source.session_id,
                **kwargs,
            }
        )
        if title is not None:
            self.titles.append((fork_id, title))
        if run_kind is not None:
            self.metadata.setdefault(fork_id, {})["run_kinds"] = [run_kind.value]
        return SimpleNamespace(id=fork_id)


class _FakeLoop:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.final_content = "Saved a memory about the user."
        self.raise_on_start: Exception | None = None
        self.wait_gate: asyncio.Event | None = None
        self.run_started = asyncio.Event()

    async def start_run(self, agent_id: str, content: str, **kwargs: Any) -> _FakeRun:
        if self.raise_on_start is not None:
            raise self.raise_on_start
        self.started.append({"agent_id": agent_id, "message": content, **kwargs})
        run = _FakeRun(final_content=self.final_content)
        run.wait_gate = self.wait_gate
        self.run_started.set()
        return run


def _make_service(
    *,
    enabled: bool = True,
    memory_turn_interval: int = 3,
    skill_model_step_interval: int = 10,
    chat_sessions: ChatSessionManager | None = None,
    agent: Any = None,
    librarian_running: set[str] | None = None,
) -> tuple[ReflectionService, _FakeSessions, _FakeLoop]:
    """Build the service over the fake Sessions, or over real ``chat_sessions``.

    ``agent`` is the effective Agent a review resolves when it starts, and
    ``librarian_running`` names the Agents a Librarian pass curates. Every
    prompt fragment reads as its bracketed name.
    """
    sessions = _FakeSessions()
    loop = _FakeLoop()
    review_agent = _identity_agent() if agent is None else agent

    async def resolve_agent_async(_project_id: str | None, _agent_id: str) -> Any:
        return review_agent

    runtime = SimpleNamespace(
        storage=SimpleNamespace(
            load_reflection_settings=lambda: {
                "enabled": enabled,
                "memory_turn_interval": memory_turn_interval,
                "skill_model_step_interval": skill_model_step_interval,
            },
            read_prompt_fragment=lambda name: f"[{name}]",
        ),
        agent_resolver=SimpleNamespace(resolve_agent_async=resolve_agent_async),
        chat_sessions=sessions if chat_sessions is None else chat_sessions,
        streaming_chat_loop=loop,
        tools=SimpleNamespace(list_tools=lambda: list(_TOOLS)),
    )
    curated = librarian_running if librarian_running is not None else set()
    service = ReflectionService(cast("Any", runtime), librarian_running=curated.__contains__)
    return service, sessions, loop


def _counters(sessions: _FakeSessions, session_id: str = "s1") -> dict[str, int]:
    raw = cast("dict[str, int]", sessions.metadata[session_id][REFLECTION_COUNTERS_META_KEY])
    return {
        "turns_since_memory_review": raw["turns_since_memory_review"],
        "iterations_since_skill_review": raw["iterations_since_skill_review"],
    }


def _counter_generation(sessions: _FakeSessions, session_id: str = "s1") -> int:
    raw = cast("dict[str, int]", sessions.metadata[session_id][REFLECTION_COUNTERS_META_KEY])
    return raw[COUNTER_GENERATION_KEY]


def _identity_agent(
    *, memory_prompt_mode: str = "agent_user", tool_access: ToolAccess | None = None
) -> Any:
    return SimpleNamespace(
        id="main",
        name="Main Agent",
        workspace="/data/workspace-main",
        memory_prompt_mode=memory_prompt_mode,
        tool_access=tool_access or ToolAccess(),
    )


def _without(*, memory: bool = False, skill_manage: bool = False, skill: bool = False) -> Any:
    """An identity Agent that cannot call the named learning Tools."""
    denied = tuple(
        name for name, missing in (("skill", skill), ("skill_manage", skill_manage)) if missing
    )
    return _identity_agent(
        memory_prompt_mode="off" if memory else "agent_user",
        tool_access=ToolAccess(denied=denied),
    )


async def _drain(service: ReflectionService) -> None:
    while service._background_tasks:
        await asyncio.gather(*list(service._background_tasks))


async def _account_during_review(
    service: ReflectionService, run: _FakeRun, *, outcome: str = "success"
) -> None:
    """Account one Run end while an earlier review of the same Agent still runs."""
    running = set(service._background_tasks)
    service.notify_run_end(cast("Any", run), _identity_agent(), internal=False, outcome=outcome)
    await asyncio.gather(*(service._background_tasks - running))


@pytest.mark.asyncio
async def test_aclose_cancels_and_drains_background_reflection_tasks() -> None:
    service, _sessions, _loop = _make_service()
    started = asyncio.Event()

    async def blocking_review() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(blocking_review())
    service._background_tasks.add(task)
    service._agents_in_review.add("main")
    await started.wait()

    await service.aclose()

    assert task.cancelled()
    assert service._background_tasks == set()
    assert service._agents_in_review == set()
    service.notify_run_end(
        cast("Any", _FakeRun()),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    assert service._background_tasks == set()


@pytest.mark.asyncio
async def test_run_end_accounting_writes_metadata_off_the_event_loop() -> None:
    service, sessions, _loop = _make_service()

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await _drain(service)

    # The Session write may wait for a busy writer, so it must not block the loop.
    assert _counters(sessions)["turns_since_memory_review"] == 1
    assert sessions.mutation_threads
    assert threading.get_ident() not in sessions.mutation_threads


# --- notify_run_end inline gates ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_options", "run", "agent", "internal", "outcome"),
    [
        pytest.param({}, {}, _identity_agent(), True, "success", id="internal-run"),
        pytest.param({}, {"iteration_count": 3}, _identity_agent(), False, "error", id="failed"),
        pytest.param(
            {},
            {"cancel_reason": "shutdown", "iteration_count": 3},
            _identity_agent(),
            False,
            "cancelled",
            id="shutdown-cancel",
        ),
        pytest.param(
            {},
            {"cancel_reason": "user", "iteration_count": 0},
            _identity_agent(),
            False,
            "cancelled",
            id="user-cancel-before-any-model-step",
        ),
        pytest.param(
            {},
            {},
            SimpleNamespace(id="builder", workspace=""),
            False,
            "success",
            id="config-agent-without-workspace",
        ),
        pytest.param(
            {"memory_turn_interval": 1, "skill_model_step_interval": 1},
            {"iteration_count": 20, "tool_call_names": {"memory", "skill_manage"}},
            _without(memory=True, skill_manage=True),
            False,
            "success",
            id="agent-without-learning-tools",
        ),
        pytest.param(
            {"enabled": False}, {}, _identity_agent(), False, "success", id="reflection-disabled"
        ),
    ],
)
async def test_run_ends_that_do_not_count_write_nothing(
    service_options: dict[str, Any],
    run: dict[str, Any],
    agent: Any,
    internal: bool,
    outcome: str,
) -> None:
    service, sessions, loop = _make_service(**service_options)

    service.notify_run_end(cast("Any", _FakeRun(**run)), agent, internal=internal, outcome=outcome)
    await _drain(service)

    assert sessions.metadata == {}
    assert loop.started == []


# --- cadence accounting -------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_sessions_are_excluded() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1)
    sessions.metadata["s1"] = {"is_subagent_session": True}

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await _drain(service)

    assert sessions.metadata["s1"] == {"is_subagent_session": True}
    assert loop.started == []


_REVIEWS: dict[str, tuple[tuple[str, ...], RunKind]] = {
    "memory": (MEMORY_REFLECTION_TOOL_RESTRICTION, RunKind.MEMORY_REFLECTION),
    "skill": (SKILL_REFLECTION_TOOL_RESTRICTION, RunKind.SKILL_REFLECTION),
    "combined": (REFLECTION_TOOL_RESTRICTION, RunKind.REFLECTION),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intervals", "counters", "run", "outcome", "expected", "brief"),
    [
        pytest.param(
            (3, 10), None, {"iteration_count": 4}, "success", (1, 4), None, id="below-thresholds"
        ),
        # Turns reset (memory reviewed); Iterations keep accumulating (skill not due).
        pytest.param(
            (2, 100),
            (1, 5),
            {"iteration_count": 2},
            "success",
            (0, 7),
            "memory",
            id="memory-due",
        ),
        pytest.param(
            (100, 5),
            None,
            {"iteration_count": 6},
            "success",
            (1, 0),
            "skill",
            id="skill-due",
        ),
        pytest.param(
            (1, 1), None, {"iteration_count": 3}, "success", (0, 0), "combined", id="both-due"
        ),
        pytest.param(
            (2, 5),
            (1, 3),
            {"cancel_reason": "user", "iteration_count": 2},
            "cancelled",
            (0, 0),
            "combined",
            id="user-cancel-after-a-model-step-counts",
        ),
        # A memory or skill_manage call resets its own dimension without counting
        # the Run for it, even when the Run later fails.
        pytest.param(
            (3, 100),
            (2, 4),
            {"iteration_count": 3, "tool_call_names": {"memory", "read"}},
            "success",
            (0, 7),
            None,
            id="memory-call-resets-memory",
        ),
        pytest.param(
            (3, 10),
            (2, 4),
            {"iteration_count": 3, "tool_call_names": {"memory"}},
            "error",
            (0, 4),
            None,
            id="memory-call-in-failed-run-resets-memory",
        ),
        pytest.param(
            (1, 3),
            None,
            {"iteration_count": 3, "tool_call_names": {"memory"}},
            "success",
            (0, 0),
            "skill",
            id="memory-call-leaves-only-skill-due",
        ),
        pytest.param(
            (100, 10),
            (2, 9),
            {"iteration_count": 3, "tool_call_names": {"skill_manage", "read"}},
            "success",
            (3, 0),
            None,
            id="skill-call-resets-skill",
        ),
        pytest.param(
            (3, 10),
            (2, 9),
            {"iteration_count": 3, "tool_call_names": {"skill_manage"}},
            "error",
            (2, 0),
            None,
            id="skill-call-in-failed-run-resets-skill",
        ),
        pytest.param(
            (1, 100),
            (0, 90),
            {"iteration_count": 3, "tool_call_names": {"skill_manage"}},
            "success",
            (0, 0),
            "memory",
            id="skill-call-leaves-only-memory-due",
        ),
    ],
)
async def test_run_end_accounting_follows_the_review_cadence(
    intervals: tuple[int, int],
    counters: tuple[int, int] | None,
    run: dict[str, Any],
    outcome: str,
    expected: tuple[int, int],
    brief: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, sessions, loop = _make_service(
        memory_turn_interval=intervals[0], skill_model_step_interval=intervals[1]
    )
    caplog.set_level(logging.DEBUG, logger="vbot.automation.reflection")
    loop.final_content = "Saved: the user's partner is called model-output-sentinel."
    if counters is not None:
        sessions.metadata["s1"] = {
            REFLECTION_COUNTERS_META_KEY: {
                "turns_since_memory_review": counters[0],
                "iterations_since_skill_review": counters[1],
            }
        }

    service.notify_run_end(
        cast("Any", _FakeRun(**run)), _identity_agent(), internal=False, outcome=outcome
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": expected[0],
        "iterations_since_skill_review": expected[1],
    }
    if brief is None:
        assert loop.started == []
        return
    [review] = loop.started
    tool_restriction, run_kind = _REVIEWS[brief]
    assert review["message"] == reflection_brief(service._runtime.storage, cast("Any", brief))
    assert review["tool_restriction"] == tool_restriction
    assert review["run_kind"] is run_kind
    # The review is an internal Run in a fork of the reviewed Session.
    assert review["internal"] is True
    assert review["session_id"] == "fork-1"
    assert "tool_grants" not in review
    # The review's closing summary is Model output, which never enters the log.
    assert "model-output-sentinel" not in caplog.text
    assert [record.levelno for record in caplog.records].count(logging.INFO) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "intervals", "counters", "run", "expected", "scope"),
    [
        # Without Memory the turns counter is frozen: neither counted nor reset.
        pytest.param(
            _without(memory=True),
            (1, 3),
            (5, 1),
            {"iteration_count": 3, "tool_call_names": {"memory"}},
            (5, 0),
            "skill",
            id="memory-unavailable",
        ),
        pytest.param(
            _without(skill_manage=True),
            (2, 1),
            (1, 5),
            {"iteration_count": 3, "tool_call_names": {"skill_manage"}},
            (0, 5),
            "memory",
            id="skill-manage-unavailable",
        ),
        pytest.param(
            _without(skill=True),
            (100, 1),
            (1, 5),
            {"iteration_count": 3},
            (2, 5),
            None,
            id="skill-unavailable",
        ),
        # The built-in Librarian can call skill and skill_manage but is never reviewed.
        pytest.param(
            SimpleNamespace(
                **{
                    **vars(_without(memory=True)),
                    "id": "librarian",
                    "builtin": "librarian",
                    "tool_access": ToolAccess(mode="selected", allowed=("skill", "skill_manage")),
                }
            ),
            (1, 1),
            (1, 5),
            {"iteration_count": 3, "tool_call_names": {"skill_manage"}},
            (1, 5),
            None,
            id="librarian",
        ),
    ],
)
async def test_unavailable_dimension_is_neither_counted_nor_reviewed(
    agent: Any,
    intervals: tuple[int, int],
    counters: tuple[int, int],
    run: dict[str, Any],
    expected: tuple[int, int],
    scope: str | None,
) -> None:
    service, sessions, loop = _make_service(
        memory_turn_interval=intervals[0], skill_model_step_interval=intervals[1], agent=agent
    )
    sessions.metadata["s1"] = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": counters[0],
            "iterations_since_skill_review": counters[1],
        }
    }

    service.notify_run_end(cast("Any", _FakeRun(**run)), agent, internal=False, outcome="success")
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": expected[0],
        "iterations_since_skill_review": expected[1],
    }
    assert [review["tool_restriction"] for review in loop.started] == (
        [] if scope is None else [_REVIEWS[scope][0]]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("review_agent", "expected", "scope"),
    [
        pytest.param(_without(skill_manage=True), (0, 3), "memory", id="narrowed-to-memory"),
        pytest.param(_without(memory=True, skill=True), (1, 3), None, id="nothing-callable"),
    ],
)
async def test_background_review_follows_tool_access_when_it_starts(
    review_agent: Any,
    expected: tuple[int, int],
    scope: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Both dimensions are due at the Run end, but Tool access changed before
    # the review resolved its Agent.
    service, sessions, loop = _make_service(
        memory_turn_interval=1, skill_model_step_interval=1, agent=review_agent
    )

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=3)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    # Only the dimensions actually reviewed consume their counts.
    assert _counters(sessions) == {
        "turns_since_memory_review": expected[0],
        "iterations_since_skill_review": expected[1],
    }
    assert [review["tool_restriction"] for review in loop.started] == (
        [] if scope is None else [_REVIEWS[scope][0]]
    )
    assert "main" not in service._agents_in_review
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.asyncio
@pytest.mark.usefixtures("current_format_data_directory")
async def test_review_fork_leaves_source_bindings_and_run_kinds_behind(tmp_path: Path) -> None:
    sessions = ChatSessionManager(tmp_path)
    try:
        service, _fake, loop = _make_service(chat_sessions=sessions)
        source = sessions.create("main", session_id="s1")
        source.append(ChatMessage.user("Plan the parser refactor"))
        bindings = {
            "source_channel_id": "telegram-main",
            "platform": "telegram",
            "platform_conv_id": "chat-1",
            "last_reply_target": {"chat_id": "chat-1"},
            "is_subagent_session": True,
            "subagent_parent": {
                "id": "sub_work",
                "agent_id": "lead",
                "session_id": "lead-session",
                "run_id": "lead-run",
                "tool_call_id": "call-1",
                "tool_call_index": 0,
                "project_id": None,
            },
            REFLECTION_COUNTERS_META_KEY: {
                "turns_since_memory_review": 3,
                "iterations_since_skill_review": 4,
                COUNTER_GENERATION_KEY: 1,
            },
        }
        sessions.set_metadata(source.address, {"title": "Refactor plan", **bindings})
        await admit_run(sessions, source.address, RunKind.CHANNEL)

        result = await service.run_review("main", "s1", review_scope="memory")

        fork = SessionAddress(project_id=None, agent_id="main", session_id=result.session_id)
        metadata = sessions.get_metadata(fork)
        # The fork keeps the reviewed history but none of the source's bindings.
        assert not bindings.keys() & metadata.keys()
        assert metadata["title"] == "Main Agent: Refactor plan"
        assert metadata["run_kinds"] == ["memory_reflection"]
        assert metadata["fork_source"]["session_id"] == "s1"
        assert sessions.get(fork).load_active() == source.load_active()
        assert loop.started[0]["session_id"] == result.session_id
        assert loop.started[0]["run_kind"] is RunKind.MEMORY_REFLECTION
        source_metadata = sessions.get_metadata(source.address)
        assert {key: source_metadata.get(key) for key in bindings} == bindings
        assert source_metadata["run_kinds"] == ["channel"]
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_in_flight_guard_skips_review_but_keeps_counters() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1)
    service._agents_in_review.add("main")

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await _drain(service)

    # The due counter is preserved so the next run end retries the review.
    assert _counters(sessions) == {
        "turns_since_memory_review": 1,
        "iterations_since_skill_review": 0,
    }
    assert loop.started == []


@pytest.mark.asyncio
async def test_skill_review_waits_while_a_librarian_pass_curates_the_skills() -> None:
    curated = {"main"}
    service, sessions, loop = _make_service(
        memory_turn_interval=1, skill_model_step_interval=1, librarian_running=curated
    )

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=2)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    # Only Memory is reviewed; the Skill counts stay due for the next Run end.
    assert [review["run_kind"] for review in loop.started] == [RunKind.MEMORY_REFLECTION]
    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "iterations_since_skill_review": 2,
    }
    curated.clear()
    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=1)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert loop.started[-1]["run_kind"] is RunKind.REFLECTION
    assert _counters(sessions)["iterations_since_skill_review"] == 0


@pytest.mark.asyncio
async def test_failed_review_releases_guard_and_keeps_cycle_due_for_next_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1)
    loop.raise_on_start = RuntimeError("provider down")

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await _drain(service)

    assert "main" not in service._agents_in_review
    assert _counters(sessions)["turns_since_memory_review"] == 1
    assert any(
        "Reflection review failed" in record.getMessage()
        for record in caplog.records
        if record.name == "vbot.automation.reflection"
    )

    loop.raise_on_start = None
    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await _drain(service)

    assert len(loop.started) == 1
    assert _counters(sessions)["turns_since_memory_review"] == 0


@pytest.mark.asyncio
async def test_successful_review_preserves_activity_recorded_while_it_runs() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1)
    loop.wait_gate = asyncio.Event()

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await loop.run_started.wait()

    await _account_during_review(service, _FakeRun())
    assert _counters(sessions)["turns_since_memory_review"] == 2

    loop.wait_gate.set()
    await _drain(service)

    assert _counters(sessions)["turns_since_memory_review"] == 1


@pytest.mark.asyncio
async def test_manual_reset_during_review_preserves_activity_after_reset() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1)
    loop.wait_gate = asyncio.Event()

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await loop.run_started.wait()

    service.reset_counters("main", "s1")
    await _account_during_review(service, _FakeRun())
    assert _counters(sessions)["turns_since_memory_review"] == 1

    loop.wait_gate.set()
    await _drain(service)

    assert _counters(sessions)["turns_since_memory_review"] == 1
    assert _counter_generation(sessions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "counter"),
    [
        ("memory", "turns_since_memory_review"),
        ("skill_manage", "iterations_since_skill_review"),
    ],
)
async def test_tool_reset_during_review_preserves_activity_after_reset(
    tool_name: str, counter: str
) -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1, skill_model_step_interval=1)
    loop.wait_gate = asyncio.Event()
    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=1)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await loop.run_started.wait()

    await _account_during_review(service, _FakeRun(tool_call_names={tool_name}), outcome="failed")
    assert _counters(sessions)[counter] == 0
    await _account_during_review(service, _FakeRun(iteration_count=1))
    assert _counters(sessions)[counter] == 1

    loop.wait_gate.set()
    await _drain(service)

    assert _counters(sessions)[counter] == 1


# --- run_review orchestration --------------------------------------------------


@pytest.mark.asyncio
async def test_run_review_reports_fork_before_run_and_returns_summary() -> None:
    service, sessions, loop = _make_service()
    loop.final_content = "Patched the deploy skill."
    fork_seen_before_run: list[tuple[str, int]] = []

    result = await service.run_review(
        "main",
        "s1",
        focus="skills",
        on_fork_created=lambda fork_id: fork_seen_before_run.append((fork_id, len(loop.started))),
    )

    # The callback fired with the fork id while no review run existed yet.
    assert fork_seen_before_run == [("fork-1", 0)]
    assert (result.session_id, result.scope) == ("fork-1", "combined")
    assert result.summary == "Patched the deploy skill."
    assert sessions.titles == [("fork-1", "Main Agent")]
    assert loop.started[0]["message"] == reflection_brief(
        service._runtime.storage, "combined", focus="skills"
    )
    assert loop.started[0]["reply_surface"] is None
    assert "tool_grants" not in loop.started[0]
    # Like every Run of the Agent, a review runs under Chat's own iteration limit.
    assert "max_tool_iterations" not in loop.started[0]
    assert loop.started[0]["run_kind"] is RunKind.REFLECTION
    assert loop.started[0]["contributes_to_agent_activity"] is False
    # The review Run carries the Session it examines for accessor attribution.
    assert loop.started[0]["source_session_id"] == "s1"
    assert sessions.metadata["fork-1"]["run_kinds"] == ["reflection"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "requested", "scope"),
    [
        pytest.param(_without(memory=True), "combined", "skill", id="combined-without-memory"),
        pytest.param(
            _without(skill_manage=True), "combined", "memory", id="combined-without-skill-manage"
        ),
        pytest.param(_without(memory=True), "skill", "skill", id="skill-without-memory"),
        pytest.param(_without(skill=True), "skill", None, id="skill-without-skill"),
        pytest.param(
            _without(memory=True, skill_manage=True), "combined", None, id="nothing-callable"
        ),
        pytest.param(
            SimpleNamespace(**{**vars(_identity_agent()), "workspace": ""}),
            "combined",
            None,
            id="config-agent",
        ),
    ],
)
async def test_run_review_narrows_to_the_callable_scope_or_refuses_before_forking(
    agent: Any, requested: str, scope: str | None
) -> None:
    service, sessions, loop = _make_service(agent=agent)

    if scope is None:
        with pytest.raises(ReflectionUnavailableError):
            await service.run_review("main", "s1", review_scope=cast("Any", requested))
        assert (sessions.forks, loop.started) == ([], [])
        return
    result = await service.run_review("main", "s1", review_scope=cast("Any", requested))

    assert result.scope == scope
    [review] = loop.started
    tool_restriction, run_kind = _REVIEWS[scope]
    assert review["tool_restriction"] == tool_restriction
    assert review["run_kind"] is run_kind
    assert review["message"] == reflection_brief(service._runtime.storage, cast("Any", scope))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "callable_tools"),
    [
        ("memory", "`memory`"),
        ("skill", "`skill` and `skill_manage`"),
        ("combined", "`memory`, `skill`, and `skill_manage`"),
    ],
)
async def test_review_run_names_and_reaches_only_the_tools_of_its_scope(
    tmp_path: Path, scope: str, callable_tools: str
) -> None:
    bundled = StorageManager(data_dir=tmp_path / "data")
    service, _sessions, loop = _make_service()
    service._runtime.storage.read_prompt_fragment = bundled.read_prompt_fragment  # type: ignore[method-assign]

    await service.run_review("main", "s1", review_scope=cast("Any", scope))

    [review] = loop.started
    tools = _REVIEWS[scope][0]
    assert review["tool_restriction"] == tools
    # A review that can write Skills learns that the list marks the ones it cannot change.
    assert ("does not mark as read-only; all other Skills are read-only" in review["message"]) == (
        "skill_manage" in tools
    )
    boundary = "in this review. This review"
    # The production brief names exactly the Tools its Run can call: backticked
    # identifiers are Tool names unless they are parameters of those Tools or
    # values those parameters take. ``skill_manage`` accepts ``absorbed_into``
    # without advertising it; the briefs that need it teach it.
    properties = [
        (name, schema)
        for tool in tools
        for name, schema in _TOOL_PARAMETERS[tool]["properties"].items()
    ]
    parameters = {name for name, _schema in properties}
    if "skill_manage" in tools:
        parameters.add("absorbed_into")
    values = {value for _name, schema in properties for value in schema.get("enum", ())}
    identifiers = {token for token in review["message"].split("`")[1::2] if token.isidentifier()}
    assert identifiers - parameters - values == set(tools)
    # Any other Tool is refused before it runs, naming what the review can call.
    deny = review["tool_denial_resolver"]
    assert [deny(tool) for tool in tools] == [None] * len(tools)
    for other in sorted({"read", *REFLECTION_TOOL_RESTRICTION} - set(tools)):
        assert deny(other) == (
            f"Nothing was run: `{other}` is not available {boundary} "
            f"can call only {callable_tools}. Do not retry this call."
        )


_TOOL_PARAMETERS: dict[str, Any] = {
    "memory": MEMORY_TOOL_PARAMETERS,
    "skill": SKILL_TOOL_PARAMETERS,
    "skill_manage": SKILL_MANAGE_TOOL_PARAMETERS,
}


@pytest.mark.parametrize(
    ("scope", "expected"),
    [("combined", (0, 0)), ("memory", (0, 12)), ("skill", (7, 0))],
)
def test_reset_counters_zeroes_the_reviewed_dimensions(
    scope: str, expected: tuple[int, int]
) -> None:
    service, sessions, _loop = _make_service()
    sessions.metadata["s1"] = {
        "title": "kept",
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 7,
            "iterations_since_skill_review": 12,
        },
    }

    service.reset_counters("main", "s1", review_scope=cast("Any", scope))

    assert sessions.metadata["s1"]["title"] == "kept"
    assert _counters(sessions) == {
        "turns_since_memory_review": expected[0],
        "iterations_since_skill_review": expected[1],
    }
    assert _counter_generation(sessions) == 1


@pytest.mark.parametrize("prompt", ["skill_maintenance.md", "skill", "combined", "learn"])
def test_real_skill_authoring_prompts_do_not_teach_removed_fields(
    tmp_path: Path, prompt: str
) -> None:
    bundled = StorageManager(data_dir=tmp_path / "data")
    text = (
        bundled.read_prompt_fragment(prompt)
        if prompt.endswith(".md")
        else learn_brief(bundled, None)
        if prompt == "learn"
        else reflection_brief(bundled, cast("Any", prompt))
    )

    # Retired authoring fields are not taught.
    for removed in ("file_content", "`match`"):
        assert removed not in text
    # The catalog shows origin headings, not origin tags.
    assert "origin `agent`" not in text
