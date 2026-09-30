"""Swarm stop and resume: retained Sessions, replayed requests and continued work."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.core.chat.chat_loop_support import StubAdapter
from tests.resources.extensions.swarm.swarm_test_support import (
    SWARM_COORDINATION_TIMEOUT_SECONDS,
    PausedSwarmAdapter,
    single_participant_profile,
    wait_idle,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("reminder_enabled", [True, False])
async def test_stop_resume_preserves_one_initial_input_for_unfinished_participant(
    lifecycle, tmp_path, monkeypatch, reminder_enabled
):
    """A stopped waiting participant resumes from its Session without replaying the initial goal."""
    from resources.extensions.swarm.agent_text import DEFAULT_REMINDERS, REMINDER_TEXTS

    monkeypatch.setitem(REMINDER_TEXTS, "resume", "resume-guidance-sentinel")

    lifecycle.runtime.adapter = StubAdapter(
        [
            {"content": "Run finished"},
            {"content": "Run finished"},
            {"content": "completion recorded"},
        ]
    )
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "resume",
            "reminders": {**DEFAULT_REMINDERS, "resume": reminder_enabled},
            "name": "Resume participant",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start",
        {"profile_id": profile["id"], "prompt": "shared goal", "request_id": "initial"},
    )
    await lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"]).wait()
    await lifecycle.service.operation(
        "swarms.stop", {"swarm_id": started["swarm_id"], "request_id": "stop"}
    )
    assert (await lifecycle.service.store.get_swarm(started["swarm_id"]))["state"] == "cancelled"

    resumed = await lifecycle.service.operation(
        "swarms.resume", {"swarm_id": started["swarm_id"], "request_id": "resume"}
    )
    resumed_run_id = resumed["runs"][0]["run_id"]
    await lifecycle.runtime.chat_run_manager.get(resumed_run_id).wait()
    binding = (await lifecycle.groups.list(started["swarm_id"]))[0]
    history = lifecycle.runtime.chat_sessions.get(binding.address).load()
    inputs = [message.content for message in history if message.role == "user"]
    assert len(inputs) == 1 and '"message_id": "#0"' in inputs[0]
    assert "shared goal" not in inputs[0]
    resumed_messages = lifecycle.runtime.adapter.requests[-1]["messages"]
    assert (
        any(
            "resume-guidance-sentinel" in str(message.get("content", ""))
            for message in resumed_messages
        )
        is reminder_enabled
    )
    assert all(
        message.get("content") != "<system-reminder>\n\n</system-reminder>"
        for message in resumed_messages
    )
    assert (
        await lifecycle.groups.owned_run(started["swarm_id"], resumed_run_id)
    ).record.terminal_status == ("completed")


class SlowClosingAdapter(StubAdapter):
    """Keeps a successful Run active while the Provider connection closes."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(responses)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_started.set()
        await self.release_close.wait()


@pytest.mark.asyncio
async def test_run_completion_never_closes_the_swarm(lifecycle, tmp_path, monkeypatch):
    """A completed Run leaves its execution group open, including after slow cleanup."""

    adapter = SlowClosingAdapter(
        [
            {"content": "Run finished"},
            {"content": "completion recorded"},
        ]
    )
    lifecycle.runtime.adapter = adapter
    close_calls: list[str] = []
    original_close_group = lifecycle.groups.close_group

    async def close_group(group_id: str, reason: str = "extension"):
        close_calls.append(group_id)
        return await original_close_group(group_id, reason)

    monkeypatch.setattr(lifecycle.groups, "close_group", close_group)
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "slow-close",
            "name": "Slow close",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start",
        {"profile_id": profile["id"], "prompt": "shared goal", "request_id": "initial"},
    )
    run = lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"])
    await adapter.close_started.wait()
    for _ in range(10):
        await asyncio.sleep(0)
    assert close_calls == []
    assert run.status.value == "running"
    adapter.release_close.set()
    await run.wait()

    assert (await wait_idle(lifecycle.service, started["swarm_id"]))["state"] == "idle"
    assert close_calls == []


@pytest.mark.asyncio
async def test_old_stop_retry_preserves_a_new_resume(lifecycle, tmp_path):
    adapter = PausedSwarmAdapter()
    lifecycle.runtime.adapter = adapter
    profile = await single_participant_profile(lifecycle, tmp_path)
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    await lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"]).wait()
    stop = {"swarm_id": started["swarm_id"], "request_id": "old-stop"}
    stopped = await lifecycle.service.operation("swarms.stop", stop)
    resumed = await lifecycle.service.operation(
        "swarms.resume", {"swarm_id": started["swarm_id"], "request_id": "new-resume"}
    )
    # Wait for the ordering barrier.
    await asyncio.wait_for(adapter.started.wait(), timeout=SWARM_COORDINATION_TIMEOUT_SECONDS)
    run = lifecycle.runtime.chat_run_manager.get(resumed["runs"][0]["run_id"])
    replay = await lifecycle.service.operation("swarms.stop", stop)
    assert replay == {**stopped, "replayed": True}
    assert run.status.value == "running"
    replay = await lifecycle.service.operation(
        "swarms.resume", {"swarm_id": started["swarm_id"], "request_id": "new-resume"}
    )
    assert replay == {**resumed, "replayed": True}
    assert run.status.value == "running"
    adapter.pause_at = 3
    adapter.started.clear()
    await lifecycle.service.operation(
        "swarms.stop", {"swarm_id": started["swarm_id"], "request_id": "second-stop"}
    )
    latest = await lifecycle.service.operation(
        "swarms.resume", {"swarm_id": started["swarm_id"], "request_id": "latest-resume"}
    )
    await asyncio.wait_for(adapter.started.wait(), timeout=SWARM_COORDINATION_TIMEOUT_SECONDS)
    current = lifecycle.runtime.chat_run_manager.get(latest["runs"][0]["run_id"])
    for operation, arguments, original in [
        ("swarms.stop", stop, stopped),
        (
            "swarms.resume",
            {"swarm_id": started["swarm_id"], "request_id": "new-resume"},
            resumed,
        ),
        (
            "swarms.start",
            {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"},
            started,
        ),
    ]:
        replay = await lifecycle.service.operation(operation, arguments)
        assert replay == {**original, "replayed": True}
        assert current.status.value == "running"
    assert (
        len(
            lifecycle.runtime.chat_sessions.owned_runs(
                owner_name="swarm", group_id=started["swarm_id"]
            )
        )
        == 3
    )


@pytest.mark.asyncio
async def test_stop_does_not_report_expected_late_completion_as_failure(
    lifecycle, tmp_path, caplog
):
    adapter = PausedSwarmAdapter(pause_at=1)
    lifecycle.runtime.adapter = adapter
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "stop-feedback",
            "name": "Stop feedback",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    run = lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"])
    async with asyncio.timeout(SWARM_COORDINATION_TIMEOUT_SECONDS):
        while not adapter.requests:
            await asyncio.sleep(0.01)
    await lifecycle.service.operation(
        "swarms.stop", {"swarm_id": started["swarm_id"], "request_id": "stop"}
    )
    assert run.status.value == "cancelled"
    assert not [record for record in caplog.records if record.exc_info]


@pytest.mark.asyncio
async def test_participant_inspection_failure_is_not_reported_as_idle(
    lifecycle, tmp_path, monkeypatch
):
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "inspect-feedback",
            "name": "Inspect feedback",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    await lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"]).wait()

    async def broken(*_args):
        raise RuntimeError("inspection sentinel")

    monkeypatch.setattr(lifecycle.groups, "owned_runs", broken)
    with pytest.raises(RuntimeError):
        await lifecycle.service.operation("swarms.get", {"swarm_id": started["swarm_id"]})


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "during_stop"), [("all", False), ("pull", False), ("idle", True)])
async def test_human_post_after_stop_continues_same_session_once(
    lifecycle, tmp_path, monkeypatch, during_stop, mode
):
    adapter = PausedSwarmAdapter(pause_at=1)
    lifecycle.runtime.adapter = adapter
    profile = await single_participant_profile(lifecycle, tmp_path, mode=mode)
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    sid = started["swarm_id"]
    await asyncio.wait_for(adapter.started.wait(), timeout=SWARM_COORDINATION_TIMEOUT_SECONDS)
    before = (await lifecycle.groups.list(sid))[0]
    draining = asyncio.Event()
    release = asyncio.Event()
    close = lifecycle.groups.close_group

    async def delayed_close(*args, **kwargs):
        draining.set()
        await release.wait()
        return await close(*args, **kwargs)

    if during_stop:
        monkeypatch.setattr(lifecycle.groups, "close_group", delayed_close)
    stop = asyncio.create_task(
        lifecycle.service.operation("swarms.stop", {"swarm_id": sid, "request_id": "stop"})
    )
    if during_stop:
        await asyncio.wait_for(draining.wait(), timeout=SWARM_COORDINATION_TIMEOUT_SECONDS)
    else:
        await stop
    arguments = {"swarm_id": sid, "text": "continue-sentinel", "request_id": "post"}
    post = asyncio.create_task(lifecycle.service.operation("board.post", arguments))
    if during_stop:
        await asyncio.sleep(0)
        assert not post.done()
        release.set()
    await stop
    result = await post
    assert len(result["runs"]) == 1
    await lifecycle.runtime.chat_run_manager.get(result["runs"][0]["run_id"]).wait()
    after = (await lifecycle.groups.list(sid))[0]
    assert after.address == before.address and after.generation_id == before.generation_id
    assert "continue-sentinel" in str(adapter.requests[1]["messages"])
    history = lifecycle.runtime.chat_sessions.get(after.address).load()
    assert len([message for message in history if message.role == "user"]) == 1
    await lifecycle.service.operation("swarms.stop", {"swarm_id": sid, "request_id": "stop-again"})
    replay = await lifecycle.service.operation("board.post", arguments)
    assert replay["post_id"] == result["post_id"] and replay["replayed"]
    snapshot = await lifecycle.service.store.get_swarm(sid)
    assert snapshot["state"] == "cancelled"
    assert len(lifecycle.runtime.chat_sessions.owned_runs(owner_name="swarm", group_id=sid)) == 2


@pytest.mark.asyncio
async def test_resume_records_a_participant_session_whose_binding_start_lost(
    lifecycle, tmp_path, monkeypatch
):
    """A participant Session a failed or interrupted Start created is bound again on Resume."""
    profile = await single_participant_profile(lifecycle, tmp_path)
    store = lifecycle.service.store

    async def lost_binding(binding: Any) -> None:
        raise OSError("the binding was not recorded")

    with monkeypatch.context() as patch:
        patch.setattr(store, "bind_participant_session", lost_binding)
        with pytest.raises(OSError):
            await lifecycle.service.operation(
                "swarms.start",
                {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"},
            )
    swarm_id = (await store.list_swarms()).entries[0]["id"]
    assert len(await lifecycle.groups.list(swarm_id)) == 1

    resumed = await lifecycle.service.operation(
        "swarms.resume", {"swarm_id": swarm_id, "request_id": "resume"}
    )
    await lifecycle.runtime.chat_run_manager.get(resumed["runs"][0]["run_id"]).wait()
    await lifecycle.service.operation("swarms.stop", {"swarm_id": swarm_id, "request_id": "stop"})
    posted = await lifecycle.service.operation(
        "board.post", {"swarm_id": swarm_id, "text": "continue-sentinel", "request_id": "post"}
    )
    await lifecycle.runtime.chat_run_manager.get(posted["runs"][0]["run_id"]).wait()
    assert "continue-sentinel" in str(lifecycle.runtime.adapter.requests[-1]["messages"])
