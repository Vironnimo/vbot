"""Tests for the background reflection service (cadence + fork review orchestration)."""

from __future__ import annotations

import asyncio
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
    _review_scope,
)
from core.runs import RunKind
from core.sessions import SESSION_FORK_ALWAYS_STRIP_META_KEYS, SessionAddress

REFLECT_BRIEFS = {
    "reflect-memory.md": "Review this Session for durable Memory updates.",
    "reflect-skill.md": "Review this Session for durable Skill updates.",
    "reflect.md": "Review this Session for Memory and Skill updates.",
}


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

    def get_metadata(self, address: SessionAddress) -> dict[str, Any]:
        return dict(self.metadata.get(address.session_id, {}))

    def set_metadata(self, address: SessionAddress, data: dict[str, Any]) -> None:
        self.metadata[address.session_id] = dict(data)

    def mutate_metadata(self, address: SessionAddress, mutation: Any) -> dict[str, Any]:
        metadata = self.get_metadata(address)
        mutation(metadata)
        self.set_metadata(address, metadata)
        return metadata

    def set_title(self, address: SessionAddress, title: str) -> str:
        self.titles.append((address.session_id, title))
        return title

    def record_run_kind(self, address: SessionAddress, run_kind: RunKind) -> None:
        metadata = self.metadata.setdefault(address.session_id, {})
        metadata.setdefault("run_kinds", []).append(run_kind.value)

    async def fork(self, source: SessionAddress, **kwargs: Any) -> Any:
        self.fork_counter += 1
        fork_id = f"fork-{self.fork_counter}"
        self.forks.append(
            {
                "source_agent_id": source.agent_id,
                "session_id": source.session_id,
                **kwargs,
            }
        )
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
) -> tuple[ReflectionService, _FakeSessions, _FakeLoop]:
    sessions = _FakeSessions()
    loop = _FakeLoop()
    runtime = SimpleNamespace(
        storage=SimpleNamespace(
            load_reflection_settings=lambda: {
                "enabled": enabled,
                "memory_turn_interval": memory_turn_interval,
                "skill_model_step_interval": skill_model_step_interval,
            },
            read_prompt_fragment=lambda name: REFLECT_BRIEFS[name],
        ),
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: _identity_agent()
        ),
        chat_sessions=sessions,
        streaming_chat_loop=loop,
    )
    return ReflectionService(cast("Any", runtime)), sessions, loop


def _counters(sessions: _FakeSessions, session_id: str = "s1") -> dict[str, int]:
    raw = cast("dict[str, int]", sessions.metadata[session_id][REFLECTION_COUNTERS_META_KEY])
    return {
        "turns_since_memory_review": raw["turns_since_memory_review"],
        "iterations_since_skill_review": raw["iterations_since_skill_review"],
    }


def _counter_generation(sessions: _FakeSessions, session_id: str = "s1") -> int:
    raw = cast("dict[str, int]", sessions.metadata[session_id][REFLECTION_COUNTERS_META_KEY])
    return raw[COUNTER_GENERATION_KEY]


def _identity_agent(*, memory_prompt_mode: str = "agent_user") -> Any:
    return SimpleNamespace(
        id="main",
        name="Main Agent",
        workspace="/data/workspace-main",
        memory_prompt_mode=memory_prompt_mode,
    )


async def _drain(service: ReflectionService) -> None:
    while service._background_tasks:
        await asyncio.gather(*list(service._background_tasks))


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
@pytest.mark.parametrize(
    ("outcome", "cancel_reason", "iteration_count"),
    [
        ("error", None, 3),
        ("cancelled", None, 3),
        ("cancelled", "shutdown", 3),
        ("cancelled", "user", 0),
    ],
)
async def test_failed_internal_and_immediate_user_cancelled_runs_never_count(
    outcome: str, cancel_reason: str | None, iteration_count: int
) -> None:
    service, sessions, loop = _make_service()

    service.notify_run_end(
        cast(
            "Any",
            _FakeRun(cancel_reason=cancel_reason, iteration_count=iteration_count),
        ),
        _identity_agent(),
        internal=False,
        outcome=outcome,
    )
    await _drain(service)

    assert sessions.metadata == {}
    assert loop.started == []


@pytest.mark.asyncio
async def test_user_cancelled_run_with_model_activity_counts_and_triggers_review() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=2, skill_model_step_interval=5)
    sessions.metadata["s1"] = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 1,
            "iterations_since_skill_review": 3,
        }
    }

    service.notify_run_end(
        cast(
            "Any",
            _FakeRun(cancel_reason="user", iteration_count=2),
        ),
        _identity_agent(),
        internal=False,
        outcome="cancelled",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "iterations_since_skill_review": 0,
    }
    assert len(loop.started) == 1
    assert loop.started[0]["message"] == REFLECT_BRIEFS["reflect.md"]
    assert loop.started[0]["tool_restriction"] == REFLECTION_TOOL_RESTRICTION


@pytest.mark.asyncio
async def test_memory_call_resets_counter_even_when_the_run_later_fails() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=3)
    sessions.metadata["s1"] = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 2,
            "iterations_since_skill_review": 4,
        }
    }

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=3, tool_call_names={"memory"})),
        _identity_agent(),
        internal=False,
        outcome="error",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "iterations_since_skill_review": 4,
    }
    assert loop.started == []


@pytest.mark.asyncio
async def test_config_agents_without_workspace_never_count() -> None:
    service, sessions, loop = _make_service()
    config_agent = SimpleNamespace(id="builder", workspace="")

    service.notify_run_end(cast("Any", _FakeRun()), config_agent, internal=False, outcome="success")
    await _drain(service)

    assert sessions.metadata == {}
    assert loop.started == []


@pytest.mark.asyncio
async def test_agents_without_the_memory_tool_never_count_or_reflect() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1)

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=20)),
        _identity_agent(memory_prompt_mode="off"),
        internal=False,
        outcome="success",
    )
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
    service, sessions, loop = _make_service(memory_turn_interval=3, skill_model_step_interval=10)

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=4)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 1,
        "iterations_since_skill_review": 4,
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
    service, sessions, loop = _make_service(memory_turn_interval=2, skill_model_step_interval=100)
    sessions.metadata["s1"] = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 1,
            "iterations_since_skill_review": 5,
        }
    }

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=2)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    # Turns reset (memory reviewed); Iterations keep accumulating (skill not due).
    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "iterations_since_skill_review": 7,
    }
    assert len(loop.started) == 1
    review = loop.started[0]
    assert review["internal"] is True
    assert review["tool_restriction"] == MEMORY_REFLECTION_TOOL_RESTRICTION
    assert "tool_grants" not in review
    assert review["session_id"] == "fork-1"
    assert review["message"] == REFLECT_BRIEFS["reflect-memory.md"]


@pytest.mark.asyncio
async def test_skill_threshold_triggers_focused_review_and_resets_iterations() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=100, skill_model_step_interval=5)

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=6)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 1,
        "iterations_since_skill_review": 0,
    }
    assert len(loop.started) == 1
    assert loop.started[0]["message"] == REFLECT_BRIEFS["reflect-skill.md"]
    assert loop.started[0]["tool_restriction"] == SKILL_REFLECTION_TOOL_RESTRICTION


@pytest.mark.asyncio
async def test_both_thresholds_due_runs_the_bare_brief_and_resets_both() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1, skill_model_step_interval=1)

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=3)),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "iterations_since_skill_review": 0,
    }
    assert len(loop.started) == 1
    assert loop.started[0]["message"] == REFLECT_BRIEFS["reflect.md"]
    assert loop.started[0]["tool_restriction"] == REFLECTION_TOOL_RESTRICTION


@pytest.mark.asyncio
async def test_memory_tool_call_resets_memory_cadence_without_counting_the_run() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=3, skill_model_step_interval=100)
    sessions.metadata["s1"] = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 2,
            "iterations_since_skill_review": 4,
        }
    }

    service.notify_run_end(
        cast(
            "Any",
            _FakeRun(iteration_count=3, tool_call_names={"memory", "read"}),
        ),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "iterations_since_skill_review": 7,
    }
    assert loop.started == []


@pytest.mark.asyncio
async def test_memory_tool_call_suppresses_memory_dimension_when_skill_is_due() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1, skill_model_step_interval=3)

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=3, tool_call_names={"memory"})),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "iterations_since_skill_review": 0,
    }
    assert loop.started[0]["message"] == REFLECT_BRIEFS["reflect-skill.md"]
    assert loop.started[0]["tool_restriction"] == SKILL_REFLECTION_TOOL_RESTRICTION
    assert loop.started[0]["run_kind"] is RunKind.SKILL_REFLECTION


@pytest.mark.asyncio
async def test_skill_manage_call_resets_skill_cadence_without_counting_the_run() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=100, skill_model_step_interval=10)
    sessions.metadata["s1"] = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 2,
            "iterations_since_skill_review": 9,
        }
    }

    service.notify_run_end(
        cast(
            "Any",
            _FakeRun(iteration_count=3, tool_call_names={"skill_manage", "read"}),
        ),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 3,
        "iterations_since_skill_review": 0,
    }
    assert loop.started == []


@pytest.mark.asyncio
async def test_skill_manage_call_suppresses_skill_dimension_when_memory_is_due() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1, skill_model_step_interval=100)
    sessions.metadata["s1"] = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 0,
            "iterations_since_skill_review": 90,
        }
    }

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=3, tool_call_names={"skill_manage"})),
        _identity_agent(),
        internal=False,
        outcome="success",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "iterations_since_skill_review": 0,
    }
    assert loop.started[0]["message"] == REFLECT_BRIEFS["reflect-memory.md"]
    assert loop.started[0]["tool_restriction"] == MEMORY_REFLECTION_TOOL_RESTRICTION
    assert loop.started[0]["run_kind"] is RunKind.MEMORY_REFLECTION


@pytest.mark.asyncio
async def test_skill_manage_call_resets_counter_even_when_the_run_later_fails() -> None:
    service, sessions, loop = _make_service(skill_model_step_interval=10)
    sessions.metadata["s1"] = {
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 2,
            "iterations_since_skill_review": 9,
        }
    }

    service.notify_run_end(
        cast("Any", _FakeRun(iteration_count=3, tool_call_names={"skill_manage"})),
        _identity_agent(),
        internal=False,
        outcome="error",
    )
    await _drain(service)

    assert _counters(sessions) == {
        "turns_since_memory_review": 2,
        "iterations_since_skill_review": 0,
    }
    assert loop.started == []


@pytest.mark.asyncio
async def test_review_fork_is_stripped_and_titled() -> None:
    service, sessions, loop = _make_service(memory_turn_interval=1)
    sessions.metadata["s1"] = {"title": "Refactor plan"}

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await _drain(service)

    assert sessions.forks[0]["strip_meta_keys"] == SESSION_FORK_ALWAYS_STRIP_META_KEYS
    assert sessions.titles == [("fork-1", "Main Agent: Refactor plan")]
    assert loop.started[0]["run_kind"] is RunKind.MEMORY_REFLECTION
    assert sessions.metadata["fork-1"]["run_kinds"] == ["memory_reflection"]


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

    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await asyncio.sleep(0)
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
    service.notify_run_end(
        cast("Any", _FakeRun()), _identity_agent(), internal=False, outcome="success"
    )
    await asyncio.sleep(0)
    assert _counters(sessions)["turns_since_memory_review"] == 1

    loop.wait_gate.set()
    await _drain(service)

    assert _counters(sessions)["turns_since_memory_review"] == 1
    assert _counter_generation(sessions) == 1


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
    assert sessions.titles == [("fork-1", "Main Agent")]
    assert loop.started[0]["message"] == (
        f"{REFLECT_BRIEFS['reflect.md']}\n\nThe user asked you to focus this reflection on:\nskills"
    )
    assert loop.started[0]["reply_surface"] is None
    assert "tool_grants" not in loop.started[0]
    assert loop.started[0]["run_kind"] is RunKind.REFLECTION
    assert loop.started[0]["contributes_to_agent_activity"] is False
    assert sessions.metadata["fork-1"]["run_kinds"] == ["reflection"]


@pytest.mark.asyncio
async def test_run_review_records_scope_specific_run_kinds() -> None:
    service, sessions, loop = _make_service()

    await service.run_review("main", "s1", review_scope="memory")
    await service.run_review("main", "s1", review_scope="skill")

    assert [call["run_kind"] for call in loop.started] == [
        RunKind.MEMORY_REFLECTION,
        RunKind.SKILL_REFLECTION,
    ]
    assert sessions.metadata["fork-1"]["run_kinds"] == ["memory_reflection"]
    assert sessions.metadata["fork-2"]["run_kinds"] == ["skill_reflection"]


@pytest.mark.asyncio
async def test_reset_counters_zeroes_both_dimensions() -> None:
    service, sessions, _loop = _make_service()
    sessions.metadata["s1"] = {
        "title": "kept",
        REFLECTION_COUNTERS_META_KEY: {
            "turns_since_memory_review": 7,
            "iterations_since_skill_review": 12,
        },
    }

    service.reset_counters("main", "s1")

    assert sessions.metadata["s1"]["title"] == "kept"
    assert _counters(sessions) == {
        "turns_since_memory_review": 0,
        "iterations_since_skill_review": 0,
    }
    assert _counter_generation(sessions) == 1


def test_review_scope_shapes() -> None:
    assert _review_scope(True, True) == "combined"
    assert _review_scope(True, False) == "memory"
    assert _review_scope(False, True) == "skill"


@pytest.mark.parametrize(
    ("fragment_name", "allowed_tools"),
    [
        ("reflect-memory.md", "Use only `memory`"),
        ("reflect-skill.md", "Use only `skill` and `skill_manage`"),
        ("reflect.md", "Use only `memory`, `skill`, and `skill_manage`"),
    ],
)
def test_real_reflection_prompts_define_their_tool_boundary(
    fragment_name: str, allowed_tools: str
) -> None:
    prompt_path = Path(__file__).parents[3] / "resources" / "prompts" / fragment_name
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "every other Tool is disabled" in prompt
    assert allowed_tools in prompt
    if fragment_name != "reflect-memory.md":
        assert "Call `skill` with no arguments to list your Skills" in prompt


@pytest.mark.parametrize("fragment_name", ["reflect-skill.md", "reflect.md"])
def test_real_skill_reflection_prompts_use_compact_private_authoring_contract(
    fragment_name: str,
) -> None:
    prompt_path = Path(__file__).parents[3] / "resources" / "prompts" / fragment_name
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "old_string" not in prompt
    assert "new_string" not in prompt
    assert "file_content" not in prompt
    assert "cannot execute support scripts" in prompt


@pytest.mark.parametrize(
    "fragment_name",
    ["skill_maintenance.md", "reflect-skill.md", "reflect.md", "learn.md"],
)
def test_real_skill_authoring_prompts_do_not_teach_removed_fields(fragment_name: str) -> None:
    prompt_path = Path(__file__).parents[3] / "resources" / "prompts" / fragment_name
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "file_content" not in prompt
    assert "old_string" not in prompt
    assert "new_string" not in prompt
    assert "replace_all" not in prompt


def test_real_skill_maintenance_prompt_forbids_implicit_global_fallback() -> None:
    prompt_path = Path(__file__).parents[3] / "resources" / "prompts" / "skill_maintenance.md"
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "user-facing Skill controls" in prompt
    assert "do not call any Tool" in prompt
    assert "offer workarounds" in prompt
    assert "instead of silently changing another scope" in prompt
