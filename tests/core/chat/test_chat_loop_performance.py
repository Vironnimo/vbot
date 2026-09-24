"""Chat Run phases are measured on the Session's performance track."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.performance import PerformanceService
from core.performance.performance import reset_for_tests
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
)

SESSION_TRACK = "coder/session-one"


@pytest_asyncio.fixture
async def performance(tmp_path: Path) -> AsyncIterator[PerformanceService]:
    reset_for_tests()
    service = PerformanceService(tmp_path / "performance")
    yield service
    await service.aclose()
    reset_for_tests()


def _spans_by_track(result: dict[str, Any]) -> dict[str, Counter[str]]:
    events = json.loads(Path(result["trace_path"]).read_text("utf-8"))["traceEvents"]
    tracks = {e["pid"]: e["args"]["name"] for e in events if e["name"] == "process_name"}
    spans: dict[str, Counter[str]] = {}
    for event in events:
        if event["ph"] == "X":
            spans.setdefault(tracks[event["pid"]], Counter())[event["name"]] += 1
    return spans


def _registry() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        "probe",
        "Return a fixed value.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"value": 1}),
    )
    return tools


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_run_phases_are_measured_on_the_session_track(
    tmp_path: Path, performance: PerformanceService, streaming: bool
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["probe"])
    tool_turn = {
        "content": None,
        "tool_calls": [
            {"id": "call_1", "name": "probe", "arguments": {}},
            {"id": "call_2", "name": "invented_tool", "arguments": {}},
        ],
    }
    if streaming:
        adapter = StubAdapter(
            [],
            stream_responses=[
                [
                    {"type": "heartbeat"},
                    {"type": "tool_call_delta", "id": "call_1", "name_delta": "probe"},
                    {"type": "tool_call_delta", "id": "call_1", "arguments_delta": "{}"},
                    {"type": "tool_call_delta", "id": "call_2", "name_delta": "invented_tool"},
                    {"type": "tool_call_delta", "id": "call_2", "arguments_delta": "{}"},
                    {"type": "finish", "reason": "tool_calls"},
                ],
                [
                    {"type": "content_delta", "text": "done"},
                    {"type": "finish", "reason": "stop"},
                ],
            ],
        )
    else:
        adapter = StubAdapter([tool_turn, {"content": "done", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=_registry())

    performance.start_recording()
    await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "measure me", session_id="session-one"
    )
    result = await performance.stop_recording()

    spans = _spans_by_track(result)[SESSION_TRACK]
    assert spans["run"] == 1
    assert spans["request build"] == 2
    assert spans["provider response"] == 2
    assert spans["first token"] == 2
    assert spans["persist assistant"] == 2
    assert spans["tool round"] == 1
    assert spans["persist tool results"] == 1
    assert spans["probe"] == 1
    metrics = result["summary"]["metrics"]
    for metric in (
        "chat.run",
        "chat.request_build",
        "provider.response",
        "provider.first_token",
        "chat.persist",
        "chat.tool_round",
        "tool.probe",
    ):
        assert metric in metrics, metric
    # A Tool name the Model invented never becomes a metric.
    assert not any(name.startswith("tool.invented") for name in metrics)
