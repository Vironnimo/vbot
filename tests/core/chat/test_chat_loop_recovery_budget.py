"""Recovery regressions through real Run progression and durable Session history."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.chat.streaming import StreamingChunkTimeoutError, StreamingProgressTimeoutError
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from core.runs import (
    PROVIDER_REQUEST_STATUS_EVENT,
    RunCancelledError,
    RunInterruptedError,
    RunStatus,
)
from core.tools import ToolRegistry, tool_success
from core.utils.retry import compute_retry_delay, retry_async
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr("core.chat.recovery.compute_retry_delay", lambda *a, **kw: (0, False))


def stream(text="Done", outcome="stop", *, reasoning=False):
    return [
        {"type": "reasoning_delta" if reasoning else "content_delta", "text": text},
        {"type": "finish", "reason": outcome},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_ninth_attempt_can_complete_without_a_fallback(tmp_path, streaming):
    failures = [NetworkError("temporarily unavailable")] * 8
    adapter = StubAdapter(
        [*failures, {"content": "Recovered"}],
        stream_responses=[*failures, stream("Recovered")],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    result = await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "Work", session_id="test"
    )
    assert result.content == "Recovered"
    assert len(adapter.stream_requests if streaming else adapter.requests) == 9
    assert next(iter(runtime.chat_runs._runs.values())).status == RunStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("reasoning", [False, True])
async def test_truncated_response_continues_from_durable_partial(tmp_path, streaming, reasoning):
    first = {
        "reasoning" if reasoning else "content": "Partial",
        "terminal_outcome": "output_truncated",
    }
    adapter = StubAdapter(
        [first, {"content": "Finished"}],
        stream_responses=[
            stream("Partial", "output_truncated", reasoning=reasoning),
            stream("Finished"),
        ],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    result = await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "Finish", session_id="test"
    )
    requests = adapter.stream_requests if streaming else adapter.requests
    assert result.content == "Finished"
    assert len(requests) == 2
    history = runtime.chat_sessions.get(session_address("coder", "test")).load()
    partial = next(message for message in history if message.role == "assistant")
    assert partial.interrupted
    assert history[-1].status == "completed"
    if not reasoning:
        assert any(message.get("content") == "Partial" for message in requests[1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_empty_stop_retries_without_persisting_empty_assistant(tmp_path, streaming):
    adapter = StubAdapter(
        [{"terminal_outcome": "stop"}, {"content": "Done"}],
        stream_responses=[[{"type": "finish", "reason": "stop"}], stream()],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    result = await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "Work", session_id="test"
    )
    assert result.content == "Done"
    history = runtime.chat_sessions.get(session_address("coder", "test")).load()
    assert [m.role for m in history] == ["user", "assistant", "run_summary"]
    assert len(adapter.stream_requests if streaming else adapter.requests) == 2


@pytest.mark.asyncio
async def test_fatal_after_text_preserves_partial_without_another_request(tmp_path):
    error = ProviderAuthError("invalid credential")
    adapter = StubAdapter(
        [], stream_responses=[[{"type": "content_delta", "text": "Partial"}, error]]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    with pytest.raises(ProviderAuthError) as raised:
        await build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="test")
    assert raised.value is error
    assert len(adapter.stream_requests) == 1
    history = runtime.chat_sessions.get(session_address("coder", "test")).load()
    assert history[1].content == "Partial"
    assert history[1].interrupted
    assert history[-1].status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "chunk", "progress", "reasoning", "empty"])
async def test_exhausted_recovery_reaches_configured_backup(tmp_path, failure):
    errors = {
        "network": NetworkError,
        "chunk": StreamingChunkTimeoutError,
        "progress": StreamingProgressTimeoutError,
    }
    failed = (
        [errors[failure]("unavailable") for _ in range(9)]
        if failure in errors
        else [stream("thinking", reasoning=True)] * 9
        if failure == "reasoning"
        else [[{"type": "finish", "reason": "stop"}]] * 9
    )
    adapter = StubAdapter([], stream_responses=failed)
    backup = StubAdapter([], stream_responses=[stream("Recovered")])
    agent = StubAgent(
        id="coder", model="openai/test", fallback_models=["anthropic/backup::api-key"]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        adapters_by_connection={"openai:api-key": adapter, "anthropic:api-key": backup},
        provider_ids={"openai", "anthropic"},
    )
    result = await build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="test")
    assert result.content == "Recovered"
    assert len(adapter.stream_requests) == 9
    assert len(backup.stream_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_establishment_and_stream_retries_do_not_multiply(tmp_path, streaming):
    class FailingAdapter(StubAdapter):
        attempts = 0

        async def fail(self):
            self.attempts += 1
            raise ProviderError("503", retryable=True)

        async def send(self, *args, **kwargs):
            return await retry_async(self.fail)

        async def stream(self, *args, **kwargs):
            await retry_async(self.fail)
            yield {}

    adapter = FailingAdapter([])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    with pytest.raises(ProviderError):
        await build_chat_loop(runtime, streaming=streaming).send("coder", "Work", session_id="test")
    assert adapter.attempts == 9


@pytest.mark.asyncio
async def test_restarts_and_partial_continuations_share_the_same_budget(tmp_path):
    adapter = StubAdapter(
        [],
        stream_responses=[
            NetworkError("first"),
            [{"type": "content_delta", "text": "Partial"}, NetworkError("second")],
            *[stream("thinking", reasoning=True)] * 7,
        ],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    with pytest.raises(RunInterruptedError):
        await build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="test")
    assert len(adapter.stream_requests) == 9
    assert next(iter(runtime.chat_runs._runs.values())).status == RunStatus.INTERRUPTED


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_in_band_rate_limit_honors_delay_before_retry_or_continuation(
    tmp_path, monkeypatch, partial
):
    delays = []

    def capture_delay(*args, **kwargs):
        delay, honored = compute_retry_delay(*args, **kwargs)
        delays.append((delay, honored))
        return 0, honored

    monkeypatch.setattr("core.chat.recovery.compute_retry_delay", capture_delay)
    error = ProviderRateLimitError("limited")
    error.retry_after = 60
    first = [{"type": "content_delta", "text": "Partial"}, error] if partial else error
    adapter = StubAdapter([], stream_responses=[first, stream()])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    await build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="test")
    assert len(delays) == 1
    assert delays[0][0] >= 60
    assert delays[0][1]


@pytest.mark.asyncio
async def test_truncated_tools_never_execute_and_cannot_reset_budget(tmp_path):
    calls = []
    tools = ToolRegistry()
    tools.register(
        "probe",
        "Probe",
        {"type": "object"},
        lambda context, args: calls.append(args) or tool_success({}),
    )
    adapter = StubAdapter(
        [
            {
                "terminal_outcome": "output_truncated",
                "tool_calls": [{"id": f"call_{i}", "name": "probe", "arguments": {"different": i}}],
            }
            for i in range(9)
        ]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/test", allowed_tools=["*"]),
        adapter=adapter,
        tools=tools,
    )
    with pytest.raises(RunInterruptedError):
        await build_chat_loop(runtime).send("coder", "Work", session_id="test")
    assert calls == []
    assert len(adapter.requests) == 9
    history = runtime.chat_sessions.get(session_address("coder", "test")).load()
    assert len([message for message in history if message.role == "tool"]) == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_fallback_chain_cannot_multiply_total_recovery_budget(tmp_path, fallback):
    adapters = [StubAdapter([], stream_responses=[NetworkError("offline")] * 9) for _ in range(3)]
    agent = StubAgent(
        id="coder",
        model="openai/test",
        fallback_models=["anthropic/backup::api-key", "third/last::api-key"] if fallback else [],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapters[0],
        adapters_by_connection=dict(
            zip(("openai:api-key", "anthropic:api-key", "third:api-key"), adapters, strict=True)
        ),
        provider_ids={"openai", "anthropic", "third"},
    )
    with pytest.raises(RunInterruptedError):
        await build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="test")
    assert [len(adapter.stream_requests) for adapter in adapters] == [9, 9 if fallback else 0, 0]
    run = next(iter(runtime.chat_runs._runs.values()))
    retry_status = [
        event.payload
        for event in run.events
        if event.type == PROVIDER_REQUEST_STATUS_EVENT
        and event.payload.get("state") == "retrying"
        and "attempt" in event.payload
    ]
    routes = ["openai/test::api-key"] + (["anthropic/backup::api-key"] if fallback else [])
    assert [
        (status["model"], status["attempt"], status["max_attempts"]) for status in retry_status
    ] == [(route, attempt, 9) for route in routes for attempt in range(2, 10)]


@pytest.mark.asyncio
async def test_completed_tool_boundaries_allow_long_runs_without_repeating_effects(tmp_path):
    effects = []
    tools = ToolRegistry()

    async def probe(context, arguments):
        effects.append(arguments["step"])
        return tool_success({"completed": arguments["step"]})

    tools.register(
        "probe",
        "Probe",
        {
            "type": "object",
            "properties": {"step": {"type": "integer"}},
            "required": ["step"],
            "additionalProperties": False,
        },
        probe,
    )
    responses = []
    for i in range(4):
        responses.extend(
            [
                NetworkError("temporary"),
                {"tool_calls": [{"id": f"call_{i}", "name": "probe", "arguments": {"step": i}}]},
            ]
        )
    responses.extend([NetworkError("last transient"), {"content": "Done"}])
    adapter = StubAdapter(responses)
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=StubAgent(id="coder", model="openai/test", allowed_tools=["*"]),
        adapter=adapter,
        tools=tools,
    )
    result = await build_chat_loop(runtime).send("coder", "Work", session_id="test")
    assert result.content == "Done"
    assert len(adapter.requests) == 10
    assert effects == [0, 1, 2, 3]
    assert [m["tool_call_id"] for m in adapter.requests[-1]["messages"] if m["role"] == "tool"] == [
        f"call_{i}" for i in range(4)
    ]


@pytest.mark.asyncio
async def test_cancel_during_backoff_stops_before_next_provider_request(tmp_path, monkeypatch):
    monkeypatch.setattr("core.chat.recovery.compute_retry_delay", lambda *a, **kw: (60, True))
    adapter = StubAdapter([], stream_responses=[NetworkError("offline")])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    runtime.chat_sessions.create("coder", session_id="test")
    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Work", session_id="test"
    )
    async with asyncio.timeout(5):
        while not any(
            event.type == PROVIDER_REQUEST_STATUS_EVENT and event.payload.get("delay_seconds") == 60
            for event in run.events
        ):
            await asyncio.sleep(0.01)
    run.request_cancel(reason="user")
    with pytest.raises(RunCancelledError):
        await run.wait()
    assert run.status == RunStatus.CANCELLED
    assert len(adapter.stream_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_whitespace_only_exhaustion_does_not_create_empty_assistant(tmp_path, streaming):
    adapter = StubAdapter(
        [{"content": " \n", "terminal_outcome": "stop"}] * 9, stream_responses=[stream(" \n")] * 9
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    with pytest.raises(ProviderError):
        await build_chat_loop(runtime, streaming=streaming).send("coder", "Work", session_id="test")
    assert len(adapter.stream_requests if streaming else adapter.requests) == 9
    assert not any(
        m.role == "assistant"
        for m in runtime.chat_sessions.get(session_address("coder", "test")).load()
    )


@pytest.mark.asyncio
async def test_inline_reasoning_only_stream_requests_missing_visible_answer(tmp_path):
    adapter = StubAdapter([], stream_responses=[stream("<think>Working</think>"), stream()])
    runtime: Any = StubRuntime(
        data_dir=tmp_path, agent=StubAgent(id="coder", model="openai/test"), adapter=adapter
    )
    result = await build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="test")
    assert result.content == "Done"
    assert len(adapter.stream_requests) == 2


@pytest.mark.asyncio
async def test_deadline_preserves_partial_and_prevents_further_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("core.chat.recovery.RECOVERY_TIMEOUT_SECONDS", 0.3)
    closed = asyncio.Event()

    class StallingAdapter(StubAdapter):
        attempts = 0

        async def stream(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise NetworkError("first failure")
            try:
                yield {"type": "content_delta", "text": "Partial"}
                await asyncio.Event().wait()
            finally:
                closed.set()

    adapter = StallingAdapter([])
    backup = StubAdapter([])
    agent = StubAgent(
        id="coder", model="openai/test", fallback_models=["anthropic/backup::api-key"]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        adapters_by_connection={"anthropic:api-key": backup},
        provider_ids={"openai", "anthropic"},
    )
    with pytest.raises(RunInterruptedError) as raised:
        await asyncio.wait_for(
            build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="test"), 5
        )
    assert raised.value.result.content == "Partial"
    assert closed.is_set()
    assert adapter.attempts == 2
    assert backup.stream_requests == []
    history = runtime.chat_sessions.get(session_address("coder", "test")).load()
    assert history[1].content == "Partial"
    assert history[-1].status == "interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize("backup", [False, True])
async def test_partial_result_survives_exhaustion_before_further_output(
    tmp_path, monkeypatch, backup
):
    if not backup:
        # The first partial is saved, but the server's delay cannot fit the
        # remaining recovery window, so no second operation may start.
        monkeypatch.setattr("core.chat.recovery.compute_retry_delay", lambda *a, **kw: (1801, True))
    adapter = StubAdapter(
        [],
        stream_responses=[
            [{"type": "content_delta", "text": "Partial"}, NetworkError("dropped")],
            *[NetworkError("offline")] * 8,
        ],
    )
    secondary = StubAdapter([], stream_responses=[NetworkError("offline")] * 9)
    agent = StubAgent(
        id="coder",
        model="openai/test",
        fallback_models=["anthropic/backup::api-key"] if backup else [],
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        adapters_by_connection={"anthropic:api-key": secondary},
        provider_ids={"openai", "anthropic"},
    )
    with pytest.raises(RunInterruptedError) as raised:
        await build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="test")
    assert raised.value.result.content == "Partial"
    assert len(adapter.stream_requests) == (9 if backup else 1)
    assert len(secondary.stream_requests) == (9 if backup else 0)


@pytest.mark.asyncio
async def test_interrupted_result_includes_fragments_from_all_fallback_routes(tmp_path):
    def fragments(text):
        return [[{"type": "content_delta", "text": part}, NetworkError("dropped")] for part in text]

    primary = StubAdapter([], stream_responses=fragments("abcdefghi"))
    secondary = StubAdapter([], stream_responses=fragments("jklmnopqr"))
    agent = StubAgent(
        id="coder", model="openai/test", fallback_models=["anthropic/backup::api-key"]
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=primary,
        adapters_by_connection={"anthropic:api-key": secondary},
        provider_ids={"openai", "anthropic"},
    )
    with pytest.raises(RunInterruptedError) as raised:
        await build_chat_loop(runtime, streaming=True).send("coder", "Work", session_id="test")
    assert raised.value.result.content == "abcdefghijklmnopqr"
    history = runtime.chat_sessions.get(session_address("coder", "test")).load()
    assert [m.content for m in history if m.role == "assistant"] == list("abcdefghijklmnopqr")
