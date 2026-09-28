"""Swarm wakes: Board posts reach idle and running participants through live Runs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from resources.extensions.swarm import _wake_pacing
from tests.core.chat.chat_loop_support import StubAdapter
from tests.resources.extensions.swarm.swarm_test_support import (
    SWARM_COORDINATION_TIMEOUT_SECONDS,
    PausedSwarmAdapter,
    QuietTimers,
    Received,
    delivery_request,
    participant_post,
    received,
    settled,
    single_participant_profile,
    wait_idle,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_first_run", [False, True])
async def test_busy_burst_reaches_next_request_without_duplicate_wakes(
    lifecycle, tmp_path, finish_first_run
):
    class BarrierAdapter(StubAdapter):
        def __init__(self):
            first = (
                {"content": "An ordinary final response"}
                if finish_first_run
                else {"tool_calls": [{"id": "status", "name": "swarm_state", "arguments": {}}]}
            )
            super().__init__(
                [
                    first,
                    {"content": "Run finished"},
                ]
            )
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.continued = asyncio.Event()

        async def send(self, messages, *, model_id, **kwargs):
            first = not self.requests
            response = await super().send(messages, model_id=model_id, **kwargs)
            if first:
                self.started.set()
                await self.release.wait()
            else:
                self.continued.set()
            return response

    adapter = BarrierAdapter()
    lifecycle.runtime.adapter = adapter
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "burst",
            "name": "Burst",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
            # Without a coalescing delay every post scans for wakes on its own.
            "delivery": {"coalesce_ms": 0},
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    async with asyncio.timeout(SWARM_COORDINATION_TIMEOUT_SECONDS):
        await adapter.started.wait()
        await asyncio.gather(
            *(
                lifecycle.service.operation(
                    "board.post",
                    {
                        "swarm_id": started["swarm_id"],
                        "text": f"burst-sentinel-{index}",
                        "request_id": f"post-{index}",
                    },
                )
                for index in range(15)
            )
        )
        assert len(adapter.requests) == 1
        adapter.release.set()
        await adapter.continued.wait()
        snapshot = await lifecycle.service.store.get_swarm(started["swarm_id"])
        participant = snapshot["participants"][0]
        await lifecycle.runtime.chat_run_manager.get(participant["lifecycle_run_id"]).wait()
    await settled()
    assert len(adapter.requests) == 2
    assert all(
        f"burst-sentinel-{index}" in str(adapter.requests[1]["messages"]) for index in range(15)
    )
    delivered = delivered_messages(adapter.requests[1], "burst-sentinel-")
    snapshot = await lifecycle.service.store.get_swarm(started["swarm_id"])
    page = await lifecycle.service.store.read_posts(
        started["swarm_id"], snapshot["participants"][0]["id"], limit=15
    )
    assert delivered == [
        Received("the main discussion (d1)", f"#{post['sequence']}", "User", None, post["text"])
        for post in page.entries
    ]
    assert sorted(message.text for message in delivered) == sorted(
        f"burst-sentinel-{index}" for index in range(15)
    )

    assert snapshot["state"] == "idle"
    inbox = await lifecycle.service.store.prepare_inbox_delivery(
        started["swarm_id"], participant["id"]
    )
    assert inbox["entries"] == []
    owned = lifecycle.runtime.chat_sessions.owned_runs(
        owner_name="swarm", group_id=started["swarm_id"]
    )
    assert len(owned) == (2 if finish_first_run else 1)


@pytest.mark.asyncio
async def test_automatic_delivery_updates_pending_during_each_running_iteration(
    lifecycle, tmp_path
):
    class SteppedAdapter(StubAdapter):
        def __init__(self):
            super().__init__(
                [
                    {"tool_calls": [{"id": f"state-{i}", "name": "swarm_state", "arguments": {}}]}
                    for i in range(3)
                ]
                + [{"content": "finished"}]
            )
            self.started = [asyncio.Event() for _ in range(4)]
            self.release = [asyncio.Event() for _ in range(4)]

        async def send(self, messages, *, model_id, **kwargs):
            response = await super().send(messages, model_id=model_id, **kwargs)
            index = len(self.requests) - 1
            self.started[index].set()
            await self.release[index].wait()
            return response

    adapter = SteppedAdapter()
    lifecycle.runtime.adapter = adapter
    changes = []
    lifecycle.service.host = replace(
        lifecycle.service.host, publish_change=lambda *args: changes.append(args)
    )
    profile = await single_participant_profile(lifecycle, tmp_path)
    profile = await lifecycle.service.store.save_profile(
        {**profile, "delivery": {**profile["delivery"], "batch_messages": 4}},
        expected_revision=profile["revision"],
    )
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    sid = started["swarm_id"]
    run = lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"])
    async with asyncio.timeout(SWARM_COORDINATION_TIMEOUT_SECONDS):
        await adapter.started[0].wait()
        for index in range(12):
            await lifecycle.service.operation(
                "board.post",
                {
                    "swarm_id": sid,
                    "text": f"pending-sentinel-{index}",
                    "request_id": f"post-{index}",
                },
            )
        assert (await lifecycle.service.store.get_swarm(sid))["participants"][0][
            "pending_count"
        ] == 12
        for index, expected in enumerate((8, 4, 0), start=1):
            changes.clear()
            adapter.release[index - 1].set()
            await adapter.started[index].wait()
            snapshot = await lifecycle.service.store.get_swarm(sid)
            assert run.status.value == "running"
            assert run.iteration_count == index
            assert snapshot["participants"][0]["pending_count"] == expected
            # Pending messages show on the participants, not in the Swarm list.
            assert {change[0] for change in changes} == {"participants"}
        adapter.release[-1].set()
        await run.wait()
    delivered = delivered_messages(adapter.requests[-1], "pending-sentinel-")
    assert [message.text for message in delivered] == [
        f"pending-sentinel-{index}" for index in range(12)
    ]
    assert len({message.post_id for message in delivered}) == 12
    assert run.status.value == "completed"


def delivered_messages(request, marker):
    """Return the Board messages that automatic delivery placed in one Provider request."""

    messages = []
    for message in request["messages"]:
        content = message.get("content", "")
        if isinstance(content, str) and marker in content:
            messages.extend(received(content.replace("</system-reminder>", "").strip()))
    return messages


@pytest.mark.asyncio
async def test_wake_failure_retains_pending_and_reports_attention(
    lifecycle, tmp_path, monkeypatch, caplog
):
    lifecycle.runtime.adapter = StubAdapter([{"content": "Run finished"}])
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "failed-wake",
            "name": "Failed wake",
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

    async def fail_admission(*_args, **_kwargs):
        raise RuntimeError("private-exception-content-sentinel")

    monkeypatch.setattr(lifecycle.groups, "start", fail_admission)
    await lifecycle.service.operation(
        "board.post",
        {"swarm_id": started["swarm_id"], "text": "pending-sentinel", "request_id": "post"},
    )
    async with asyncio.timeout(SWARM_COORDINATION_TIMEOUT_SECONDS):
        while True:
            snapshot = await lifecycle.service.store.get_swarm(started["swarm_id"])
            if snapshot["state"] == "needs_attention":
                break
            await asyncio.sleep(0.01)
    pending = await lifecycle.service.store.prepare_inbox_delivery(
        started["swarm_id"], snapshot["participants"][0]["id"]
    )
    assert [entry["text"] for entry in pending["entries"]] == ["pending-sentinel"]
    assert len(lifecycle.runtime.adapter.requests) == 1
    assert started["swarm_id"] in caplog.text
    assert "private-exception-content-sentinel" not in caplog.text
    assert "pending-sentinel" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "wake", "reminders_enabled"),
    [("all", True, True), ("pull", True, False), ("idle", False, True)],
)
async def test_human_post_wakes_idle_participant_with_delivery_policy(
    lifecycle, tmp_path, mode, wake, reminders_enabled, monkeypatch
):
    """Every wake carries the post without an Inbox call, even with guidance disabled."""
    from resources.extensions.swarm.agent_text import DEFAULT_REMINDERS, REMINDER_TEXTS

    monkeypatch.setitem(REMINDER_TEXTS, "delivery", "delivery-guidance-sentinel")

    lifecycle.runtime.adapter = StubAdapter(
        [
            {"content": "Run finished"},
            {"content": "Run finished"},
        ]
    )
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": f"wake-{mode}-{int(wake)}",
            "name": "Wake",
            "reminders": dict.fromkeys(DEFAULT_REMINDERS, reminders_enabled),
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
    swarm = await lifecycle.service.store.get_swarm(started["swarm_id"])
    delivery = {**swarm["delivery"], "coalesce_ms": 0, "main": {"mode": mode, "wake_idle": wake}}
    await lifecycle.service.operation(
        "swarms.settings",
        {
            "swarm_id": started["swarm_id"],
            "delivery": delivery,
            "expected_revision": swarm["settings_revision"],
            "request_id": "settings",
        },
    )
    await lifecycle.service.operation(
        "board.post",
        {"swarm_id": started["swarm_id"], "text": "wake message", "request_id": "post"},
    )
    await settled()
    participant = (await lifecycle.service.store.get_swarm(started["swarm_id"]))["participants"][0][
        "id"
    ]
    pending = await lifecycle.service.store.prepare_inbox_delivery(started["swarm_id"], participant)
    if not wake:
        assert len(lifecycle.runtime.adapter.requests) == 1
        assert len(pending["entries"]) == 1
    else:
        request = str(lifecycle.runtime.adapter.requests[1]["messages"])
        assert ("delivery-guidance-sentinel" in request) is reminders_enabled
        assert "wake message" in request
        assert pending["entries"] == []


@pytest.mark.asyncio
async def test_automatic_wake_publishes_running_state(lifecycle, tmp_path):
    adapter = PausedSwarmAdapter()
    lifecycle.runtime.adapter = adapter
    changes = []
    lifecycle.service.host = replace(
        lifecycle.service.host, publish_change=lambda *args: changes.append(args)
    )
    profile = await single_participant_profile(lifecycle, tmp_path)
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    await lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"]).wait()
    await lifecycle.service.operation(
        "board.post", {"swarm_id": started["swarm_id"], "text": "wake", "request_id": "post"}
    )
    changes.clear()
    await asyncio.wait_for(adapter.started.wait(), timeout=SWARM_COORDINATION_TIMEOUT_SECONDS)
    snapshot = await lifecycle.service.store.get_swarm(started["swarm_id"])
    assert snapshot["participants"][0]["state"] == "running"
    # The woken participant runs, and so the Swarm in the Swarm list.
    expected = {("participants", started["swarm_id"]), ("swarms", started["swarm_id"])}
    async with asyncio.timeout(SWARM_COORDINATION_TIMEOUT_SECONDS):
        while not expected <= {(resource, *ids) for resource, ids, _revision in changes}:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_run_callbacks_publish_the_swarm_list_only_when_the_swarm_state_changes(board):
    sid = board.swarm["id"]
    await board.store.set_swarm_state(sid, "idle")
    changes = []
    board.service.host = replace(
        board.service.host, publish_change=lambda *args: changes.append(args)
    )

    def published():
        result = [(resource, *ids) for resource, ids, _revision in changes]
        changes.clear()
        return result

    first, second = (delivery_request(board, peer, run_id=f"run-{peer}") for peer in (0, 1))
    await board.runtime.before_request(first)
    assert published() == [("participants", sid), ("swarms", sid)]
    await board.runtime.before_request(second)
    await board.runtime.before_request(second)
    assert published() == [("participants", sid)]
    await board.runtime.run_finished(first, outcome="success")
    assert published() == [("participants", sid)]
    await board.runtime.run_finished(second, outcome="error")
    assert published() == [("participants", sid), ("swarms", sid)]
    assert (await board.store.get_swarm(sid))["state"] == "needs_attention"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["all", "idle", "pull"])
async def test_delivery_mode_after_wake(lifecycle, tmp_path, mode):
    adapter = PausedSwarmAdapter()
    lifecycle.runtime.adapter = adapter
    profile = await single_participant_profile(lifecycle, tmp_path, mode=mode)
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    await lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"]).wait()
    await lifecycle.service.operation(
        "board.post",
        {"swarm_id": started["swarm_id"], "text": "first-wake-sentinel", "request_id": "post-1"},
    )
    await asyncio.wait_for(adapter.started.wait(), timeout=SWARM_COORDINATION_TIMEOUT_SECONDS)
    assert "first-wake-sentinel" in str(adapter.requests[1]["messages"])
    await lifecycle.service.operation(
        "board.post",
        {"swarm_id": started["swarm_id"], "text": "deferred-sentinel", "request_id": "post-2"},
    )
    adapter.release.set()
    await asyncio.wait_for(adapter.followed.wait(), timeout=SWARM_COORDINATION_TIMEOUT_SECONDS)
    assert ("deferred-sentinel" in str(adapter.requests[2]["messages"])) is (mode == "all")
    await wait_idle(lifecycle.service, started["swarm_id"])
    async with asyncio.timeout(SWARM_COORDINATION_TIMEOUT_SECONDS):
        while mode != "all" and len(adapter.requests) < 4:
            await asyncio.sleep(0.01)
    if mode != "all":
        assert "deferred-sentinel" in str(adapter.requests[3]["messages"])


@pytest.mark.asyncio
async def test_runs_without_tools_pace_wakes_until_addressed_or_quiet_ends(
    lifecycle, tmp_path, monkeypatch
):
    quiet = QuietTimers()
    monkeypatch.setattr(_wake_pacing, "asyncio", SimpleNamespace(get_running_loop=lambda: quiet))
    lifecycle.runtime.adapter = StubAdapter(
        [
            {"content": "Run finished"},
            {"content": "Run finished"},
            {"content": "Nothing to add"},
            {"tool_calls": [{"id": "look", "name": "swarm_state", "arguments": {}}]},
            {"content": "Checked"},
            {
                "tool_calls": [
                    {
                        "id": "note",
                        "name": "swarm_wiki",
                        "arguments": {"action": "create", "title": "Notes", "content": "Parser"},
                    }
                ]
            },
            {"content": "Written"},
            {"content": "Seen"},
        ]
    )
    requests = lifecycle.runtime.adapter.requests
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "name": "Paced",
            "participants": [{"model": "fixture/model", "count": 2}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
            "delivery": {"coalesce_ms": 0},
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    sid = started["swarm_id"]
    sender, reader = (await lifecycle.service.store.get_swarm(sid))["participants"]

    async def post(text: str) -> int:
        await participant_post(lifecycle, sid, sender["id"], text)
        await settled()
        return len(requests)

    # Both first Runs used no Tool, so an ordinary post waits for the quiet period, and its
    # end wakes the reader.
    await settled()
    assert [timer.delay for timer in quiet.timers] == [30.0, 30.0]
    assert await post("ordinary-sentinel") == 2
    next(timer for timer in quiet.timers if timer.args[0] == (sid, reader["id"])).fire()
    await settled()
    assert len(requests) == 3 and "ordinary-sentinel" in str(requests[2]["messages"])

    # The second Run without a Tool starts a longer period; a post addressing the reader ends
    # the wait, and a name without "@" does not.
    assert quiet.timers[-1].delay == 60.0
    assert await post(f"{reader['display_name']} mentioned-sentinel") == 3
    assert await post(f"@{reader['display_name']} addressed-sentinel") == 5
    assert "addressed-sentinel" in str(requests[3]["messages"])

    # That Run only read its status, so an ordinary post still waits.
    assert quiet.timers[-1].delay == 120.0 and not quiet.timers[-1].cancelled
    assert await post("after-read-sentinel") == 5
    assert await post(f"@{reader['display_name']} write-sentinel") == 7
    assert "write-sentinel" in str(requests[5]["messages"])

    # That Run changed the Wiki, so the next ordinary post wakes the reader at once.
    assert quiet.timers[-1].cancelled
    assert await post("after-tool-sentinel") == 8
    assert "after-tool-sentinel" in str(requests[7]["messages"])
