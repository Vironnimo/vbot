"""MCP: a Tool call that a 2026-07-28 server runs as a task (Tasks extension, SEP-2663)."""

from __future__ import annotations

import asyncio
from typing import Any

import mcp.types as types
import pytest
from mcp.server import Server

from resources.extensions.mcp import _tasks
from resources.extensions.mcp._tasks import TASKS_EXTENSION, TaskEndedError
from tests.resources.extensions.mcp.mcp_test_support import context, runner_for

_TASK = "task-sentinel"
_STAMP = "2026-10-02T00:00:00Z"
_QUESTION = {
    "method": "elicitation/create",
    "params": {
        "message": "test-owned-question",
        "requestedSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
}


class _TaskParams(types.RequestParams):
    task_id: str


class _TaskUpdate(_TaskParams):
    input_responses: dict[str, Any]


class _TaskServer:
    """A 2026-07-28 server that answers its Tool with a task.

    Each ``tasks/get`` returns the next entry of *script* (the last one repeats);
    a cancelled task reports ``cancelled`` from then on.
    """

    def __init__(self, script: list[dict[str, Any]], **handle: Any) -> None:
        self.script = script
        self.handle = {"ttlMs": None, "pollIntervalMs": 250, **handle}
        self.gets = 0
        self.updates: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.server = Server("tasks", on_call_tool=self._call, on_list_tools=self._list)
        self.server.extensions[TASKS_EXTENSION] = {}
        self.server.add_request_handler("tasks/get", _TaskParams, self._get)
        self.server.add_request_handler("tasks/update", _TaskUpdate, self._update)
        self.server.add_request_handler("tasks/cancel", _TaskParams, self._cancel)

    def _task(self, status: str, **fields: Any) -> dict[str, Any]:
        return {
            "taskId": _TASK,
            "status": status,
            "createdAt": _STAMP,
            "lastUpdatedAt": _STAMP,
            **self.handle,
            **fields,
        }

    async def _list(self, server_context: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[types.Tool(name="long", input_schema={"type": "object"})]
        )

    async def _call(self, server_context: Any, params: Any) -> Any:
        return {"resultType": "task", **self._task("working")}

    async def _get(self, server_context: Any, params: _TaskParams) -> dict[str, Any]:
        assert params.task_id == _TASK
        if self.cancels:
            return self._task("cancelled")
        entry = self.script[min(self.gets, len(self.script) - 1)]
        self.gets += 1
        return self._task(**entry)

    async def _update(self, server_context: Any, params: _TaskUpdate) -> dict[str, Any]:
        self.updates.append(params.input_responses)
        return {}

    async def _cancel(self, server_context: Any, params: _TaskParams) -> dict[str, Any]:
        self.cancels.append(params.task_id)
        return {}


class _Clock:
    """The driver's clock and sleep: each sleep advances the clock at once."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(_tasks, "_clock", clock)
    monkeypatch.setattr(_tasks, "_sleep", clock.sleep)
    return clock


@pytest.mark.asyncio
async def test_a_task_backed_call_returns_its_result_after_answering_the_server(
    host, monkeypatch, clock
):
    tasks = _TaskServer(
        [
            {"status": "working", "pollIntervalMs": 600_000},
            {"status": "input_required", "pollIntervalMs": 5, "inputRequests": {"q": _QUESTION}},
            # Listed until the server processed the answer: answered once.
            {"status": "input_required", "pollIntervalMs": None, "inputRequests": {"q": _QUESTION}},
            {
                "status": "completed",
                "result": {
                    "content": [{"type": "text", "text": "payload-sentinel"}],
                    "structuredContent": {"nested": [1, 2, 3]},
                },
            },
        ]
    )
    runner = runner_for(host, tasks.server, monkeypatch)
    call = asyncio.create_task(runner.invoke("tools/call", {"name": "long"}, context(host)))
    try:
        async with asyncio.timeout(10):
            while not runner.inputs.list():
                if call.done():
                    await call
                await asyncio.sleep(0)
            pending = runner.inputs.list()[0]
            assert pending["payload"]["message"] == "test-owned-question"
            runner.inputs.respond(
                pending["id"], {"action": "accept", "content": {"name": "user-sentinel"}}
            )
            result = await call
            # An explicit status read reaches the extension's tasks/get.
            status = await runner.invoke("tasks/get", {"taskId": _TASK})
    finally:
        call.cancel()
        await runner.close()

    assert result["content"][0]["text"] == "payload-sentinel"
    assert result["structuredContent"] == {"nested": [1, 2, 3]}
    assert [update["q"]["content"] for update in tasks.updates] == [{"name": "user-sentinel"}]
    # The server's interval, held to 0.1..60 seconds, else one second.
    assert clock.sleeps == [0.25, 60.0, 0.1, 1.0]
    assert status["status"] == "completed"
    assert [
        event["payload"]["status"]
        for event in runner.events()["events"]
        if event["kind"] == "task_status"
    ] == ["working", "input_required", "completed"]
    assert tasks.cancels == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "handle", "message", "cancelled"),
    [
        (
            [{"status": "failed", "error": {"code": -32603, "message": "disk-full-sentinel"}}],
            {},
            "The MCP server reports that this call failed: disk-full-sentinel",
            False,
        ),
        (
            [{"status": "cancelled", "statusMessage": "operator-sentinel"}],
            {},
            "The MCP server cancelled this call: operator-sentinel",
            False,
        ),
        (
            [{"status": "working"}],
            {"ttlMs": 3000, "pollIntervalMs": 1000},
            "This call did not finish within the 3 seconds the MCP server keeps its task, so "
            "vBot cancelled it",
            True,
        ),
    ],
    ids=["failed", "cancelled-by-server", "expired"],
)
async def test_a_task_that_ends_without_a_result_reports_why(
    host, monkeypatch, clock, script, handle, message, cancelled
):
    tasks = _TaskServer(script, **handle)
    runner = runner_for(host, tasks.server, monkeypatch)
    try:
        with pytest.raises(TaskEndedError) as ended:
            await asyncio.wait_for(runner.invoke("tools/call", {"name": "long"}, context(host)), 10)
    finally:
        await runner.close()

    assert str(ended.value) == message
    assert tasks.cancels == ([_TASK] if cancelled else [])


@pytest.mark.asyncio
async def test_a_cancelled_call_cancels_its_task_at_the_server(host, monkeypatch, clock):
    tasks = _TaskServer([{"status": "working"}])
    runner = runner_for(host, tasks.server, monkeypatch)
    call = asyncio.create_task(runner.invoke("tools/call", {"name": "long"}, context(host)))
    try:
        async with asyncio.timeout(10):
            while tasks.gets < 2:
                await asyncio.sleep(0)
            call.cancel()
            while not tasks.cancels:
                await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await call
    finally:
        await runner.close()

    assert tasks.cancels == [_TASK]
