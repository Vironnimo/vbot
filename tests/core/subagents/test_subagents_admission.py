"""Generation-safe Sub-Agent sends and accepted-input settings preparation."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext

import pytest

from core.chat import ChatMessage
from core.runs import Run, RunAdmissionBlockedError
from core.subagents.links import find_link
from tests.core.subagents.subagents_test_support import SubAgentHarness
from tests.core.subagents.subagents_test_support import harness as harness

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("replace", [False, True], ids=["archived", "replaced"])
async def test_send_refuses_a_generation_archived_during_preflight(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    child = await harness.spawn("review", model="fixture/original")
    await harness.settle()
    address = harness.subagent_session(child["id"])
    generation = harness.sessions.get(address).generation_id
    entered, release = asyncio.Event(), asyncio.Event()
    resolve = harness.resolver.session_working_project_async

    async def held_resolution(target):
        result = await resolve(target)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(harness.resolver, "session_working_project_async", held_resolution)
    sending = asyncio.create_task(
        harness.call(
            {
                "action": "send",
                "id": child["id"],
                "content": "follow-up",
                "model": "fixture/rejected",
                "thinking_effort": "high",
            }
        )
    )
    await entered.wait()
    async with harness.manager.session_admission_guard(address):
        await harness.sessions.archive(address)
        if replace:
            replacement = harness.sessions.create(
                address.agent_id, address.session_id, project_id=address.project_id
            )
            assert replacement.generation_id != generation
            harness.resolver.update_session_overrides(address, {"model": "fixture/replacement"})
            replacement.append(ChatMessage.user("replacement history"))
    notices_before = list(harness.triggers.notices)
    release.set()
    result = await sending
    await harness.settle()

    assert result["ok"] is False
    assert result["error"]["code"] == "subagent_not_found"
    assert harness.sessions.exists(address) is replace
    assert find_link(harness.sessions, child["id"]) is None
    assert len(harness.loop.turns) == 1
    assert harness.triggers.notices == notices_before
    assert harness.manager.waiting_work_count() == 0
    if replace:
        stored = await harness.resolver.session_overrides_async(address)
        assert (stored.model, stored.thinking_effort) == ("fixture/replacement", None)
        assert [message.content for message in harness.sessions.get(address).load()] == [
            "replacement history"
        ]


@pytest.mark.parametrize("rejection", ["capacity", "transition", "shutdown"])
async def test_rejected_send_keeps_settings_and_waiting_capacity(
    harness: SubAgentHarness, rejection: str
) -> None:
    if rejection == "capacity":
        harness.loop.hold("review")
    child = await harness.spawn("review", model="fixture/original", thinking_effort="low")
    address = harness.subagent_session(child["id"])
    reservations = []
    if rejection == "capacity":
        reservations = [
            harness.manager.reserve_waiting_work(scope="fixture:inbound", scope_limit=32)
            for _ in range(32)
        ]
    else:
        await harness.settle()
        if rejection == "shutdown":
            await harness.manager.aclose()
    before = harness.manager.waiting_work_count()
    guard = (
        harness.manager.session_admission_guard(address)
        if rejection == "transition"
        else nullcontext()
    )
    async with guard:
        result = await harness.call(
            {
                "action": "send",
                "id": child["id"],
                "content": "rejected",
                "model": "fixture/rejected",
                "thinking_effort": "high",
            }
        )

    assert result["ok"] is False
    assert harness.manager.waiting_work_count() == before
    assert (
        harness.manager.list_queued(
            address.agent_id, address.session_id, project_id=address.project_id
        )
        == []
    )
    stored = await harness.resolver.session_overrides_async(address)
    assert (stored.model, stored.thinking_effort) == ("fixture/original", "low")
    assert all(turn.run is None for turn in harness.loop.turns if turn.content == "rejected")
    for reservation in reservations:
        assert harness.manager.release_waiting_work(reservation)
    assert harness.manager.waiting_work_count() == 0


@pytest.mark.parametrize(
    ("busy", "drain"),
    [(False, False), (True, False), (True, True)],
    ids=["idle", "busy", "drained-during-preparation"],
)
async def test_send_preparation_claims_work_without_blocking_other_sessions(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch, busy: bool, drain: bool
) -> None:
    if busy:
        harness.loop.hold("review")
    child = await harness.spawn("review", model="fixture/original", thinking_effort="low")
    if not busy:
        await harness.settle()
    address = harness.subagent_session(child["id"])
    generation = harness.sessions.get(address).generation_id
    entered, release, executed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    run_async = harness.sessions.run_async
    build_executor = harness.loop.run_executor

    async def held_write(function, *args, **kwargs):
        if function == harness.resolver.update_session_overrides:
            entered.set()
            await release.wait()
        return await run_async(function, *args, **kwargs)

    def observe_executor(content, **kwargs):
        executor = build_executor(content, **kwargs)

        async def execute(run: Run):
            if content == "more":
                stored = await harness.resolver.session_overrides_async(address)
                assert (stored.model, stored.thinking_effort) == ("fixture/accepted", "high")
                assert run.expected_session_generation_id == generation
                executed.set()
            return await executor(run)

        return execute

    monkeypatch.setattr(harness.sessions, "run_async", held_write)
    monkeypatch.setattr(harness.loop, "run_executor", observe_executor)
    sending = asyncio.create_task(
        harness.call(
            {
                "action": "send",
                "id": child["id"],
                "content": "more",
                "model": "fixture/accepted",
                "thinking_effort": "high",
            }
        )
    )
    try:
        await entered.wait()
        assert not executed.is_set()
        assert harness.manager.waiting_work_count() == int(busy)
        if busy:
            [queued] = harness.manager.list_queued(
                address.agent_id, address.session_id, project_id=address.project_id
            )
            assert not queued.steerable and not queued.steering
            assert queued.admission.expected_session_generation_id == generation
        else:
            admitted = harness.manager.active_run(
                agent_id=address.agent_id,
                session_id=address.session_id,
                project_id=address.project_id,
            )
            assert admitted is not None
            await admitted.wait_admitted()
        with pytest.raises(RunAdmissionBlockedError):
            async with harness.manager.session_admission_guard(address):
                pytest.fail("preparing input must protect its Session from lifecycle changes")

        # No global Run-manager lock is held across the stalled settings write.
        unrelated = await harness.start_turn(harness.parent, "unrelated")
        await unrelated.wait()
        if drain:
            initial_run = harness.manager.active_run(
                agent_id=address.agent_id,
                session_id=address.session_id,
                project_id=address.project_id,
            )
            assert initial_run is not None
            harness.loop.hold("review").set()
            await initial_run.wait()
            drained = await queued.future
            assert drained is not initial_run
            await drained.wait_admitted()
            assert not executed.is_set()
            assert harness.manager.waiting_work_count() == 0
        sending.cancel()
        await asyncio.sleep(0)
        assert not sending.done()
    finally:
        release.set()
    result = await sending
    assert result["ok"], result
    if busy:
        await harness.finished("review")
    else:
        await harness.settle()
    assert executed.is_set()
    assert harness.manager.waiting_work_count() == 0


async def test_withdrawn_input_stays_ineligible_when_preparation_finishes(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.loop.hold("review")
    child = await harness.spawn("review")
    address = harness.subagent_session(child["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    run_async = harness.sessions.run_async

    async def held_write(function, *args, **kwargs):
        if function == harness.resolver.update_session_overrides:
            entered.set()
            await release.wait()
        return await run_async(function, *args, **kwargs)

    monkeypatch.setattr(harness.sessions, "run_async", held_write)
    sending = asyncio.create_task(
        harness.call(
            {"action": "send", "id": child["id"], "content": "withdrawn", "model": "fixture/new"}
        )
    )
    try:
        await entered.wait()
        [item] = harness.manager.list_queued(
            address.agent_id, address.session_id, project_id=address.project_id
        )
        assert harness.manager.remove_queued(
            address.agent_id, address.session_id, item.item_id, project_id=address.project_id
        )
        assert harness.manager.waiting_work_count() == 0
    finally:
        release.set()
    result = await sending
    assert result["ok"], result  # Accepted input was subsequently withdrawn, not rejected.
    assert item.future.cancelled() and not item.steerable and not item.steering
    await harness.finished("review")
    assert all(turn.run is None for turn in harness.loop.turns if turn.content == "withdrawn")


@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_failed_override_preparation_never_executes_input_or_leaks_capacity(
    harness: SubAgentHarness, monkeypatch: pytest.MonkeyPatch, busy: bool
) -> None:
    if busy:
        harness.loop.hold("review")
    child = await harness.spawn("review", model="fixture/original")
    if not busy:
        await harness.settle()
    address = harness.subagent_session(child["id"])
    run_async = harness.sessions.run_async

    async def failed_write(function, *args, **kwargs):
        if function == harness.resolver.update_session_overrides:
            raise ValueError("fixture preparation failure")
        return await run_async(function, *args, **kwargs)

    monkeypatch.setattr(harness.sessions, "run_async", failed_write)
    result = await harness.call(
        {"action": "send", "id": child["id"], "content": "not executed", "model": "fixture/new"}
    )
    assert result["ok"] is False
    if not busy:
        await harness.settle()
    assert harness.manager.waiting_work_count() == 0
    assert (
        harness.manager.list_queued(
            address.agent_id, address.session_id, project_id=address.project_id
        )
        == []
    )
    assert (await harness.resolver.session_overrides_async(address)).model == "fixture/original"
    assert all(turn.run is None for turn in harness.loop.turns if turn.content == "not executed")
