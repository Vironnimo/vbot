"""Delegated reasoning of Live calls through an ordinary Provider Adapter."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.model_tasks._live_brain import BrainTarget, DelegationInput, LiveBrain
from core.model_tasks._live_tools import DELEGATION_INSTRUCTIONS
from core.providers.accounts import ConnectionRef
from core.providers.errors import ProviderError
from core.usage import UsageRecorder

TARGET = BrainTarget(
    provider_id="openai",
    connection_id="subscription",
    model_id="gpt-5.6-terra",
    thinking_effort="low",
)


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
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.sleeps: list[float] = []
        self.tool_results: list[dict[str, Any]] = []
        runtime = SimpleNamespace(get_adapter=self._get_adapter, models=SimpleNamespace())
        self.brain = LiveBrain(
            runtime,
            target,
            self._execute,
            conversation_id="live:rtc_1",
            max_steps=max_steps,
            sleep=self._sleep,
            usage_recorder=usage_recorder,
        )

    def _get_adapter(self, connection: ConnectionRef) -> FakeAdapter:
        self.connections.append(connection)
        return self.adapter

    async def _execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.executed.append((name, arguments))
        return self.tool_results.pop(0) if self.tool_results else {"ok": True}

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _tool_turn(*calls: tuple[str, dict[str, Any]], meta: Any = None) -> dict[str, Any]:
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
                **_tool_turn(("vbot_app", {"action": "context"})),
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
            {**_answer("Done"), "usage": {"input_tokens": 8, "output_tokens": 3}},
        ],
        usage_recorder=recorder,
    )
    assert await harness.brain.answer(DELEGATION) == "Done"
    _, records = recorder.read_since()
    assert len(records) == 3
    assert [record.status for record in records] == ["failed", "completed", "completed"]
    assert all(record.kind == "live_voice_backend" for record in records)
    assert "input_tokens" not in records[0].usage
    assert [record.usage["input_tokens"] for record in records[1:]] == [4, 8]


@pytest.mark.asyncio
async def test_tool_loop_executes_calls_and_replays_reasoning_to_the_same_connection():
    harness = Harness(
        [
            _tool_turn(("vbot_terminal", {"action": "start", "program": "codex"}), meta={"r": 1}),
            _answer("One Codex terminal is running."),
        ]
    )
    harness.tool_results.append({"ok": True, "terminals": [{"id": "term-1"}]})

    answer = await harness.brain.answer(DELEGATION)

    assert answer == "One Codex terminal is running."
    assert harness.connections == [ConnectionRef("openai", "subscription")]
    assert harness.executed == [("vbot_terminal", {"action": "start", "program": "codex"})]
    first_messages, model_id, kwargs = harness.adapter.requests[0]
    assert model_id == "gpt-5.6-terra"
    assert first_messages[0] == {"role": "system", "content": DELEGATION_INSTRUCTIONS}
    assert "Start a Codex terminal please" in first_messages[-1]["content"]
    assert first_messages[-1]["content"].endswith("Delegated request: Start a Codex terminal")
    assert [tool["name"] for tool in kwargs["tools"]] == ["vbot_app", "vbot_terminal"]
    assert kwargs["thinking_effort"] == "low"
    assert kwargs["conversation_id"] == "live-voice:live:rtc_1"
    second_messages = harness.adapter.requests[1][0]
    assert second_messages[-2]["reasoning_meta"] == {"r": 1}
    assert second_messages[-1] == {
        "role": "tool",
        "tool_call_id": "call-0",
        "content": json.dumps({"ok": True, "terminals": [{"id": "term-1"}]}),
    }
    assert harness.adapter.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["high", None], ids=["configured", "model-default"])
async def test_every_model_request_sends_the_configured_reasoning_effort(effort: str | None):
    target = BrainTarget(
        provider_id="openai",
        connection_id="subscription",
        model_id="gpt-5.6-terra",
        thinking_effort=effort,
    )
    harness = Harness(
        [_tool_turn(("vbot_app", {"action": "context"})), _answer("Done.")], target=target
    )

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
        DelegationInput(request=None, conversation="User: and now?", updates="vBot update: x")
    )

    messages = harness.adapter.requests[1][0]
    assert messages[1:3] == [
        {"role": "user", "content": "Delegated request: Start a Codex terminal"},
        {"role": "assistant", "content": "First."},
    ]
    assert "Recent vBot updates (quoted data):\nvBot update: x" in messages[-1]["content"]
    assert "infer it from the conversation" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_retryable_model_failures_are_retried_but_tools_never_replayed():
    harness = Harness(
        [
            _tool_turn(("vbot_app", {"action": "send", "agent_id": "a", "session_id": "s"})),
            ProviderError("busy", retryable=True),
            _answer("Sent."),
        ]
    )

    assert await harness.brain.answer(DELEGATION) == "Sent."
    assert harness.sleeps == [0.5]
    assert len(harness.executed) == 1


@pytest.mark.asyncio
async def test_failure_note_lists_only_actions_that_may_have_changed_something():
    harness = Harness(
        [
            _tool_turn(("vbot_app", {"action": "context"})),
            _tool_turn(("vbot_terminal", {"action": "input", "terminal_id": "t", "text": "go"})),
            ProviderError("broken", retryable=False),
        ]
    )

    answer = await harness.brain.answer(DELEGATION)

    assert answer == (
        "The request could not be completed: the backend model request failed. Actions already "
        "performed, possibly with uncertain results: vbot_terminal input. Nothing was retried."
    )
    assert harness.adapter.closed


@pytest.mark.asyncio
async def test_failure_without_actions_says_nothing_changed():
    harness = Harness([ProviderError("broken", retryable=False)])

    assert await harness.brain.answer(DELEGATION) == (
        "The request could not be completed: the backend model request failed. "
        "Nothing in the app was changed."
    )


@pytest.mark.asyncio
async def test_step_limit_stops_the_loop():
    harness = Harness(
        [
            _tool_turn(("vbot_terminal", {"action": "list"})),
            _tool_turn(("vbot_terminal", {"action": "list"})),
        ],
        max_steps=2,
    )

    answer = await harness.brain.answer(DELEGATION)

    assert answer.startswith("The request could not be completed: the request needed more than 2")
    assert len(harness.executed) == 2


@pytest.mark.asyncio
async def test_unknown_tools_and_malformed_arguments_are_refused_without_execution():
    harness = Harness(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "a", "name": "shell", "arguments": {"action": "run"}},
                    {"id": "b", "name": "vbot_app", "arguments": "{bad"},
                ],
            },
            _answer("I could not do that."),
        ]
    )

    await harness.brain.answer(DELEGATION)

    assert harness.executed == []
    tool_messages = [m for m in harness.adapter.requests[1][0] if m["role"] == "tool"]
    assert [json.loads(m["content"])["error"]["code"] for m in tool_messages] == [
        "unknown_tool",
        "invalid_arguments",
    ]
