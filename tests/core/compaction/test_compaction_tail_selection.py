"""Tests for compaction tail selection."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.chat._message_history import effective_compaction_messages
from core.chat.wire_shaping import _embed_notes_into_request, _restore_in_run_assistant_reasoning
from core.compaction import (
    CompactionService,
    CompactionSettings,
    find_tail_boundary,
)
from core.compaction.compaction import (
    COMPACTION_USER_QUOTE_PREFIX,
    _plan_working_tail,
)
from core.providers.github_copilot_responses import (
    _messages_to_responses_input,
    estimate_responses_input_tokens,
)
from core.sessions import SessionAddress
from core.utils.tokens import NATIVE_MEDIA_TOKEN_RESERVE, estimate_request_input_tokens
from tests.core.compaction.compaction_test_support import (
    StubAdapter,
    StubStorage,
    _tail_token_span,
    assistant,
    message,
    provider_request,
    user,
)


def test_find_tail_boundary_does_not_anchor_latest_user() -> None:
    messages = [
        user("u1", "Keep working"),
        assistant("a1", "older answer " * 100),
        assistant("a2", "recent answer"),
    ]

    assert find_tail_boundary(messages, tail_tokens=1) == "a2"


def test_find_tail_boundary_keeps_parallel_tool_cycle_atomic() -> None:
    messages = [
        user("u1", "Keep working"),
        message(
            "a1",
            "assistant",
            "",
            model="openai/gpt-5",
            tool_calls=[
                {"id": "c1", "name": "read", "arguments": {"path": "one"}},
                {"id": "c2", "name": "read", "arguments": {"path": "two"}},
            ],
        ),
        message("t1", "tool", "one", tool_call_id="c1", name="read"),
        message("t2", "tool", "two", tool_call_id="c2", name="read"),
    ]

    assert find_tail_boundary(messages, tail_tokens=1) == "a1"


def test_context_ratio_and_absolute_token_triggers() -> None:
    service = CompactionService()

    assert service.should_auto_compact(80, 100, 0.8)
    assert not service.should_auto_compact(79, 100, 0.8)
    settings = CompactionSettings(trigger="input_tokens", trigger_tokens=100_000)
    assert service.should_auto_compact(100_000, 1_000_000, 0.8, settings=settings)
    assert not service.should_auto_compact(99_999, 1_000_000, 0.8, settings=settings)

    capped_ratio = CompactionSettings(threshold=0.8, max_input_tokens=200_000)
    assert service.should_auto_compact(200_000, 1_000_000, 0.8, settings=capped_ratio)
    assert not service.should_auto_compact(199_999, 1_000_000, 0.8, settings=capped_ratio)
    assert service.should_auto_compact(80_000, 100_000, 0.8, settings=capped_ratio)


def test_request_estimate_reserves_tool_result_media_without_counting_base64() -> None:
    encoded = "A" * 100_000
    messages = [
        {
            "role": "tool",
            "content": '{"ok":true}',
            "tool_call_id": "call-image",
            "tool_result_content": [
                {
                    "type": "media",
                    "media_type": "image/png",
                    "base64": encoded,
                }
            ],
        }
    ]

    estimated_tokens = CompactionService().estimate_messages_tokens(messages)

    assert estimated_tokens >= NATIVE_MEDIA_TOKEN_RESERVE
    assert estimated_tokens < NATIVE_MEDIA_TOKEN_RESERVE + 100


def test_working_tail_fills_backward_across_active_user_anchor() -> None:
    messages = [
        assistant("a-before", "Useful work before the latest instruction. " * 200),
        user("u-active", "Finish the same task with these final constraints."),
        assistant("a-after", "I am applying those constraints now."),
    ]
    target = _tail_token_span(messages)

    plan = _plan_working_tail(messages, target)

    assert plan.boundary_id == "a-before"
    assert list(plan.retained_messages) == messages


def test_working_tail_keeps_oversized_active_tool_batch_exact() -> None:
    active_user = user("u-active", "Inspect this large Tool result and continue.")
    active_arguments = {"query": "Q" * 20_000}
    active_result_content = "active-output-" * 10_000
    active_carrier = message(
        "a-active",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-active", "name": "read", "arguments": active_arguments}],
    )
    active_result = message(
        "t-active",
        "tool",
        active_result_content,
        tool_call_id="call-active",
        name="read",
    )

    plan = _plan_working_tail(
        [active_user, active_carrier, active_result],
        tail_tokens=10,
    )

    retained = list(plan.retained_messages)
    assert _tail_token_span(retained) > 10
    assert retained[0].tool_calls == active_carrier.tool_calls
    assert retained[1].content == active_result_content
    assert active_user not in retained


def test_working_tail_summarizes_whole_older_steps_instead_of_anchoring_user() -> None:
    active_user = user("u-active", "Keep working on this task.")
    older = assistant("a-old", "older work " * 4_000)
    recent = assistant("a-recent", "recent work " * 100)
    target = _tail_token_span([recent]) + 100

    plan = _plan_working_tail([active_user, older, recent], target)

    assert list(plan.retained_messages) == [recent]
    assert plan.boundary_index == 2
    assert _tail_token_span(plan.retained_messages) <= target


def test_working_tail_counts_live_reasoning_before_choosing_boundary() -> None:
    older = assistant("a-old", "old step")
    recent = assistant("a-new", "new step")
    messages = [user("u", "do it"), older, recent]
    live = provider_request(messages)
    live[2]["reasoning"] = "retained reasoning " * 4_000
    target = _tail_token_span(messages) + 100
    before = json.dumps(live)

    plan = _plan_working_tail(messages, target, request_messages=tuple(live))

    assert list(plan.retained_messages) == [recent]
    assert json.dumps(live) == before


def test_working_tail_counts_request_only_tool_media() -> None:
    calls = [{"id": "c", "name": "read", "arguments": {"path": "image.png"}}]
    carrier = message("a-old", "assistant", "", model="openai/gpt-5", tool_calls=calls)
    result = message("t", "tool", "image", tool_call_id="c", name="read")
    recent = assistant("a-new", "image consumed")
    messages = [user("u", "inspect"), carrier, result, recent]
    live = provider_request(messages)
    live[3]["tool_result_content"] = [
        {"type": "media", "media_type": "image/png", "base64": "A" * 10_000}
    ]

    plan = _plan_working_tail(messages, 1_000, request_messages=tuple(live))

    assert list(plan.retained_messages) == [recent]


def test_working_tail_uses_selected_wire_estimate_for_opaque_state() -> None:
    messages = [user("u", "go"), assistant("a-old", "old"), assistant("a-new", "new")]
    live = provider_request(messages)
    live[2]["reasoning_meta"] = {
        "response_output": [
            {"type": "reasoning", "id": "rs-old", "encrypted_content": "opaque" * 1_000}
        ]
    }
    seen = []

    def estimate(candidate):
        seen.append(candidate)
        return 5_000 if any(item.get("reasoning_meta") for item in candidate) else 100

    plan = _plan_working_tail(
        messages, 1_000, request_messages=tuple(live), estimate_tail_tokens=estimate
    )

    assert plan.boundary_id == "a-new"
    assert any(item.get("reasoning_meta") for item in seen[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("opaque", [False, True])
async def test_long_run_compaction_budgets_and_replays_the_actual_tail(opaque: bool) -> None:
    class WireAdapter(StubAdapter):
        def __init__(self):
            super().__init__("SUMMARY_SENTINEL: the approved work is partly completed.")
            self.estimates = []

        def estimate_request_input_tokens(self, messages, *, model_id, tools=None):
            self.estimates.append((messages, model_id))
            if opaque:
                return estimate_responses_input_tokens(list(messages), tools=tools)
            return estimate_request_input_tokens(messages, tools)[0]

    adapter = WireAdapter()
    messages = [
        user("u-old", "Earlier task"),
        assistant("a-old", "old context " * 43_000),
        assistant("proposal", "Option A: preserve complete recent steps and summarize old work."),
        user("u-current", "jo, mach A"),
    ]
    for index in range(24):
        call_id = f"call-{index}"
        arguments = {"text": "exact arguments " * 300}
        extra: dict[str, Any] = {"reasoning": "reasoning detail " * 1_000}
        if opaque:
            extra["reasoning_meta"] = {
                "response_output": [
                    {
                        "type": "reasoning",
                        "id": f"rs-{index}",
                        "encrypted_content": "sealed" * 1_000,
                    },
                    {
                        "type": "message",
                        "id": f"output-{index}",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "progress details " * 2_000}],
                    },
                    {
                        "type": "function_call",
                        "id": f"fc-{index}",
                        "call_id": call_id,
                        "name": "read",
                        "arguments": json.dumps(arguments),
                    },
                ]
            }
        messages.extend(
            [
                message(
                    f"a-{index}",
                    "assistant",
                    "progress details " * 2_000,
                    model="openai/gpt-5",
                    phase="commentary",
                    **extra,
                    tool_calls=[{"id": call_id, "name": "read", "arguments": arguments}],
                ),
                message(
                    f"t-{index}", "tool", "exact result " * 300, tool_call_id=call_id, name="read"
                ),
            ]
        )
    live = provider_request(messages)
    snapshot = json.dumps(live)
    budget = 15_000
    service = CompactionService()
    assert service.has_new_compactable_context(
        messages,
        CompactionSettings(tail_tokens=budget),
        request_messages=live,
        active_adapter=adapter,
        active_model_id="gpt-5",
    )
    result = await service.compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=adapter,
        summary_model_id="gpt-5",
        active_adapter=adapter,
        active_model_id="gpt-5",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=budget),
        request_messages=live,
    )
    effective = effective_compaction_messages([result])
    rebuilt = _restore_in_run_assistant_reasoning(_embed_notes_into_request(effective), live)
    tail = [item for item in rebuilt if item.get("id")]
    start = next(index for index, item in enumerate(live) if item.get("id") == tail[0]["id"])
    assert len(adapter.requests) == 1
    assert adapter.requests[0]["messages"][:-1] == live[:start]
    assert all(model_id == "gpt-5" for _, model_id in adapter.estimates)
    assert adapter.estimate_request_input_tokens(tail, model_id="gpt-5") <= budget
    assert [item["id"] for item in tail] == [item["id"] for item in live[start:]]
    for actual, original in zip(tail, live[start:], strict=True):
        for key in (
            "content",
            "tool_calls",
            "tool_call_id",
            "reasoning",
            "reasoning_meta",
            "phase",
        ):
            assert actual.get(key) == original.get(key)
    if opaque:
        assert _messages_to_responses_input(tail, document_media_types=frozenset()) == (
            _messages_to_responses_input(live[start:], document_media_types=frozenset())
        )
    assert json.dumps(live) == snapshot
    assert not any(item.role == "user" for item in effective)
    quote = next(
        item for item in effective if str(item.content).startswith(COMPACTION_USER_QUOTE_PREFIX)
    )
    assert (
        json.loads(str(quote.content).removeprefix(COMPACTION_USER_QUOTE_PREFIX))
        == messages[3].to_dict()
    )
