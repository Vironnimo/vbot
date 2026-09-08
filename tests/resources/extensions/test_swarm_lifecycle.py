"""Production Swarm lifecycle acceptance through the protected Chat path."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio

from core.agents.temporary import TemporaryAgentRegistry, TemporaryExecutionGroups
from core.chat import CommandDispatcher, CommandExecutionContext, ReplySurface
from core.extensions import ExtensionAPI, ExtensionRecord, ExtensionRegistry
from core.extensions.extensions import ExtensionDeclarations
from core.extensions.operations import ExtensionHost
from core.tools import ToolRegistry
from resources.extensions.swarm.extension import register
from tests.core.chat.chat_loop_support import StubAdapter, StubAgent, StubRuntime, build_chat_loop


class SlowClosingAdapter(StubAdapter):
    """Keeps a successful Run active while the Provider connection closes."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(responses)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def aclose(self) -> None:
        self.close_started.set()
        await self.release_close.wait()


async def wait_completed(service: Any, swarm_id: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            snapshot = await service.store.get_swarm(swarm_id)
            if snapshot["state"] == "completed":
                return cast(dict[str, Any], snapshot)
            await asyncio.sleep(0.01)


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
                else {
                    "tool_calls": [
                        {"id": "status", "name": "swarm_state", "arguments": {"action": "status"}}
                    ]
                }
            )
            super().__init__(
                [
                    first,
                    {
                        "tool_calls": [
                            {"id": "wait", "name": "swarm_state", "arguments": {"action": "wait"}}
                        ]
                    },
                ]
            )
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def send(self, messages, *, model_id, **kwargs):
            first = not self.requests
            response = await super().send(messages, model_id=model_id, **kwargs)
            if first:
                self.started.set()
                await self.release.wait()
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
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start", {"profile_id": profile["id"], "prompt": "goal", "request_id": "start"}
    )
    async with asyncio.timeout(5):
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
        while len(adapter.requests) < 2:
            await asyncio.sleep(0.01)
        snapshot = await lifecycle.service.store.get_swarm(started["swarm_id"])
        participant = snapshot["participants"][0]
        await lifecycle.runtime.chat_run_manager.get(participant["lifecycle_run_id"]).wait()
    await asyncio.sleep(0.35)
    assert len(adapter.requests) == 2
    assert all(
        f"burst-sentinel-{index}" in str(adapter.requests[1]["messages"]) for index in range(15)
    )
    snapshot = await lifecycle.service.store.get_swarm(started["swarm_id"])
    assert snapshot["state"] == "waiting"
    inbox = await lifecycle.service.store.prepare_inbox_delivery(
        started["swarm_id"], participant["id"]
    )
    assert inbox["entries"] == []
    owned = lifecycle.runtime.chat_sessions.owned_runs(
        owner_name="swarm", group_id=started["swarm_id"]
    )
    assert len(owned) == (2 if finish_first_run else 1)


@pytest.mark.asyncio
async def test_wake_failure_retains_pending_and_reports_attention(
    lifecycle, tmp_path, monkeypatch, caplog
):
    lifecycle.runtime.adapter._responses[:] = [  # noqa: SLF001 - deterministic Provider fixture
        {"tool_calls": [{"id": "wait", "name": "swarm_state", "arguments": {"action": "wait"}}]}
    ]
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
    async with asyncio.timeout(5):
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


@pytest_asyncio.fixture
async def lifecycle(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    """Load the production Extension into a real ChatLoop with 40 private Sessions."""

    responses = [
        {
            "content": None,
            "tool_calls": [
                {
                    "id": f"done-{index}",
                    "name": "swarm_state",
                    "arguments": {"action": "done", "summary": f"participant {index} done"},
                }
            ],
        }
        for index in range(40)
    ] + [{"content": "completion recorded"} for _ in range(40)]
    tools = ToolRegistry()
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="ordinary", model="fixture/model", allowed_tools=["*"]),
        adapter=StubAdapter(responses),
        tools=tools,
    )
    declarations = ExtensionDeclarations()
    api = ExtensionAPI(
        "swarm", declarations, config={}, logger=logging.getLogger("vbot.test.swarm")
    )
    register(api)
    extensions = ExtensionRegistry()
    extensions._records.append(  # noqa: SLF001 - production declaration fixture
        ExtensionRecord(
            "swarm", tmp_path, tmp_path / "extension.py", "loaded", declarations=declarations
        )
    )
    extensions.apply_tools(tools)
    runtime.extensions = extensions
    temporary = TemporaryAgentRegistry(runtime.chat_sessions)
    runtime.agent_resolver.temporary_agents = temporary
    identity = extensions.registration_identity("swarm")
    groups = TemporaryExecutionGroups(
        temporary,
        build_chat_loop(runtime),
        extensions.is_registration_current,
        identity,
        run_manager=runtime.chat_run_manager,
    )

    async def catalog() -> dict[str, Any]:
        return {"models": [{"id": "fixture/model"}], "tools": [], "skills": []}

    host = ExtensionHost(
        data_dir=tmp_path,
        sample=None,  # type: ignore[arg-type]
        resolve_agent=None,  # type: ignore[arg-type]
        store_attachment=None,  # type: ignore[arg-type]
        resolve_credential=None,  # type: ignore[arg-type]
        set_credential=None,  # type: ignore[arg-type]
        state_dir=tmp_path,
        temporary_agents=groups,
        catalog=catalog,
    )
    await api.operations.startup[0](host)
    service = cast(Any, declarations.tools[0].handler).__self__
    try:
        yield SimpleNamespace(service=service, runtime=runtime, groups=groups, tools=tools)
    finally:
        await service.close()
        await runtime.chat_run_manager.aclose()
        runtime.chat_sessions.close()


@pytest.mark.asyncio
async def test_forty_participants_complete_through_production_state_tool(lifecycle, tmp_path: Path):
    """Each participant completes through State, records its owned Run, then closes once."""

    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "forty",
            "name": "Forty participants",
            "participants": [{"model": "fixture/model", "count": 40}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )

    started = await lifecycle.service.operation(
        "swarms.start",
        {"profile_id": profile["id"], "prompt": "shared goal", "request_id": "initial"},
    )
    run_ids = [entry["run_id"] for entry in started["runs"]]
    assert len(run_ids) == 40
    await asyncio.gather(
        *(lifecycle.runtime.chat_run_manager.get(run_id).wait() for run_id in run_ids)
    )

    snapshot = await wait_completed(lifecycle.service, started["swarm_id"])
    assert {item["state"] for item in snapshot["participants"]} == {"done"}
    assert {tool["name"] for tool in lifecycle.runtime.adapter.requests[0]["kwargs"]["tools"]} == {
        "swarm_board",
        "swarm_inbox",
        "swarm_state",
    }
    for run_id in run_ids:
        assert (
            await lifecycle.groups.owned_run(started["swarm_id"], run_id)
        ).record.terminal_status == ("completed")


@pytest.mark.asyncio
async def test_stop_resume_preserves_one_initial_input_for_unfinished_participant(
    lifecycle, tmp_path
):
    """A stopped waiting participant resumes from its Session without replaying the initial goal."""

    lifecycle.runtime.adapter._responses[:] = [  # noqa: SLF001 - deterministic Provider fixture
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "wait",
                    "name": "swarm_state",
                    "arguments": {"action": "wait", "reason": "need input"},
                }
            ],
        },
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "done",
                    "name": "swarm_state",
                    "arguments": {"action": "done", "summary": "resumed work done"},
                }
            ],
        },
        {"content": "completion recorded"},
    ]
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "resume",
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
    assert [message.content for message in history if message.role == "user"] == ["shared goal"]
    assert (
        await lifecycle.groups.owned_run(started["swarm_id"], resumed_run_id)
    ).record.terminal_status == ("completed")


@pytest.mark.asyncio
async def test_completion_waits_for_slow_adapter_close_before_draining_group(
    lifecycle, tmp_path, monkeypatch
):
    """Group completion cannot consume its still-finalizing owned Run as a drain report."""

    adapter = SlowClosingAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "done",
                        "name": "swarm_state",
                        "arguments": {"action": "done", "summary": "done"},
                    }
                ],
            },
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

    assert (await wait_completed(lifecycle.service, started["swarm_id"]))["state"] == "completed"


@pytest.mark.asyncio
async def test_swarm_command_preserves_quoted_unicode_prompt_and_starts_distinct_swarms(
    lifecycle, tmp_path
):
    """The registered immediate Command shares start semantics without idempotent user intent."""

    await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "command",
            "name": "Command profile",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    dispatcher = CommandDispatcher(lifecycle.runtime.chat_run_manager)
    lifecycle.runtime.extensions.apply_commands(dispatcher)
    quoted = dispatcher.prepare('/swarm command "研究 \\"quoted\\" goal"')
    unquoted = dispatcher.prepare("/swarm command plain Unicode 研究")
    assert quoted is not None and unquoted is not None
    context = CommandExecutionContext(
        agent_id="ordinary",
        session_id="command-session",
        project_id=None,
        reply_surface=ReplySurface.webui(),
    )
    first = await dispatcher.execute(quoted, context)
    second = await dispatcher.execute(unquoted, context)
    assert first.facts is not None and second.facts is not None
    assert first.facts["swarm_id"] != second.facts["swarm_id"]
    assert (await lifecycle.service.store.get_swarm(first.facts["swarm_id"]))[
        "prompt"
    ] == '研究 "quoted" goal'
    assert (await lifecycle.service.store.get_swarm(second.facts["swarm_id"]))[
        "prompt"
    ] == "plain Unicode 研究"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "wake", "expected_request"),
    [
        ("all", True, "wake message"),
        ("idle", True, "wake message"),
        ("pull", True, "metadata"),
        ("all", False, None),
        ("idle", False, None),
        ("pull", False, None),
    ],
)
async def test_human_post_wakes_waiting_participant_with_delivery_policy(
    lifecycle, tmp_path, mode, wake, expected_request
):
    """A durable post reaches the request, wakes with metadata, or remains idle."""

    lifecycle.runtime.adapter._responses[:] = [  # noqa: SLF001 - deterministic Provider fixture
        {
            "content": None,
            "tool_calls": [{"id": "wait", "name": "swarm_state", "arguments": {"action": "wait"}}],
        },
        {
            "content": None,
            "tool_calls": [
                {"id": "wait-again", "name": "swarm_state", "arguments": {"action": "wait"}}
            ],
        },
    ]
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": f"wake-{mode}-{int(wake)}",
            "name": "Wake",
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
    delivery = {**swarm["delivery"], "main": {"mode": mode, "wake_idle": wake}}
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
    if wake:
        # This guards a stuck wake, not a latency SLA. Parallel canonical SQLite
        # fixtures may briefly occupy the worker pool on Windows.
        async with asyncio.timeout(15):
            while len(lifecycle.runtime.adapter.requests) < 2:
                await asyncio.sleep(0.01)
    else:
        await asyncio.sleep(0.4)
    participant = (await lifecycle.service.store.get_swarm(started["swarm_id"]))["participants"][0][
        "id"
    ]
    pending = await lifecycle.service.store.prepare_inbox_delivery(started["swarm_id"], participant)
    if expected_request is None:
        assert len(lifecycle.runtime.adapter.requests) == 1
        assert len(pending["entries"]) == 1
    else:
        request = str(lifecycle.runtime.adapter.requests[1]["messages"])
        if mode == "pull":
            assert "wake message" not in request
            assert len(pending["entries"]) == 1
        else:
            assert expected_request in request
            assert pending["entries"] == []
