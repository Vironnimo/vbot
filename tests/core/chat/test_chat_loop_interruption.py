"""Interrupted Chat Runs: the history they leave and the notice later requests carry."""

from __future__ import annotations

import asyncio
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, cast, override

import pytest

from core.chat import ChatMessage
from core.providers.errors import NetworkError
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    ReasoningReplayPolicy,
)
from core.runs import RunCancelledError, RunInterruptedError
from core.tools import ToolRegistry, tool_success
from tests.core.chat.chat_loop_support import (
    PolicyStubAdapter,
    StubAdapter,
    StubAgent,
    StubRuntime,
    StubSkill,
    StubSkills,
    TenToolsThenBlockingReasoningAdapter,
    build_chat_loop,
    session_address,
)

JsonObject = dict[str, Any]

USER_STOP_NOTICE = "The user stopped your previous turn before it was complete."
NETWORK_NOTICE = (
    "Your previous turn stopped before it was complete because the connection to the "
    "Model provider failed."
)


def _request_text(messages: list[JsonObject]) -> str:
    return "\n".join(str(message.get("content") or "") for message in messages)


def _content(messages: list[JsonObject]) -> list[JsonObject]:
    """The request messages without the per-request identity of the System Prompt."""
    return [
        {key: value for key, value in message.items() if key not in {"id", "timestamp"}}
        for message in messages
    ]


@pytest.mark.asyncio
async def test_input_append_is_the_only_write_between_admission_and_the_first_request(
    tmp_path: Path, monkeypatch
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_skills=["*"])
    observed: list[tuple[int, list[ChatMessage]]] = []
    address = session_address("coder", "session-one")
    writes = 0

    class ObservingAdapter(StubAdapter):
        @override
        async def send(self, messages: Any, *, model_id: str, **kwargs: Any) -> Any:
            observed.append((writes, runtime.chat_sessions.get(address).load()))
            return await super().send(messages, model_id=model_id, **kwargs)

    adapter = ObservingAdapter(
        [{"content": "warm", "tool_calls": None}, {"content": "done", "tool_calls": None}]
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    runtime.skills = StubSkills([])
    loop = build_chat_loop(runtime)
    await loop.send("coder", "warm up", session_id="session-one")
    runtime.skills = StubSkills([StubSkill("deploy", "Ship the app.", tmp_path / "deploy")])
    store = runtime.chat_sessions._store
    execute_write = store._execute_write

    def counting_write(*args: Any, **kwargs: Any) -> Any:
        nonlocal writes
        writes += 1
        return execute_write(*args, **kwargs)

    monkeypatch.setattr(store, "_execute_write", counting_write)
    run = await loop.start_run("coder", "measure me", session_id="session-one")
    await run.wait()

    # Run admission is one transaction; the input append carries the Skill
    # announcement and its seen-Skill record in the next.
    request_writes, history = observed[-1]
    assert request_writes == 2
    assert [message.role for message in history[-2:]] == ["note", "user"]
    assert "deploy: Ship the app." in cast(str, history[-2].content)
    assert runtime.chat_sessions.seen_skills(address) == frozenset({"deploy"})


@pytest.mark.asyncio
async def test_user_stop_keeps_completed_tool_results_and_tells_the_next_run(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    first_adapter = TenToolsThenBlockingReasoningAdapter()
    second_adapter = StubAdapter(
        [],
        stream_responses=[
            [
                {"type": "content_delta", "text": "Corrected from retained work"},
                {"type": "finish", "reason": "stop"},
            ]
        ],
    )
    executions: list[str] = []
    tools = ToolRegistry()

    def get_weather(_context: Any, arguments: JsonObject) -> JsonObject:
        executions.append(str(arguments["city"]))
        return tool_success({"city": arguments["city"], "temperature": 22})

    tools.register("get_weather", "Get weather.", {"type": "object"}, get_weather)
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=first_adapter, tools=tools)
    runtime.chat_sessions.create("coder", session_id="session-one")
    loop = build_chat_loop(runtime)

    first_run = await loop.start_run("coder", "Inspect ten cities", session_id="session-one")
    await first_adapter.second_step_started.wait()
    runtime.adapter = second_adapter
    first_run.request_cancel(reason="user")
    queued = await loop.queue_run(
        "coder", "Use those results, but correct the conclusion", session_id="session-one"
    )

    with pytest.raises(RunCancelledError):
        await first_run.wait()
    assistant = await (await queued.future).wait()

    assert assistant.content == "Corrected from retained work"
    assert executions == ["Berlin"] * 10
    request_messages = second_adapter.stream_requests[0]["messages"]
    assert sum(message["role"] == "tool" for message in request_messages) == 10
    correction_index = next(
        index
        for index, message in enumerate(request_messages)
        if message.get("content") == "Use those results, but correct the conclusion"
    )
    assert USER_STOP_NOTICE in str(request_messages[correction_index - 1]["content"])
    request_text = _request_text(request_messages)
    assert request_text.count(USER_STOP_NOTICE) == 1
    # The stopped step's reasoning stays in history only.
    assert "Review the completed batch." not in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [REASONING_REPLAY_CURRENT_RUN, REASONING_REPLAY_FULL_HISTORY])
async def test_an_interrupted_run_leaves_its_partial_answer_and_one_stable_notice(
    tmp_path: Path, recovery_waits: list[float], policy: ReasoningReplayPolicy
) -> None:
    adapter = PolicyStubAdapter([], policy=policy)
    adapter._stream_responses = [
        [
            {"type": "reasoning_delta", "text": "PLAN-SENTINEL"},
            {"type": "content_delta", "text": "PARTIAL-SENTINEL"},
            NetworkError("dropped after text"),
        ],
        *[NetworkError("offline before text") for _ in range(8)],
        *(
            [{"type": "content_delta", "text": answer}, {"type": "finish", "reason": "stop"}]
            for answer in ("First answer", "Second answer")
        ),
    ]
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    loop = build_chat_loop(runtime)
    with pytest.raises(RunInterruptedError, match="network"):
        await loop.send("coder", "Work", session_id="s")

    await loop.send("coder", "Next", session_id="s")
    await loop.send("coder", "Again", session_id="s")

    first, second = (request["messages"] for request in adapter.stream_requests[-2:])
    partials = [message for message in first if message["role"] == "assistant"]
    assert [message["content"] for message in partials] == ["PARTIAL-SENTINEL"]
    assert not {"reasoning", "reasoning_meta"} & partials[0].keys()
    assert "PLAN-SENTINEL" not in _request_text(first)
    next_index = next(
        index for index, message in enumerate(first) if message.get("content") == "Next"
    )
    assert NETWORK_NOTICE in str(first[next_index - 1]["content"])
    assert _request_text(first).count(NETWORK_NOTICE) == 1
    # Later requests repeat the notice unchanged at the same position.
    assert _content(second[: len(first)]) == _content(first)
    assert _request_text(second).count(NETWORK_NOTICE) == 1


@pytest.mark.asyncio
async def test_streamed_output_is_drafted_until_its_assistant_entry_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.chat._stream_draft.STREAM_DRAFT_FLUSH_INTERVAL_SECONDS", 0.0)

    class PausingAdapter(StubAdapter):
        def __init__(self) -> None:
            super().__init__([])
            self.paused = asyncio.Event()
            self.release = asyncio.Event()

        @override
        async def stream(self, messages: list[JsonObject], *, model_id: str, **kwargs: Any) -> Any:
            self.stream_requests.append({"messages": deepcopy(messages), "model_id": model_id})
            yield {"type": "reasoning_delta", "text": "Plan"}
            yield {"type": "content_delta", "text": "Half"}
            self.paused.set()
            await self.release.wait()
            yield {"type": "content_delta", "text": " done"}
            yield {"type": "finish", "reason": "stop"}

    adapter = PausingAdapter()
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="s")

    def drafted() -> tuple[str, str]:
        with sqlite3.connect(runtime.chat_sessions._store.path) as connection:
            rows = connection.execute(
                "SELECT model, reasoning_delta, content_delta FROM run_stream_drafts "
                "ORDER BY chunk_key"
            ).fetchall()
        assert {row[0] for row in rows} <= {"openai/test"}
        return "".join(row[1] for row in rows), "".join(row[2] for row in rows)

    run = await build_chat_loop(runtime).start_run("coder", "Work", session_id="s")
    await adapter.paused.wait()
    async with asyncio.timeout(5):
        while drafted() != ("Plan", "Half"):
            await asyncio.sleep(0.01)
    adapter.release.set()
    assistant = await run.wait()

    assert assistant.content == "Half done"
    assert drafted() == ("", "")
