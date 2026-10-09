"""Tests for session-level token usage aggregation."""

from __future__ import annotations

from typing import Any

import pytest

from core.chat.messages import ChatMessage
from core.chat.usage import (
    ContextRoute,
    RequestContextUsage,
    aggregate_session_usage,
    latest_session_context_usage,
)
from core.utils.tokens import estimate_request_input_tokens

JsonObject = dict[str, Any]


def _assistant(usage: JsonObject | None) -> ChatMessage:
    return ChatMessage.assistant(model="openai/gpt-5.2", content="ok", usage=usage)


NO_TURNS = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
}


@pytest.mark.parametrize(
    ("messages", "totals"),
    [
        ([], NO_TURNS),
        (
            # Every turn counts: estimated counters join the totals, and a
            # counter a turn does not report counts as zero.
            [
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
                _assistant({"input_tokens": 500, "output_tokens": 5}),
                _assistant(
                    {
                        "input_tokens": 9000,
                        "output_tokens": 90,
                        "cache_read_tokens": 500,
                        "reasoning_tokens": 50,
                        "input_tokens_estimated": True,
                        "output_tokens_estimated": True,
                        "estimated": True,
                    }
                ),
            ],
            {
                "input_tokens": 10_500,
                "output_tokens": 145,
                "cache_read_tokens": 1300,
                "cache_write_tokens": 100,
                "reasoning_tokens": 80,
            },
        ),
        (
            [
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
            ],
            NO_TURNS,
        ),
    ],
    ids=["empty", "every-turn-counts", "junk-ignored"],
)
def test_session_usage_totals(messages: list[ChatMessage], totals: JsonObject) -> None:
    assert aggregate_session_usage(messages) == totals


def test_latest_session_context_usage_restores_saved_snapshot_plus_new_messages() -> None:
    assistant = ChatMessage.assistant(
        model="openai/gpt-5.6-luna",
        content=None,
        usage={
            "input_tokens": 10_000,
            "output_tokens": 100,
            "context_usage": {
                "tokens": 10_100,
                "estimated": True,
                "provider_input_tokens": 10_000,
                "provider_output_tokens": 100,
                "context_window": 400_000,
            },
        },
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
        "context_window": 400_000,
        "estimated_delta_tokens": delta_tokens,
    }


def test_latest_session_context_usage_has_no_projection_without_a_snapshot() -> None:
    # Every Assistant step the Agentic Loop persists carries a snapshot; the
    # counters alone are not reinterpreted as a Context size.
    assistant = _assistant({"input_tokens": 10_000, "output_tokens": 100})

    assert latest_session_context_usage([assistant, ChatMessage.user("next")]) is None


def test_latest_session_context_usage_prefers_newer_compaction_checkpoint() -> None:
    # The checkpoint fills the window of the Model that last answered.
    assistant = _assistant(
        {
            "input_tokens": 20_000,
            "output_tokens": 500,
            "context_usage": {"tokens": 20_500, "estimated": False, "context_window": 128_000},
        }
    )
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
        "context_window": 128_000,
    }


class _BiasedInputAdapter:
    def estimate_request_input_tokens(self, messages, *, model_id, tools=None):
        return 120_000 + estimate_request_input_tokens(messages, tools)[0]


def _route(adapter: Any = None, model: str = "model") -> ContextRoute:
    return ContextRoute(adapter or _BiasedInputAdapter(), model, f"provider/{model}")


class _Calibration:
    """A fixed correction that records what it learns."""

    def __init__(self, factor: float) -> None:
        self.value = factor
        self.samples: list[tuple[str, int, int]] = []

    def input_estimate_factor(self, model: str) -> float:
        return self.value

    def record_input_estimate(self, model: str, *, measured: int, estimated: int) -> None:
        self.samples.append((model, measured, estimated))


def test_request_measurement_cancels_existing_estimation_bias_and_counts_changes():
    accounting = RequestContextUsage()
    adapter = _BiasedInputAdapter()
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    tools = [{"type": "function", "function": {"name": "read", "description": "x" * 500}}]
    args = {"target": _route(adapter), "tools": tools, "scope": "epoch"}
    accounting.observe({"input_tokens": 150_000, "output_tokens": 20_000}, base, **args)
    assert accounting.project(base, **args)["tokens"] == 150_000
    assert accounting.project(base, **args)["estimated"] is False
    # A projection names the window of the Model the request goes to.
    assert accounting.project(base, **args, context_window=200_000)["context_window"] == 200_000
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


def test_estimate_factor_scales_only_the_estimated_part_of_a_projection():
    calibration = _Calibration(1.5)
    accounting = RequestContextUsage(calibration)
    adapter = _BiasedInputAdapter()
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    args = {"target": _route(adapter), "tools": [], "scope": "epoch"}
    raw = adapter.estimate_request_input_tokens(base, model_id="model")
    assert accounting.project(base, **args)["tokens"] == round(raw * 1.5)
    assert accounting.estimate(base, target=args["target"], tools=[]) == round(raw * 1.5)
    # A measurement teaches the calibration with the uncorrected estimate.
    accounting.observe({"input_tokens": 150_000}, base, **args)
    assert calibration.samples == [("provider/model", 150_000, raw)]
    assert accounting.project(base, **args)["tokens"] == 150_000
    assistant = {"role": "assistant", "content": "done " * 200}
    delta = estimate_request_input_tokens([assistant])[0]
    prepared = accounting.prepare([*base, assistant], **args)
    projection = accounting.project_prepared(prepared)
    assert projection["estimated_delta_tokens"] == round(delta * 1.5)
    assert projection["tokens"] == 150_000 + round(delta * 1.5)
    accounting.observe({"input_tokens": 9, "input_tokens_estimated": True}, base, **args)
    assert len(calibration.samples) == 1
    # Reusing a prepared request must still see calibration taught by another Run.
    calibration.value = 2.0
    assert accounting.project_prepared(prepared)["tokens"] == 150_000 + round(delta * 2.0)
    accounting.reset()
    assert accounting.project_prepared(prepared) == {
        "tokens": round((raw + delta) * 2.0),
        "estimated": True,
    }


def test_measurement_continues_across_runs_until_compaction():
    """A new Run resumes the Session's anchor; history corrects with its factor."""
    first = RequestContextUsage(_Calibration(1.5))
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    args = {"target": _route(), "tools": [], "scope": "epoch"}
    first.observe({"input_tokens": 150_000, "output_tokens": 50}, base, **args)
    answer = {"role": "assistant", "content": "done"}
    assistant = _assistant(
        {
            "input_tokens": 150_000,
            "output_tokens": 50,
            "context_usage": first.project([*base, answer], **args),
            "context_estimation": first.estimation_record(args["target"]),
        }
    )
    newer = ChatMessage.user("next task " * 100)
    # A later Run has a new Adapter instance for the same route.
    resumed = RequestContextUsage.resume([assistant, newer], _Calibration(1.5))
    next_args = {**args, "target": _route()}
    next_request = [*base, answer, {"role": "user", "content": "next task " * 100}]
    projection = resumed.project(next_request, **next_args)
    assert projection["provider_input_tokens"] == 150_000
    delta = estimate_request_input_tokens(next_request[2:])[0]
    assert projection["tokens"] == 150_000 + round(delta * 1.5)
    history = latest_session_context_usage([assistant, newer])
    assert history is not None
    assert history["tokens"] - assistant.usage["context_usage"]["tokens"] == round(
        estimate_request_input_tokens(next_request[3:])[0] * 1.5
    )

    checkpoint = ChatMessage.compaction_checkpoint(
        summary="summary",
        projection=[ChatMessage.user("tail")],
        compacted_token_count=10_000,
        context_tokens_before=150_000,
        context_tokens_after=4_000,
    ).with_compaction_context_tokens(
        context_tokens_before=150_000, context_tokens_after=4_000, estimate_factor=2.0
    )
    after_compaction = RequestContextUsage.resume([assistant, checkpoint], _Calibration(1.5))
    assert "provider_input_tokens" not in after_compaction.project(base, **next_args)
    assert latest_session_context_usage([assistant, checkpoint, newer])["tokens"] == 4_000 + round(
        estimate_request_input_tokens([{"role": "user", "content": "next task " * 100}])[0] * 2.0
    )


@pytest.mark.parametrize("change", ["route", "scope", "tools", "system", "reset"])
def test_request_measurement_does_not_cross_rebuilt_context(change):
    accounting = RequestContextUsage()
    base = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    args = {"target": _route(), "tools": [], "scope": "epoch"}
    accounting.observe({"input_tokens": 10_000}, base, **args)
    if change == "system":
        base = [{"role": "system", "content": "new rules"}, *base[1:]]
    elif change == "reset":
        accounting.reset()
    else:
        args[{"route": "target"}.get(change, change)] = {
            "route": _route(model="different"),
            "scope": "new-epoch",
            "tools": [{"function": {"name": "new"}}],
        }[change]
    prepared = accounting.prepare(base, **args)
    projection = accounting.project_prepared(prepared)
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
    args = {"target": _route(), "tools": [], "scope": "epoch"}
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


def test_estimated_input_never_anchors_or_replaces_a_measurement():
    accounting = RequestContextUsage()
    args = {"target": _route(), "tools": [], "scope": "epoch"}
    base = [{"role": "user", "content": "task"}]
    accounting.observe({"input_tokens": 10, "input_tokens_estimated": True}, base, **args)
    assert accounting.project(base, **args) == {
        "tokens": 120_000 + estimate_request_input_tokens(base)[0],
        "estimated": True,
    }

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
    args = {"target": _route(), "tools": [], "scope": "epoch"}
    base = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "word " * 500}]
    accounting.observe({"input_tokens": 100}, base, **args)
    projected = accounting.project(base[:1], **args)
    assert projected["tokens"] > 0
    assert projected["estimated"] is True
    assert "provider_input_tokens" not in projected


class _CountingAdapter:
    def __init__(self) -> None:
        self.estimates = 0

    def estimate_request_input_tokens(self, messages, *, model_id, tools=None):
        self.estimates += 1
        return estimate_request_input_tokens(messages, tools)[0]


def test_identical_requests_are_estimated_once_and_changes_estimate_afresh():
    """A Step projects, observes and re-projects one request with one estimate."""
    accounting = RequestContextUsage()
    adapter = _CountingAdapter()
    args = {"target": _route(adapter), "tools": [], "scope": "epoch"}
    request = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]

    prepared = accounting.prepare(request, **args)
    before = accounting.project_prepared(prepared)
    accounting.observe_prepared({"input_tokens": 5_000}, prepared)
    continuation = [*request, {"role": "assistant", "content": "done"}]
    accounting.project(continuation, **args)
    accounting.project([dict(message) for message in continuation], **args)

    assert before == {"tokens": estimate_request_input_tokens(request)[0], "estimated": True}
    assert accounting.project(request, **args)["tokens"] == 5_000
    assert adapter.estimates == 2
    edited = [*request, {"role": "assistant", "content": "edited"}]
    accounting.project(edited, **args)
    accounting.project(request, **{**args, "tools": [{"function": {"name": "read"}}]})
    assert adapter.estimates == 4
    # A snapshot owns only digests/counts: a later request edit cannot rewrite it.
    request[1]["content"] = "a changed request " * 100
    assert accounting.project_prepared(prepared)["tokens"] == 5_000
    assert accounting.project(request, **args)["tokens"] > 5_000
    assert adapter.estimates == 5


def test_estimate_memo_is_bounded_and_retains_only_digests():
    accounting = RequestContextUsage()
    args = {"target": _route(_CountingAdapter()), "tools": [], "scope": "epoch"}
    for index in range(10):
        accounting.project([{"role": "user", "content": f"private {index}"}], **args)

    memo = accounting._estimates
    assert len(memo) == 4
    assert all(
        len(key) == len(request_hash) == 64 and isinstance(count, int)
        for (key, request_hash), count in memo.items()
    )
