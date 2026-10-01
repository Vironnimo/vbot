"""Delegated reasoning of Live calls through an ordinary Provider Adapter."""

from __future__ import annotations

import copy
import json
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
from core.providers.github_copilot_responses import normalize_responses_response
from core.usage import UsageRecorder
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


class FakeAdapter:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[list[dict[str, Any]], str, dict[str, Any]]] = []
        self.closed = False

    async def send(self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any) -> Any:
        self.requests.append((copy.deepcopy(messages), model_id, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def normalize_response(self, response: Any, *, model_id: str | None = None) -> Any:
        return response

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
    # The host gets the call as the Model made it.
    assert harness.calls == [("start_coding_terminal", '{"program": "codex"}', None)]
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
async def test_retryable_model_failures_are_retried_but_tools_never_replayed():
    harness = Harness(
        [
            _tool_turn(("send_message", {"target": "s1", "text": "go"})),
            ProviderError("busy", retryable=True),
            _answer("Sent."),
        ]
    )

    assert await harness.brain.answer(DELEGATION) == f"Sent.\n\n{EFFECTS_LABEL} Done."
    assert harness.sleeps == [0.5]
    assert len(harness.calls) == 1


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
async def test_adapter_rejections_reach_the_host_with_the_call(
    name: str, arguments: str, monkeypatch: pytest.MonkeyPatch
):
    harness = Harness(
        [
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "rejected-call",
                        "name": name,
                        "arguments": arguments,
                    },
                    {
                        "type": "function_call",
                        "call_id": "valid-call",
                        "name": "overview",
                        "arguments": "{}",
                    },
                ],
            },
            {"status": "completed", "output": [{"type": "output_text", "text": "Done"}]},
        ]
    )
    monkeypatch.setattr(
        harness.adapter,
        "normalize_response",
        lambda response, **_kwargs: normalize_responses_response(response),
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
