"""Usage in Chat Runs: measured and estimated counters on the answer, the history and the Run
end, the Context guard, price snapshots and the usage record of every Model attempt."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, override

import pytest

from core.models.pricing import TokenPricing
from core.providers.errors import NetworkError, ProviderError
from core.runs import RunCancelledError
from core.tools import ToolRegistry, tool_success
from core.usage import UsageRecorder
from core.utils.tokens import estimate_message_tokens, estimate_request_input_tokens
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubModels,
    StubRuntime,
    build_chat_loop,
    history,
    last_run,
    session_address,
)
from tests.core.chat.usage_recorder_support import RecordingUsageRecorder
from tests.core.usage.usage_test_support import read_ledger

JsonObject = dict[str, Any]

MEASURED = {
    "input_tokens": 1000,
    "output_tokens": 40,
    "cache_read_tokens": 700,
    "cache_write_tokens": 200,
    "reasoning_tokens": 25,
}
FUTURE_FIELD = {"measurement": "preserve-me"}
SESSION = session_address("coder", "session-one")


def _runtime(tmp_path: Path, adapter: StubAdapter, **runtime_options: Any) -> Any:
    agent = StubAgent(id="coder", model="openai/gpt-4.1", allowed_tools=["*"])
    return StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, **runtime_options)


def _adapter(text: str = "Hello", usage: JsonObject | None = None) -> StubAdapter:
    """One answer, served to a plain request and to a streaming one.

    A stream reports only token counters; a plain response may carry further fields.
    """
    response: JsonObject = {"content": text, "tool_calls": None}
    stream: list[JsonObject] = [{"type": "content_delta", "text": text}]
    if usage is not None:
        response["usage"] = usage
        counters = {key: value for key, value in usage.items() if key.endswith("_tokens")}
        stream.append({"type": "usage", **counters})
    stream.append({"type": "finish", "reason": "stop"})
    return StubAdapter([response], stream_responses=[stream])


def _weather_adapter(
    first_usage: JsonObject | None = None, adapter_type: type[StubAdapter] = StubAdapter
) -> StubAdapter:
    call: JsonObject = {
        "content": None,
        "tool_calls": [{"id": "call_1", "name": "get_weather", "arguments": {"city": "Berlin"}}],
    }
    if first_usage is not None:
        call["usage"] = first_usage
    return adapter_type([call, {"content": "Sunny", "tool_calls": None}])


def _weather_tools(report: str = "", *, install: bool = False) -> ToolRegistry:
    """A weather Tool; with *install* each call also enables another Tool."""
    tools = ToolRegistry()

    def weather(_context: Any, arguments: JsonObject) -> JsonObject:
        if install:
            tools.register("extra", "Extra Tool.", {"type": "object"}, weather)
        return tool_success({"temp": 22, "city": arguments["city"], "report": report})

    tools.register("get_weather", "Get weather.", {"type": "object"}, weather)
    return tools


def _counters(usage: JsonObject | None) -> JsonObject:
    assert usage is not None
    return {key: value for key, value in usage.items() if key.endswith(("_tokens", "estimated"))}


def _run_completed(runtime: Any, session_id: str = "session-one") -> JsonObject:
    run = last_run(runtime, session_id)
    [payload] = [event.payload for event in run.events if event.type == "run_completed"]
    return dict(payload)


def _saved_answer(runtime: Any, session_id: str = "session-one") -> Any:
    """The Session's last saved Assistant step."""
    return [message for message in history(runtime, session_id) if message.role == "assistant"][-1]


def _hello_tokens() -> int:
    """The estimated size the saved "Hello" answer adds to the next request."""
    return estimate_request_input_tokens([{"role": "assistant", "content": "Hello"}])[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["plain", "streaming"])
async def test_measured_usage_reaches_the_answer_the_run_end_and_the_usage_record(
    tmp_path: Path, streaming: bool
) -> None:
    runtime = _runtime(tmp_path, _adapter(usage={**MEASURED, "future_usage_field": FUTURE_FIELD}))
    recorder = runtime.usage_recorder = UsageRecorder(tmp_path / "model-usage.db")
    try:
        answer = await build_chat_loop(runtime, streaming=streaming).send(
            "coder", "Hi", session_id="session-one"
        )

        assert answer.usage is not None
        saved = _saved_answer(runtime)
        completed = _run_completed(runtime)
        # Measured counters are kept as reported, without an estimated flag.
        for usage in (answer.usage, saved.usage, completed["usage"]):
            assert _counters(usage) == MEASURED
        assert saved.usage["context_usage"] == answer.usage["context_usage"]
        if not streaming:
            assert saved.usage["future_usage_field"] == FUTURE_FIELD
        assert completed["status"] == "completed"
        assert completed["timing"]["duration_ms"] >= 0
        assert completed["session_usage"] == {
            "measured_turns": 1,
            "estimated_turns": 0,
            "cache_turns": 1,
            "input_tokens": 1000,
            "output_tokens": 40,
            "cache_read_tokens": 700,
            "cache_write_tokens": 200,
            "reasoning_turns": 1,
            "reasoning_tokens": 25,
        }
        assert completed["context_usage"] == {
            "tokens": 1000 + _hello_tokens(),
            "estimated": True,
            "estimated_delta_tokens": _hello_tokens(),
            "provider_input_tokens": 1000,
            "provider_output_tokens": 40,
        }

        # The usage record keeps only the canonical counters and outlives the Session.
        runtime.chat_sessions.delete(SESSION)
        _, records = read_ledger(recorder)
        assert [record.id for record in records] == [answer.usage["usage_call_id"]]
        assert _counters(records[0].usage) == MEASURED
        assert not {"estimated", "context_usage", "future_usage_field"} & set(records[0].usage)
    finally:
        recorder.close()


@pytest.mark.asyncio
async def test_completed_answer_saves_a_price_snapshot_with_its_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 500}
    runtime = _runtime(tmp_path, _adapter(usage=usage))
    pricing = TokenPricing.from_cost(
        {"input": 2, "output": 8, "cache_read": 0.2}, source="models.dev:openai/gpt-4.1"
    )
    monkeypatch.setattr(runtime.models, "pricing_for", lambda _: pricing)

    answer = await build_chat_loop(runtime).send("coder", "Hi", session_id="session-one")

    assert answer.usage is not None
    assert answer.usage["cost"]["amount_usd"] == pytest.approx(0.0019)
    assert answer.usage["cost"]["source"] == "catalog"
    assert _saved_answer(runtime).usage["cost"] == answer.usage["cost"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("streaming", "with_tool"),
    [(False, False), (True, False), (False, True)],
    ids=["plain", "streaming", "after-tool-results"],
)
async def test_missing_usage_is_estimated_from_the_sent_request_and_answer(
    tmp_path: Path, streaming: bool, with_tool: bool
) -> None:
    if with_tool:
        adapter = _weather_adapter()
        runtime = _runtime(tmp_path, adapter, tools=_weather_tools())
    else:
        adapter = _adapter()
        runtime = _runtime(tmp_path, adapter)

    answer = await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "Hi", session_id="session-one"
    )

    # The last request includes the previous Tool Call and its result.
    request = (adapter.stream_requests if streaming else adapter.requests)[-1]
    expected = {
        "input_tokens": estimate_request_input_tokens(
            request["messages"], request["kwargs"]["tools"]
        )[0],
        "input_tokens_estimated": True,
        "output_tokens": estimate_message_tokens({"role": "assistant", "content": answer.content})[
            0
        ],
        "output_tokens_estimated": True,
        "estimated": True,
    }
    for usage in (answer.usage, _saved_answer(runtime).usage, _run_completed(runtime)["usage"]):
        assert _counters(usage) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported",
    [{"output_tokens": 2572}, {"input_tokens": 0, "output_tokens": 2572}],
    ids=["output-only", "zero-input"],
)
async def test_unusable_provider_input_is_estimated_and_measured_output_kept(
    tmp_path: Path, reported: JsonObject
) -> None:
    runtime = _runtime(tmp_path, _adapter(usage=reported))

    answer = await build_chat_loop(runtime, streaming=True).send(
        "coder", "Hi", session_id="session-one"
    )

    assert answer.usage is not None
    estimated_input = answer.usage["input_tokens"]
    assert estimated_input > 0
    assert _counters(answer.usage) == {
        "input_tokens": estimated_input,
        "input_tokens_estimated": True,
        "output_tokens": 2572,
        "estimated": True,
    }
    completed = _run_completed(runtime)
    # Session totals count the measured output but never the estimated input.
    assert completed["session_usage"] == {
        "measured_turns": 0,
        "estimated_turns": 1,
        "cache_turns": 0,
        "input_tokens": 0,
        "output_tokens": 2572,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert completed["context_usage"] == {
        "tokens": estimated_input + _hello_tokens(),
        "estimated": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("measured_input", "report", "estimation_bias", "install", "fits"),
    [
        (950, "x" * 400, 0, False, False),
        # An enabled Tool leaves the pinned Tool list, and so the measurement, valid.
        (950, "x" * 400, 0, True, False),
        (300, "", 0, False, True),
        (300, "", 2_000, False, True),
    ],
    ids=[
        "measured-fills-window",
        "measured-fills-window-after-tool-install",
        "measured-below-window",
        "biased-estimate-below-window",
    ],
)
async def test_measured_context_decides_whether_the_next_request_fits_the_window(
    tmp_path: Path,
    measured_input: int,
    report: str,
    estimation_bias: int,
    install: bool,
    fits: bool,
) -> None:
    class BiasedAdapter(StubAdapter):
        @override
        def estimate_request_input_tokens(self, messages, *, model_id, tools=None):
            return estimation_bias + estimate_request_input_tokens(messages, tools)[0]

    adapter = _weather_adapter(
        {"input_tokens": measured_input, "output_tokens": 20}, adapter_type=BiasedAdapter
    )
    runtime = _runtime(
        tmp_path,
        adapter,
        tools=_weather_tools(report, install=install),
        models=StubModels({("openai", "gpt-4.1"): 1_000}),
    )
    loop = build_chat_loop(runtime)

    # A measured anchor that fills the window fails cleanly before the next send; an
    # estimation bias never overrides a measurement below the window.
    if fits:
        answer = await loop.send("coder", "Weather?", session_id="session-one")
        assert answer.content == "Sunny"
        assert len(adapter.requests) == 2
    else:
        with pytest.raises(ProviderError) as raised:
            await loop.send("coder", "Weather?", session_id="session-one")
        assert raised.value.retryable is False
        assert len(adapter.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["plain", "streaming"])
async def test_empty_or_restarted_attempt_keeps_reported_usage_and_links_saved_step(
    tmp_path: Path, recovery_waits: list[float], streaming: bool
) -> None:
    adapter = StubAdapter(
        [
            {
                "content": "",
                "terminal_outcome": "stop",
                "usage": {"input_tokens": 20, "output_tokens": 4},
            },
            {"content": "Recovered", "usage": {"input_tokens": 30}},
        ],
        stream_responses=[
            [
                {"type": "usage", "input_tokens": 20, "output_tokens": 4},
                NetworkError("retry this attempt"),
            ],
            [
                {"type": "content_delta", "text": "Recovered"},
                {"type": "usage", "input_tokens": 30},
                {"type": "finish", "reason": "stop"},
            ],
        ],
    )
    runtime = _runtime(tmp_path, adapter)
    recorder = runtime.usage_recorder = RecordingUsageRecorder()

    answer = await build_chat_loop(runtime, streaming=streaming).send(
        "coder", "Work", session_id="session-one"
    )

    failed, completed = recorder.calls
    assert failed["status"] == "failed"
    assert failed["usage"] == {
        "input_tokens": 20,
        "output_tokens": 4,
        "usage_call_id": failed["id"],
    }
    assert completed["status"] == "completed"
    assert completed["usage"]["input_tokens"] == 30
    assert completed["usage"]["output_tokens_estimated"] is True
    assert completed["usage"]["cost"]["source"] == "unknown"
    assert (completed["model"], completed["session_id"], completed["run_id"]) == (
        "openai/gpt-4.1",
        "session-one",
        last_run(runtime).id,
    )
    # Only the saved step links to its usage record.
    assert answer.usage is not None
    assert answer.usage["usage_call_id"] == completed["id"]
    assert _saved_answer(runtime).usage["usage_call_id"] == completed["id"]
    assert [message.role for message in history(runtime)].count("assistant") == 1
    assert len(recovery_waits) == 1


@pytest.mark.asyncio
async def test_cancel_before_visible_output_keeps_reported_counters(tmp_path: Path) -> None:
    waiting = asyncio.Event()

    class UsageThenWaitAdapter(StubAdapter):
        @override
        async def stream(self, messages, **kwargs):
            yield {"type": "usage", "input_tokens": 42}
            waiting.set()
            await asyncio.Event().wait()

    runtime = _runtime(tmp_path, UsageThenWaitAdapter([]))
    runtime.chat_sessions.create("coder", session_id="session-one")
    recorder = runtime.usage_recorder = RecordingUsageRecorder()
    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Work", session_id="session-one"
    )
    await waiting.wait()
    run.request_cancel()
    with pytest.raises(RunCancelledError):
        await run.wait()

    [call] = recorder.calls
    assert call["status"] == "cancelled"
    assert call["usage"] == {"input_tokens": 42, "usage_call_id": call["id"]}
    assert all(message.role != "assistant" for message in history(runtime))


@pytest.mark.asyncio
async def test_fatal_request_without_counters_records_unknown_usage(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, StubAdapter([ProviderError("not available", retryable=False)]))
    recorder = runtime.usage_recorder = RecordingUsageRecorder()

    with pytest.raises(ProviderError):
        await build_chat_loop(runtime).send("coder", "Work", session_id="session-one")

    [call] = recorder.calls
    assert call["status"] == "failed"
    assert call["usage"] == {"usage_call_id": call["id"]}


@pytest.mark.asyncio
async def test_cancel_during_usage_persistence_keeps_visible_answer(tmp_path: Path) -> None:
    saving = asyncio.Event()
    release = asyncio.Event()

    class WaitingUsageRecorder(RecordingUsageRecorder):
        @override
        async def finish(self, call_id, usage=None, *, status="completed"):
            saving.set()
            await release.wait()
            return await super().finish(call_id, usage, status=status)

    runtime = _runtime(
        tmp_path, _adapter("Visible answer", {"input_tokens": 42, "output_tokens": 5})
    )
    runtime.chat_sessions.create("coder", session_id="session-one")
    recorder = runtime.usage_recorder = WaitingUsageRecorder()
    run = await build_chat_loop(runtime, streaming=True).start_run(
        "coder", "Work", session_id="session-one"
    )
    await saving.wait()
    run.request_cancel()
    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()

    saved = _saved_answer(runtime)
    assert saved.content == "Visible answer"
    assert saved.usage["usage_call_id"] == recorder.calls[0]["id"]
