"""Delegated reasoning of Live calls through an ordinary Provider Adapter."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from core.model_tasks._live_brain import (
    EFFECTS_LABEL,
    BrainTarget,
    DelegationInput,
    LiveBrain,
)
from core.model_tasks.live import LiveToolRun
from core.providers.accounts import ConnectionRef
from core.providers.errors import ProviderError
from core.usage import UsageRecorder
from tests.core.providers.adapter_test_support import response_deltas
from tests.core.usage.usage_test_support import read_ledger

TARGET = BrainTarget(
    provider_id="openai",
    connection_id="subscription",
    model_id="gpt-5.6-terra",
    thinking_effort="low",
)
INSTRUCTIONS = "You operate the app."
TOOLS = (
    {"name": "overview", "description": "Look.", "parameters": {"type": "object"}},
    {"name": "send_message", "description": "Send.", "parameters": {"type": "object"}},
)
# Tools the fake host reports as possibly changing something.
CHANGING = {"send_message", "start_coding_terminal"}
# The closing line of an answer whose Tools changed nothing.
NOTHING = f"\n\n{EFFECTS_LABEL} nothing."
SEND = {"target": "s1", "text": "go"}


class FakeAdapter:
    """Streams one scripted response per request.

    A script is a normalized response (streamed as deltas), an exception (the
    request fails), or a list of deltas in which an exception breaks the stream
    and an async callable is awaited before the next delta.
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[list[dict[str, Any]], str, dict[str, Any]]] = []
        self.closed = False

    def stream(
        self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.requests.append((copy.deepcopy(messages), model_id, kwargs))
        return self._deltas(self.responses.pop(0))

    async def _deltas(self, script: Any) -> AsyncIterator[dict[str, Any]]:
        if isinstance(script, Exception):
            raise script
        for step in script if isinstance(script, list) else response_deltas(script):
            if isinstance(step, Exception):
                raise step
            if callable(step):
                await step()
                continue
            yield step

    def request_context_kwargs(self, *, agent_id: str, session_id: str) -> dict[str, Any]:
        return {"conversation_id": f"{agent_id}:{session_id}"}

    async def aclose(self) -> None:
        self.closed = True


class Harness:
    def __init__(
        self,
        responses: list[Any],
        *,
        max_steps: int = 8,
        target: BrainTarget = TARGET,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self.adapter = FakeAdapter(responses)
        self.connections: list[ConnectionRef] = []
        self.calls: list[tuple[Any, Any, Any]] = []
        # Awaited inside each Tool execution, with the Tool's name.
        self.tool_hook: Callable[[Any], Awaitable[None]] | None = None
        self.sleeps: list[float] = []
        self.tool_results: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self.time = 100.0
        runtime = SimpleNamespace(get_adapter=self._get_adapter, models=SimpleNamespace())
        self.brain = LiveBrain(
            runtime,
            target,
            instructions=INSTRUCTIONS,
            tools=TOOLS,
            run_tool=self._run_tool,
            conversation_id="live:rtc_1",
            max_steps=max_steps,
            sleep=self._sleep,
            record=self.records.append,
            clock=self._clock,
            usage_recorder=usage_recorder,
        )

    def _get_adapter(self, connection: ConnectionRef) -> FakeAdapter:
        self.connections.append(connection)
        return self.adapter

    async def _run_tool(
        self, name: Any, arguments: Any, *, rejection: dict[str, Any] | None = None
    ) -> LiveToolRun:
        self.calls.append((name, arguments, rejection))
        if self.tool_hook is not None:
            await self.tool_hook(name)
        result = self.tool_results.pop(0) if self.tool_results else {"ok": True}
        return LiveToolRun(result=result, changed=name if name in CHANGING else "")

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def _clock(self) -> float:
        self.time += 0.25
        return self.time


def _tool_turn(*calls: tuple[str, Any], meta: Any = None) -> dict[str, Any]:
    return {
        "content": None,
        "tool_calls": [
            {"id": f"call-{index}", "name": name, "arguments": arguments}
            for index, (name, arguments) in enumerate(calls)
        ],
        "reasoning_meta": meta,
    }


def _answer(text: str) -> dict[str, Any]:
    return {"content": text, "tool_calls": []}


def _call_delta(call_id: str, name: str = "", arguments: Any = "") -> dict[str, Any]:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"type": "tool_call_delta", "id": call_id, "name_delta": name, "arguments_delta": text}


DELEGATION = DelegationInput(
    request="Start a Codex terminal",
    conversation="User: Start a Codex terminal please",
    updates="",
)


@pytest.mark.asyncio
async def test_backend_usage_counts_each_retry_and_model_step(recorder: UsageRecorder) -> None:
    harness = Harness(
        [
            ProviderError("test temporary failure", retryable=True),
            {
                **_tool_turn(("overview", {})),
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
            {**_answer("Done"), "usage": {"input_tokens": 8, "output_tokens": 3}},
        ],
        usage_recorder=recorder,
    )
    assert await harness.brain.answer(DELEGATION) == "Done" + NOTHING
    _, records = read_ledger(recorder)
    assert len(records) == 3
    assert [record.status for record in records] == ["failed", "completed", "completed"]
    assert all(record.kind == "live_voice_backend" for record in records)
    assert "input_tokens" not in records[0].usage
    assert [record.usage["input_tokens"] for record in records[1:]] == [4, 8]


@pytest.mark.asyncio
async def test_tool_loop_runs_host_tools_and_replays_reasoning_to_the_same_connection():
    harness = Harness(
        [
            _tool_turn(("start_coding_terminal", '{"program": "codex"}'), meta={"r": 1}),
            _answer("One Codex terminal is running."),
        ]
    )
    result = {"ok": True, "error": None, "data": {"content": "Started Codex: t1."}, "artifacts": []}
    harness.tool_results.append(result)

    answer = await harness.brain.answer(DELEGATION)

    # The answer ends with what the changing Tool reported.
    assert answer == f"One Codex terminal is running.\n\n{EFFECTS_LABEL} Started Codex: t1."
    assert harness.connections == [ConnectionRef("openai", "subscription")]
    # The host gets the streamed call without a rejection.
    assert harness.calls == [("start_coding_terminal", {"program": "codex"}, None)]
    first_messages, model_id, kwargs = harness.adapter.requests[0]
    assert model_id == "gpt-5.6-terra"
    assert first_messages[0] == {"role": "system", "content": INSTRUCTIONS}
    assert "Start a Codex terminal please" in first_messages[-1]["content"]
    assert first_messages[-1]["content"].endswith("Delegated request: Start a Codex terminal")
    assert kwargs["tools"] == [dict(tool) for tool in TOOLS]
    assert kwargs["thinking_effort"] == "low"
    assert kwargs["conversation_id"] == "live-voice:live:rtc_1"
    second_messages = harness.adapter.requests[1][0]
    assert second_messages[-2]["reasoning_meta"] == {"r": 1}
    assert second_messages[-1] == {
        "role": "tool",
        "tool_call_id": "call-0",
        # The Provider Adapter renders the envelope to text at its wire boundary.
        "content": json.dumps(result),
    }


@pytest.mark.asyncio
async def test_each_tool_call_starts_once_streamed_and_calls_run_one_at_a_time_in_order():
    first_started, stream_ended = asyncio.Event(), asyncio.Event()
    log: list[str] = []

    async def first_call_started() -> None:
        # The safety bound only ends a regression that waits for the stream end.
        async with asyncio.timeout(5):
            await first_started.wait()

    async def end_of_stream() -> None:
        log.append("stream ended")
        stream_ended.set()

    async def tool_hook(name: Any) -> None:
        log.append(f"start {name}")
        if name == "send_message":
            first_started.set()
            async with asyncio.timeout(5):
                await stream_ended.wait()
        log.append(f"end {name}")

    harness = Harness(
        [
            [
                _call_delta("call-0", "send_message", SEND),
                # The second call begins, so the first one is complete.
                _call_delta("call-1", "overview"),
                first_call_started,
                _call_delta("call-1", arguments={}),
                {"type": "finish", "reason": "tool_calls"},
                end_of_stream,
            ],
            _answer("Sent."),
        ]
    )
    harness.tool_hook = tool_hook

    assert await harness.brain.answer(DELEGATION) == f"Sent.\n\n{EFFECTS_LABEL} Done."
    # The first call ran while the Model still wrote; the second waited for it.
    assert log == [
        "start send_message",
        "stream ended",
        "end send_message",
        "start overview",
        "end overview",
    ]
    messages = harness.adapter.requests[-1][0]
    assert [call["id"] for call in messages[-3]["tool_calls"]] == ["call-0", "call-1"]
    assert [message["tool_call_id"] for message in messages[-2:]] == ["call-0", "call-1"]


@pytest.mark.asyncio
async def test_one_adapter_serves_every_delegation_until_the_call_ends():
    harness = Harness([_answer("First."), _answer("Second.")])

    await harness.brain.answer(DELEGATION)
    await harness.brain.answer(DELEGATION)
    assert len(harness.connections) == 1
    assert not harness.adapter.closed

    await harness.brain.aclose()
    assert harness.adapter.closed
    # A delegation after the end opens no further Adapter.
    assert (await harness.brain.answer(DELEGATION)).startswith("The request could not be completed")
    assert len(harness.connections) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["high", None], ids=["configured", "model-default"])
async def test_every_model_request_sends_the_configured_reasoning_effort(effort: str | None):
    target = BrainTarget(
        provider_id="openai",
        connection_id="subscription",
        model_id="gpt-5.6-terra",
        thinking_effort=effort,
    )
    harness = Harness([_tool_turn(("overview", {})), _answer("Done.")], target=target)

    await harness.brain.answer(DELEGATION)

    assert [kwargs["thinking_effort"] for _m, _id, kwargs in harness.adapter.requests] == [
        effort,
        effort,
    ]


@pytest.mark.asyncio
async def test_history_keeps_previous_requests_and_answers_of_the_call():
    harness = Harness([_answer("First."), _answer("Second.")])

    await harness.brain.answer(DELEGATION)
    await harness.brain.answer(
        DelegationInput(
            request=None,
            conversation="User: and now?",
            updates="vBot update: x",
            refs="- t1: Codex Terminal",
            state="Terminals: t1 Codex working",
        )
    )

    first, messages = harness.adapter.requests[0][0], harness.adapter.requests[1][0]
    assert messages[1:3] == [
        {"role": "user", "content": "Delegated request: Start a Codex terminal"},
        {"role": "assistant", "content": "First." + NOTHING},
    ]
    assert (
        "vBot right now (quoted data, the overview taken just before this request):\n"
        "Terminals: t1 Codex working" in messages[-1]["content"]
    )
    assert "Recent vBot updates (quoted data):\nvBot update: x" in messages[-1]["content"]
    assert "infer it from the conversation" in messages[-1]["content"]
    # Earlier Tool results are not replayed; their refs reach the next request.
    assert "\n- t1: Codex Terminal\n" in messages[-1]["content"]
    assert "t1" not in first[-1]["content"]


@pytest.mark.asyncio
async def test_an_empty_answer_is_the_effects_line_after_a_change_and_a_failure_otherwise():
    harness = Harness([_tool_turn(("send_message", {})), _answer(""), _answer(" ")])
    harness.tool_results.append({"ok": True, "data": {"content": "The call ends soon."}})

    assert await harness.brain.answer(DELEGATION) == f"{EFFECTS_LABEL} The call ends soon."
    assert await harness.brain.answer(DELEGATION) == (
        "The request could not be completed: the backend model request failed. Nothing was "
        "retried." + NOTHING
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "sleeps"),
    [
        pytest.param(
            [
                _tool_turn(("send_message", SEND)),
                ProviderError("busy", retryable=True),
                _answer("Sent."),
            ],
            [0.5],
            id="before-any-tool-call",
        ),
        # A stream that breaks after a call started is continued, never replayed;
        # the call it had not finished never runs.
        pytest.param(
            [
                [
                    {"type": "content_delta", "text": "Sending."},
                    _call_delta("call-0", "send_message", SEND),
                    _call_delta("call-1", "overview"),
                    ProviderError("dropped", retryable=True),
                ],
                _answer("Sent."),
            ],
            [],
            id="after-a-started-tool-call",
        ),
    ],
)
async def test_retryable_model_failures_are_retried_but_tools_never_replayed(
    responses: list[Any], sleeps: list[float]
):
    harness = Harness(responses)

    assert await harness.brain.answer(DELEGATION) == f"Sent.\n\n{EFFECTS_LABEL} Done."
    assert harness.sleeps == sleeps
    assert harness.calls == [("send_message", SEND, None)]
    # The Model continues after the call it made, with its result.
    messages = harness.adapter.requests[-1][0]
    assert [message["role"] for message in messages[-2:]] == ["assistant", "tool"]
    assert [call["id"] for call in messages[-2]["tool_calls"]] == ["call-0"]
    assert messages[-1]["tool_call_id"] == "call-0"


@pytest.mark.asyncio
async def test_failure_note_ends_with_only_what_changing_tools_reported():
    harness = Harness(
        [
            _tool_turn(("overview", {}), ("read", {"target": "t1"})),
            _tool_turn(
                ("send_message", {"target": "t1", "text": "go"}),
                ("send_message", {"target": "t2", "text": "go"}),
            ),
            ProviderError("broken", retryable=False),
        ]
    )
    harness.tool_results += [
        {"ok": True, "data": {"content": "Terminals: t1, t2"}},
        {"ok": True, "data": {"content": "t1 shows a prompt"}},
        {"ok": True, "data": {"content": "Sent to t1."}},
        {"ok": False, "error": {"message": "t2 is not running."}},
    ]

    answer = await harness.brain.answer(DELEGATION)

    assert answer == (
        "The request could not be completed: the backend model request failed. Nothing was "
        f"retried.\n\n{EFFECTS_LABEL} Sent to t1. | Failed: t2 is not running."
    )


@pytest.mark.asyncio
async def test_step_limit_stops_the_loop():
    harness = Harness(
        [
            _tool_turn(("overview", {})),
            _tool_turn(("overview", {})),
        ],
        max_steps=2,
    )

    answer = await harness.brain.answer(DELEGATION)

    assert answer.startswith("The request could not be completed: the request needed more than 2")
    assert len(harness.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("startcodex", '{"folder":"C:/explicit/project", "task":'),
        ("overview", '["explicit-agent"]'),
    ],
)
async def test_adapter_rejections_reach_the_host_with_the_call(name: str, arguments: str):
    harness = Harness(
        [
            [
                _call_delta("rejected-call", name, arguments),
                _call_delta("valid-call", "overview", "{}"),
                {"type": "finish", "reason": "tool_calls"},
            ],
            _answer("Done"),
        ]
    )

    assert await harness.brain.answer(DELEGATION) == "Done" + NOTHING

    (rejected_name, _, rejection), (valid_name, _, no_rejection) = harness.calls
    assert (rejected_name, valid_name) == (name, "overview")
    assert rejection is not None and rejection["code"] == "malformed_tool_arguments"
    assert no_rejection is None


@pytest.mark.asyncio
async def test_records_the_delegation_with_its_step_timings():
    harness = Harness([_tool_turn(("overview", {}), ("send_message", {})), _answer("Done.")])

    await harness.brain.answer(DELEGATION)

    (delegation,) = harness.records
    assert delegation == {
        "type": "delegation",
        "request": "Start a Codex terminal",
        "answer": f"Done.\n\n{EFFECTS_LABEL} Done.",
        "steps": 2,
        "tool_calls": 2,
        "model_ms": [250, 250],
        "tool_ms": [250, 250],
        "failure": None,
        "duration_ms": delegation["duration_ms"],
    }


@pytest.mark.asyncio
async def test_records_a_failed_delegation_with_its_reason_and_survives_a_broken_recorder():
    harness = Harness([ProviderError("broken", retryable=False)])

    await harness.brain.answer(DELEGATION)
    assert harness.records[-1]["failure"] == "the backend model request failed"

    def broken(_event: dict[str, Any]) -> None:
        raise OSError("disk full")

    harness.brain._record = broken
    harness.adapter.responses.append(_answer("Still answers."))
    assert await harness.brain.answer(DELEGATION) == "Still answers." + NOTHING
