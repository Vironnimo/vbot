"""Tests for the background reflection service (cadence + fork review orchestration)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.automation.reflection import (
    REFLECTION_COUNTERS_META_KEY,
    REFLECTION_TOOL_RESTRICTION,
    ReflectionService,
    _cadence_instruction,
)
from core.sessions import SESSION_FORK_ALWAYS_STRIP_META_KEYS

REFLECT_BRIEF = "Review this session and update your memory and skill library."


class _FakeRun:
    """Minimal stand-in for a chat Run: identity fields plus a final message."""

    def __init__(
        self,
        *,
        agent_id: str = "main",
        session_id: str = "s1",
        project_id: str | None = None,
        tool_call_count: int = 0,
        final_content: str = "Saved a memory about the user.",
    ) -> None:
        self.agent_id = agent_id
        self.session_id = session_id
        self.project_id = project_id
        self.tool_call_count = tool_call_count
        self._final_content = final_content

    async def wait(self) -> Any:
        return SimpleNamespace(content=self._final_content)


class _FakeSessions:
    """In-memory session manager stub: metadata sidecars, titles, forks."""

    def __init__(self) -> None:
        self.metadata: dict[str, dict[str, Any]] = {}
        self.titles: list[tuple[str, str]] = []
        self.forks: list[dict[str, Any]] = []
        self.fork_counter = 0

    def get_metadata(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        return dict(self.metadata.get(session_id, {}))

    def set_metadata(
        self,
        agent_id: str,
        session_id: str,
        data: dict[str, Any],
        project_id: str | None = None,
    ) -> None:
        self.metadata[session_id] = dict(data)

    def set_title(
        self, agent_id: str, session_id: str, title: str, project_id: str | None = None
    ) -> str:
        self.titles.append((session_id, title))
        return title

    async def fork(self, source_agent_id: str, session_id: str, **kwargs: Any) -> Any:
        self.fork_counter += 1
        fork_id = f"fork-{self.fork_counter}"
        self.forks.append({"source_agent_id": source_agent_id, "session_id": session_id, **kwargs})
        return SimpleNamespace(id=fork_id)


class _FakeLoop:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.final_content = "Saved a memory about the user."
        self.raise_on_start: Exception | None = None

    async def start_run(self, agent_id: str, content: str, **kwargs: Any) -> _FakeRun:
        if self.raise_on_start is not None:
            raise self.raise_on_start
        self.started.append({"agent_id": agent_id, "message": content, **kwargs})
        return _FakeRun(final_content=self.final_content)


def _make_service(
    *,
    enabled: bool = True,
    memory_turn_interval: int = 3,
    skill_tool_call_interval: int = 10,
) -> tuple[ReflectionService, _FakeSessions, _FakeLoop]:
    sessions = _FakeSessions()
    loop = _FakeLoop()
    runtime = SimpleNamespace(
        storage=SimpleNamespace(
            load_reflection_settings=lambda: {
                "enabled": enabled,
                "memory_turn_interval": memory_turn_interval,
                "skill_tool_call_interval": skill_tool_call_interval,
            },
            read_prompt_fragment=lambda name: REFLECT_BRIEF,
        ),
        chat_sessions=sessions,
        streaming_chat_loop=loop,
    )
    return ReflectionService(cast("Any", runtime)), sessions, loop


def _counters(sessions: _FakeSessions, session_id: str = "s1") -> dict[str, int]:
    return cast("dict[str, int]", sessions.metadata[session_id][REFLECTION_COUNTERS_META_KEY])


def _identity_agent() -> Any:
    return SimpleNamespace(id="main", workspace="/data/workspace-main")


async def _drain(service: ReflectionService) -> None:
    while service._background_tasks:
        await asyncio.gather(*list(service._background_tasks))


# --- notify_run_end inline gates ---------------------------------------------


@pytest.mark.asyncio
async def test_internal_runs_never_count() -> None:
    service, sessions, loop = _make_service()

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=True, outcome="success"
    )
    await _drain(service)

    assert sessions.metadata == {}
    assert loop.started == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["error", "cancelled"])
async def test_unsuccessful_runs_never_count(outcome: str) -> None:
    service, sessions, loop = _make_service()

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome=outcome
    )
    await _drain(service)

    assert sessions.metadata == {}
    assert loop.started == []


@pytest.mark.asyncio
async def test_config_agents_without_workspace_never_count() -> None:
    service, sessions, loop = _make_service()
    config_agent = SimpleNamespace(id="builder", workspace="")

    service.notify_run_end(cast("Any", _FakeRun()), config_agent, internal=False, outcome="success")
    await _drain(service)

    assert sessions.metadata == {}
    assert loop.started == []


# --- cadence accounting -------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_feature_writes_nothing() -> None:
    service, sessions, loop = _make_service(enabled=False)

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await _drain(service)

    assert sessions.metadata == {}
    assert loop.started == []


@pytest.mark.asyncio
async def test_below_threshold_increments_and_persists_counters() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=3, skill_tool_call_interval=10)

    service.notify_run_end(
        cast("Any", _FakeRun(tool_call_count=4)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 1,
        "tool_calls_since_skill_review": 4,
    }
    assert loop.started == []


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


@pytest.mark.asyncio
async def test_memory_threshold_triggers_focused_review_and_resets_turns() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=2, skill_tool_call_interval=100)
    sessions.metadata["s1"] = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 1,
            "tool_calls_since_skill_review": 5,
        }
    }

    service.notify_run_end(
        cast("Any", _FakeRun(tool_call_count=2)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    # Turns reset (memory reviewed); tool calls keep accumulating (skill not due).
    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "tool_calls_since_skill_review": 7,
    }
    assert len(loop.started) == 1
    review = loop.started[0]
    assert review["internal"] is True
    assert review["tool_restriction"] == REFLECTION_TOOL_RESTRICTION
    assert review["session_id"] == "fork-1"
    assert review["message"].startswith(REFLECT_BRIEF)
    assert "memory cadence" in review["message"]


@pytest.mark.asyncio
async def test_skill_threshold_triggers_focused_review_and_resets_tool_calls() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=100, skill_tool_call_interval=5)

    service.notify_run_end(
        cast("Any", _FakeRun(tool_call_count=6)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 1,
        "tool_calls_since_skill_review": 0,
    }
    assert len(loop.started) == 1
    assert "skill cadence" in loop.started[0]["message"]


@pytest.mark.asyncio
async def test_both_thresholds_due_runs_the_bare_brief_and_resets_both() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1, skill_tool_call_interval=1)

    service.notify_run_end(
        cast("Any", _FakeRun(tool_call_count=3)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "tool_calls_since_skill_review": 0,
    }
    assert len(loop.started) == 1
    assert loop.started[0]["message"] == REFLECT_BRIEF


@pytest.mark.asyncio
async def test_review_fork_is_stripped_and_titled() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1)
    sessions.metadata["s1"] = {"title": "Refactor plan"}

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await _drain(service)

    assert sessions.forks[0]["strip_meta_keys"] == SESSION_FORK_ALWAYS_STRIP_META_KEYS
    assert sessions.titles == [("fork-1", "Reflection: Refactor plan")]


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
        "tool_calls_since_skill_review": 0,
    }
    assert loop.started == []


@pytest.mark.asyncio
async def test_failed_review_releases_the_guard_and_costs_the_cycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1)
    loop.raise_on_start = RuntimeError("provider down")

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await _drain(service)

    assert "main" not in service._agents_in_review
    # Counter was reset at trigger time — a failed review costs the cycle.
    assert _counters(sessions)["turns_since_memory_review"] == 0
    assert any(
        "Reflection review failed" in record.getMessage()
        for record in caplog.records
        if record.name == "vbot.automation.reflection"
    )


# --- run_review orchestration --------------------------------------------------


@pytest.mark.asyncio
async def test_run_review_reports_fork_before_run_and_returns_summary() -> None:
    service, sessions, loop = _make_service()
    loop.final_content = "Patched the deploy skill."
    fork_seen_before_run: list[tuple[str, int]] = []

    result = await service.run_review(
        "main",
        "s1",
        extra_instruction="The user asked you to focus this reflection on:\nskills",
        on_fork_created=lambda fork_id: fork_seen_before_run.append((fork_id, len(loop.started))),
    )

    # The callback fired with the fork id while no review run existed yet.
    assert fork_seen_before_run == [("fork-1", 0)]
    assert result.session_id == "fork-1"
    assert result.summary == "Patched the deploy skill."
    assert sessions.titles == [("fork-1", "Reflection")]
    assert loop.started[0]["message"] == (
        f"{REFLECT_BRIEF}\n\nThe user asked you to focus this reflection on:\nskills"
    )
    assert loop.started[0]["reply_surface"] is None


@pytest.mark.asyncio
async def test_reset_counters_zeroes_both_dimensions() -> None:
    service, sessions, _loop = _make_service()
    sessions.metadata["s1"] = {
        "title": "kept",
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 7,
            "tool_calls_since_skill_review": 12,
        },
    }

    service.reset_counters("main", "s1")

    assert sessions.metadata["s1"]["title"] == "kept"
    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "tool_calls_since_skill_review": 0,
    }


def test_cadence_instruction_shapes() -> None:
    assert _cadence_instruction(True, True) is None
    memory_note = _cadence_instruction(True, False)
    skill_note = _cadence_instruction(False, True)
    assert memory_note is not None and "memory cadence" in memory_note
    assert skill_note is not None and "skill cadence" in skill_note
