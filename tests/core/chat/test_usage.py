"""Tests for session-level token usage aggregation."""

from __future__ import annotations

from typing import Any

import pytest

from core.chat.messages import ChatMessage
from core.chat.usage import (
    RequestContextUsage,
    aggregate_session_usage,
    build_model_step_context_usage,
    latest_session_context_usage,
)
from core.utils.tokens import estimate_request_input_tokens

JsonObject = dict[str, Any]


def _assistant(usage: JsonObject | None) -> ChatMessage:
    return ChatMessage.assistant(model="openai/gpt-5.2", content="ok", usage=usage)


def test_sums_measured_turns_field_by_field() -> None:
    messages = [
        ChatMessage.user(content="hello"),
        _assistant(
            {
                "input_tokens": 1000,
                "output_tokens": 50,
                "cache_read_tokens": 800,
                "cache_write_tokens": 100,
                "reasoning_tokens": 30,
            }
        ),
        _assistant({"input_tokens": 2000, "output_tokens": 150, "cache_read_tokens": 1900}),
    ]

    totals = aggregate_session_usage(messages)

    assert totals == {
        "measured_turns": 2,
        "estimated_turns": 0,
        "cache_turns": 2,
        "input_tokens": 3000,
        "output_tokens": 200,
        "cache_read_tokens": 2700,
        "cache_write_tokens": 100,
        "reasoning_turns": 1,
        "reasoning_tokens": 30,
    }


def test_estimated_turns_are_counted_but_never_summed() -> None:
    messages = [
        _assistant({"input_tokens": 1000, "output_tokens": 10}),
        _assistant(
            {
                "input_tokens": 9999,
                "output_tokens": 9999,
                "reasoning_tokens": 5000,
                "estimated": True,
            }
        ),
    ]

    totals = aggregate_session_usage(messages)

    assert totals["measured_turns"] == 1
    assert totals["estimated_turns"] == 1
    assert totals["input_tokens"] == 1000
    assert totals["output_tokens"] == 10
    assert "reasoning_tokens" not in totals
    assert "reasoning_turns" not in totals


def test_partial_turn_splits_estimated_input_from_measured_output() -> None:
    totals = aggregate_session_usage(
        [
            _assistant(
                {
                    "input_tokens": 9999,
                    "input_tokens_estimated": True,
                    "output_tokens": 2572,
                    "estimated": True,
                }
            )
        ]
    )

    assert totals["measured_turns"] == 0
    assert totals["estimated_turns"] == 1
    assert totals["input_tokens"] == 0
    assert totals["output_tokens"] == 2572


def test_reasoning_turns_count_reported_zero_without_changing_output() -> None:
    totals = aggregate_session_usage(
        [
            _assistant(
                {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "reasoning_tokens": 0,
                }
            )
        ]
    )

    assert totals["output_tokens"] == 20
    assert totals["reasoning_tokens"] == 0
    assert totals["reasoning_turns"] == 1


def test_cache_turns_counts_field_presence_not_value() -> None:
    messages = [
        _assistant({"input_tokens": 500, "output_tokens": 5, "cache_read_tokens": 0}),
        _assistant({"input_tokens": 500, "output_tokens": 5}),
    ]

    totals = aggregate_session_usage(messages)

    assert totals["cache_turns"] == 1
    assert totals["cache_read_tokens"] == 0


def test_ignores_non_assistant_messages_usage_less_turns_and_junk_values() -> None:
    messages = [
        ChatMessage.user(content="hi"),
        ChatMessage.note(content="internal"),
        _assistant(None),
        _assistant(
            {
                "input_tokens": -5,
                "output_tokens": "junk",
                "cache_read_tokens": True,
                "reasoning_tokens": True,
            }
        ),
    ]

    totals = aggregate_session_usage(messages)

    assert totals == {
        "measured_turns": 1,
        "estimated_turns": 0,
        "cache_turns": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def test_empty_history_yields_zero_totals() -> None:
    totals = aggregate_session_usage([])

    assert totals["measured_turns"] == 0
    assert totals["estimated_turns"] == 0
    assert totals["cache_turns"] == 0
    assert totals["input_tokens"] == 0


def test_context_usage_anchors_provider_measurement_and_estimates_only_tool_delta() -> None:
    assistant = {
        "role": "assistant",
        "reasoning": "short",
        "reasoning_meta": {
            "response_output": [{"type": "reasoning", "encrypted_content": "x" * 200_000}],
            "reasoning_items": [{"type": "reasoning", "encrypted_content": "x" * 200_000}],
            "encrypted_content": ["x" * 200_000],
        },
        "tool_calls": [{"id": "call-1", "name": "read", "arguments": {"path": "a"}}],
    }
    tool_result = {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "read",
        "content": "result payload",
    }
    delta_tokens, _ = estimate_request_input_tokens([tool_result])

    context_usage = build_model_step_context_usage(
        {"input_tokens": 154_731, "output_tokens": 243},
        [{"role": "system", "content": "large request"}, assistant, tool_result],
        estimated_delta_messages=[tool_result],
    )

    assert context_usage == {
        "tokens": 154_731 + 243 + delta_tokens,
        "estimated": True,
        "provider_input_tokens": 154_731,
        "provider_output_tokens": 243,
        "estimated_delta_tokens": delta_tokens,
    }


def test_context_usage_falls_back_to_complete_request_without_provider_measurement() -> None:
    current_request = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    context_usage = build_model_step_context_usage(
        {"input_tokens": 10, "output_tokens": 2, "estimated": True},
        current_request,
    )

    assert context_usage == {"tokens": 12, "estimated": True}


def test_context_usage_preserves_provider_output_with_estimated_input() -> None:
    context_usage = build_model_step_context_usage(
        {
            "input_tokens": 134_547,
            "input_tokens_estimated": True,
            "output_tokens": 2572,
            "estimated": True,
        },
        [{"role": "user", "content": "request"}],
    )

    assert context_usage == {
        "tokens": 137_119,
        "estimated": True,
        "provider_output_tokens": 2572,
    }


def test_latest_session_context_usage_restores_provider_anchor_plus_new_messages() -> None:
    assistant = ChatMessage.assistant(
        model="openai/gpt-5.6-luna",
        content=None,
        usage={"input_tokens": 10_000, "output_tokens": 100},
        tool_calls=[],
    )
    tool_result = ChatMessage.tool(
        tool_call_id="call-1",
        name="read",
        content="new result",
    )
    delta_tokens, _ = estimate_request_input_tokens(
        [
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "read",
                "content": "new result",
            }
        ]
    )

    context_usage = latest_session_context_usage([assistant, tool_result])

    assert context_usage == {
        "tokens": 10_100 + delta_tokens,
        "estimated": True,
        "provider_input_tokens": 10_000,
        "provider_output_tokens": 100,
        "estimated_delta_tokens": delta_tokens,
    }


def test_latest_session_context_usage_prefers_newer_compaction_checkpoint() -> None:
    assistant = _assistant({"input_tokens": 20_000, "output_tokens": 500})
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="summary",
        projection=[ChatMessage.user("tail")],
        compacted_token_count=10_000,
        context_tokens_before=20_500,
        context_tokens_after=4_000,
    )

    assert latest_session_context_usage([assistant, checkpoint]) == {
        "tokens": 4_000,
        "estimated": True,
    }


class _BiasedInputAdapter:
    def estimate_request_input_tokens(self, messages, *, model_id, tools=None):
        return 120_000 + estimate_request_input_tokens(messages, tools)[0]


def test_request_measurement_cancels_existing_estimation_bias_and_counts_changes():
    accounting = RequestContextUsage()
    adapter = _BiasedInputAdapter()
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    tools = [{"type": "function", "function": {"name": "read", "description": "x" * 500}}]
    args = {"adapter": adapter, "model_id": "model", "tools": tools, "scope": "epoch"}
    accounting.observe({"input_tokens": 150_000, "output_tokens": 20_000}, base, **args)
    assert accounting.project(base, **args)["tokens"] == 150_000
    assert accounting.project(base, **args)["estimated"] is False
    assistant = {"role": "assistant", "content": "done"}
    after = [*base, assistant]
    delta = estimate_request_input_tokens([assistant])[0]
    projection = accounting.project(after, **args)
    assert projection == {
        "tokens": 150_000 + delta,
        "estimated": True,
        "provider_input_tokens": 150_000,
        "provider_output_tokens": 20_000,
        "estimated_delta_tokens": delta,
    }
    # Output Usage measures generation, not how much of it is replayed.
    assert projection["tokens"] < 151_000
    assert accounting.project(base, **{**args, "scope": "new-epoch"})["tokens"] > 120_000
    assert "provider_input_tokens" not in accounting.project(base, **{**args, "scope": "new-epoch"})


@pytest.mark.parametrize("change", ["model", "adapter", "tools", "system", "reset"])
def test_request_measurement_does_not_cross_rebuilt_context(change):
    accounting = RequestContextUsage()
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    args = {"adapter": _BiasedInputAdapter(), "model_id": "model", "tools": [], "scope": "epoch"}
    accounting.observe({"input_tokens": 10_000}, base, **args)
    if change == "system":
        base = [{"role": "system", "content": "new rules"}, *base[1:]]
    elif change == "reset":
        accounting.reset()
    else:
        args[{"model": "model_id"}.get(change, change)] = {
            "model": "different",
            "adapter": _BiasedInputAdapter(),
            "tools": [{"function": {"name": "new"}}],
        }[change]
    projection = accounting.project(base, **args)
    assert projection["estimated"] is True
    assert projection["tokens"] > 120_000
    assert "provider_input_tokens" not in projection


def test_request_projection_accounts_for_retired_images_and_persists_signed_delta():
    accounting = RequestContextUsage()
    image = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 2000}}
        ],
    }
    base = [{"role": "user", "content": "task"}, image]
    args = {"adapter": _BiasedInputAdapter(), "model_id": "model", "tools": [], "scope": "epoch"}
    accounting.observe({"input_tokens": 30_000}, base, **args)
    projection = accounting.project(base[:1], **args)
    assert projection["estimated_delta_tokens"] < 0
    assert projection["tokens"] == 30_000 - estimate_request_input_tokens([image])[0]
    assistant = _assistant(
        {"input_tokens": 30_000, "output_tokens": 500, "context_usage": projection}
    )
    assert latest_session_context_usage([assistant]) == projection
    newer = ChatMessage.user("next task")
    restored = latest_session_context_usage([assistant, newer])
    assert restored["tokens"] > projection["tokens"]
    assert restored["estimated"] is True


def test_estimated_input_is_never_promoted_to_a_measurement():
    accounting = RequestContextUsage()
    args = {"adapter": _BiasedInputAdapter(), "model_id": "model", "tools": [], "scope": "epoch"}
    messages = [{"role": "user", "content": "task"}]
    accounting.observe({"input_tokens": 10, "input_tokens_estimated": True}, messages, **args)
    assert accounting.project(messages, **args) == {
        "tokens": 120_000 + estimate_request_input_tokens(messages)[0],
        "estimated": True,
    }


def test_missing_usage_keeps_previous_measured_request_anchor():
    accounting = RequestContextUsage()
    adapter = _BiasedInputAdapter()
    args = {"adapter": adapter, "model_id": "model", "tools": [], "scope": "epoch"}
    base = [{"role": "user", "content": "task"}]
    accounting.observe({"input_tokens": 10_000}, base, **args)
    next_request = [*base, {"role": "assistant", "content": "hello"}]
    before = accounting.project(next_request, **args)
    accounting.observe(
        {"input_tokens": before["tokens"], "input_tokens_estimated": True}, next_request, **args
    )
    assert accounting.project(next_request, **args) == before
    assert before["provider_input_tokens"] == 10_000


def test_removal_cannot_project_nonempty_request_to_zero_tokens():
    accounting = RequestContextUsage()
    args = {"adapter": _BiasedInputAdapter(), "model_id": "model", "tools": [], "scope": "epoch"}
    base = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "word " * 500}]
    accounting.observe({"input_tokens": 100}, base, **args)
    projected = accounting.project(base[:1], **args)
    assert projected["tokens"] > 0
    assert projected["estimated"] is True
    assert "provider_input_tokens" not in projected
