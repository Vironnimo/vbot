"""Tail selection: whole steps up to the requested size, counted on the actual request."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.chat import ChatMessage, ToolCall
from core.chat._message_history import effective_compaction_messages
from core.chat.wire_shaping import (
    _embed_notes_into_request,
    _notes_to_request_messages,
    _restore_in_run_assistant_reasoning,
)
from core.compaction import (
    CompactionError,
    CompactionService,
    CompactionSettings,
    find_tail_boundary,
)
from core.compaction.compaction import (
    COMPACTION_SUMMARY_NOTE_PREFIX,
    TAIL_SOFT_LIMIT_PERCENT,
    _plan_working_tail,
)
from core.providers.github_copilot_responses import (
    _messages_to_responses_input,
    estimate_responses_input_tokens,
)
from core.utils.tokens import estimate_request_input_tokens
from tests.core.compaction.compaction_test_support import (
    StubAdapter,
    _tail_token_span,
    assistant,
    compact,
    message,
    provider_request,
    quoted_user,
    tool_step,
    user,
    user_quotes,
)


@pytest.mark.parametrize(
    ("messages", "boundary"),
    [
        (
            [
                user("u1", "Keep working"),
                assistant("a1", "older answer " * 100),
                assistant("a2", "recent answer"),
            ],
            "a2",
        ),
        (
            [
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
            ],
            "a1",
        ),
    ],
    ids=["latest-user-is-no-anchor", "parallel-tool-cycle-stays-whole"],
)
def test_find_tail_boundary_starts_at_a_whole_step(
    messages: list[ChatMessage], boundary: str
) -> None:
    assert find_tail_boundary(messages, tail_tokens=1) == boundary


def _historical_steps(steps: tuple[str, ...]) -> list[ChatMessage]:
    """Build valid records whose ordering may contain incomplete or orphaned Tool cycles."""
    messages = []
    for index, step in enumerate(steps):
        key = f"message-{index}"
        if step.startswith(("call:", "silent-call:")):
            messages.append(
                ChatMessage.assistant(
                    message_id=key,
                    content=None if step.startswith("silent-call:") else "",
                    model="test/model",
                    tool_calls=[
                        ToolCall(id=call_id, name="read", arguments={})
                        for call_id in step.split(":", 1)[1].split(",")
                    ],
                )
            )
        elif step.startswith("result:"):
            messages.append(message(key, "tool", "result", tool_call_id=step[7:], name="read"))
        elif step == "reasoning":
            messages.append(
                ChatMessage.assistant(model="test/model", content=None, reasoning="thinking")
            )
        elif step == "assistant":
            messages.append(assistant(key, "answer"))
        elif step == "run_summary":
            messages.append(
                ChatMessage.run_summary(
                    run_id="historical-run",
                    status="completed",
                    timing={
                        "started_at": "2026-05-19T12:00:00+00:00",
                        "completed_at": "2026-05-19T12:00:01+00:00",
                        "duration_ms": 1_000,
                    },
                    iteration_count=1,
                )
            )
        elif step == "agent_takeover":
            messages.append(
                ChatMessage.agent_takeover(from_address="one@project", to_address="two@project")
            )
        elif step == "error":
            messages.append(ChatMessage.error("provider", "historical error"))
        else:
            messages.append(message(key, step, step))
    return messages


@pytest.mark.parametrize(
    ("steps", "boundary_index"),
    [
        (("call:a,b", "result:b", "note", "run_summary", "agent_takeover", "error", "result:a"), 0),
        (("silent-call:a", "result:a"), 0),
        (("call:a,a", "result:a"), 0),
        (("call:a", "result:a", "call:a", "result:a"), 0),
        (("result:orphan", "user", "assistant"), 1),
        (("call:a", "user", "assistant"), 1),
        (("call:a,b", "result:a", "assistant", "user"), 2),
        (("call:a", "result:b", "user"), 2),
        (("call:a", "result:a", "result:a", "assistant"), 3),
        (("call:a", "user", "result:a", "assistant"), 3),
        (("user", "call:a"), None),
        (("user", "call:a,b", "result:b"), None),
        (("user", "result:orphan"), None),
        (("call:a", "reasoning", "result:a"), None),
        (("user", "reasoning"), 0),
        (("reasoning",), None),
    ],
    ids=[
        "parallel-results-in-reverse-order-ignore-metadata-carriers",
        "assistant-without-content-can-start-complete-cycle",
        "duplicate-call-ids-still-form-one-pending-id",
        "call-ids-may-repeat-in-later-cycles",
        "orphan-before-valid-tail",
        "unresolved-cycle-before-valid-tail",
        "partial-batch-before-valid-tail",
        "foreign-result-before-valid-tail",
        "duplicate-result-before-valid-tail",
        "result-cannot-cross-user",
        "unresolved-cycle-at-end",
        "partial-batch-at-end",
        "orphan-at-end",
        "result-cannot-cross-reasoning-only-assistant",
        "reasoning-only-assistant-can-follow-user",
        "reasoning-only-assistant-is-no-boundary",
    ],
)
def test_find_tail_boundary_keeps_only_provider_safe_suffixes_of_historical_sequences(
    steps: tuple[str, ...], boundary_index: int | None
) -> None:
    messages = _historical_steps(steps)
    # A target larger than this history selects the oldest valid cut, not merely
    # the newest safe step; corruption before that cut cannot invalidate its Tail.
    if boundary_index is None:
        with pytest.raises(CompactionError):
            find_tail_boundary(messages, tail_tokens=100_000)
    else:
        assert find_tail_boundary(messages, tail_tokens=100_000) == messages[boundary_index].id


def test_working_tail_treats_the_latest_user_as_an_ordinary_step() -> None:
    # Growing backward, the Tail crosses the User message like any other step ...
    across = [
        assistant("a-before", "Useful work before the latest instruction. " * 200),
        user("u-active", "Finish the same task with these final constraints."),
        assistant("a-after", "I am applying those constraints now."),
    ]
    plan = _plan_working_tail(across, _tail_token_span(across))
    assert plan.boundary_id == "a-before"
    assert list(plan.retained_messages) == across

    # ... and a User before the cut is summarized with the whole older steps.
    recent = assistant("a-recent", "recent work " * 100)
    target = _tail_token_span([recent]) + 20
    plan = _plan_working_tail(
        [user("u-active", "Keep working."), assistant("a-old", "older work " * 4_000), recent],
        target,
    )
    assert list(plan.retained_messages) == [recent]
    assert plan.boundary_index == 2
    assert _tail_token_span(plan.retained_messages) <= target


def test_working_tail_keeps_oversized_active_tool_batch_exact() -> None:
    active_user = user("u-active", "Inspect this large Tool result and continue.")
    active_result_content = "active-output-" * 10_000
    carrier, result = tool_step("active", active_result_content, arguments={"query": "Q" * 20_000})

    plan = _plan_working_tail([active_user, carrier, result], tail_tokens=10)

    retained = list(plan.retained_messages)
    assert _tail_token_span(retained) > 10
    assert retained == [carrier, result]
    assert retained[1].content == active_result_content


@pytest.mark.parametrize(
    ("newest_first_words", "retained_steps"),
    [
        # Tails of 72% and 130%: the first cut reaching the target wins,
        # although the smaller one is closer to it.
        ((720, 580), 2),
        # Tails of 40%, 80% and 200%: the first reaching cut is past the soft
        # limit, so the largest cut down to the floor wins.
        ((400, 400, 1_200), 2),
        # Tails of 30%, 60% and 200%: nothing lies between floor and soft limit,
        # so the Tail grows past the soft limit instead of shrinking further.
        ((300, 300, 1_400), 3),
    ],
)
def test_working_tail_takes_first_cut_reaching_the_target(
    newest_first_words: tuple[int, ...], retained_steps: int
) -> None:
    steps = [
        assistant(f"a{index}", "word " * words)
        for index, words in reversed(list(enumerate(newest_first_words)))
    ]
    messages = [user("u", "long task"), *steps]

    plan = _plan_working_tail(messages, 1_000)

    assert list(plan.retained_messages) == steps[-retained_steps:]


def _delivered_review(output_words: int) -> list[ChatMessage]:
    return [
        user("u", "start"),
        *tool_step("1", "output " * output_words),
        message("n-delivery", "note", "New Board messages for you: please review."),
        assistant("a-reply", "Reviewed; posting my findings."),
    ]


def _live_request(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    return [
        {"id": "system-1", "role": "system", "content": "system"},
        *_embed_notes_into_request(messages),
    ]


@pytest.mark.parametrize("merged", [False, True], ids=["rendered-exactly", "merged-in-request"])
def test_working_tail_keeps_delivered_notes_only_when_the_request_renders_them_exactly(
    merged: bool,
) -> None:
    messages = _delivered_review(2_000)
    delivery, reply = messages[-2:]
    live = _live_request(messages)
    if merged:
        # The request merged the delivery with other context, so its request
        # message is not exactly the rendered note: the Tail must not claim it.
        live[-2] = {"role": "user", "content": live[-2]["content"] + "\nother context"}

    plan = _plan_working_tail(messages, 1, request_messages=tuple(live))

    if merged:
        assert plan.boundary_id == "a-reply"
        assert list(plan.retained_messages) == [reply]
        assert plan.request_start == len(live) - 1
    else:
        assert plan.boundary_id == "n-delivery"
        assert list(plan.retained_messages) == [delivery, reply]
        assert plan.request_start is not None
        assert live[plan.request_start :] == [*_notes_to_request_messages([delivery]), live[-1]]
        assert live[plan.request_start - 1]["id"] == "t-1"


@pytest.mark.asyncio
async def test_summary_tail_summarizes_before_lead_in_notes_and_retains_them() -> None:
    messages = _delivered_review(8_000)
    live = _live_request(messages)
    adapter = StubAdapter("SUMMARY: reviewed the first output.")

    result = await compact(
        messages,
        summary_adapter=adapter,
        summary_model_id="gpt-5",
        settings=CompactionSettings(tail_tokens=10),
        request_messages=live,
    )

    assert adapter.requests[0]["messages"][:-1] == live[:-2]
    projection = effective_compaction_messages([result])
    assert str(projection[0].content).startswith(COMPACTION_SUMMARY_NOTE_PREFIX)
    assert [item.id for item in projection[-2:]] == ["n-delivery", "a-reply"]


def _replayed_reasoning() -> tuple[list[ChatMessage], list[dict[str, Any]], list[ChatMessage]]:
    older = assistant("a-old", "old step " * 20)
    recent = assistant("a-new", "new step " * 80)
    messages = [user("u", "do it"), older, recent]
    live = provider_request(messages)
    live[2]["reasoning"] = "retained reasoning " * 4_000
    return messages, live, [older, recent]


def _request_only_tool_media() -> tuple[list[ChatMessage], list[dict[str, Any]], list[ChatMessage]]:
    carrier, result = tool_step("old", "image", arguments={"path": "image.png"})
    recent = assistant("a-new", "image consumed " * 200)
    messages = [user("u", "inspect"), carrier, result, recent]
    live = provider_request(messages)
    live[3]["tool_result_content"] = [
        {"type": "media", "media_type": "image/png", "base64": "A" * 10_000}
    ]
    return messages, live, [carrier, result, recent]


@pytest.mark.parametrize(
    "build", [_replayed_reasoning, _request_only_tool_media], ids=["reasoning", "tool-media"]
)
def test_working_tail_counts_request_only_payload_before_choosing_boundary(build) -> None:
    messages, live, stored_tail = build()
    target = _tail_token_span(stored_tail)
    before = json.dumps(live)

    plan = _plan_working_tail(messages, target, request_messages=tuple(live))

    assert list(_plan_working_tail(messages, target).retained_messages) == stored_tail
    assert list(plan.retained_messages) == [messages[-1]]
    assert json.dumps(live) == before


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
        return 5_000 if any(item.get("reasoning_meta") for item in candidate) else 800

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
    result = await compact(
        messages,
        service=service,
        summary_adapter=adapter,
        summary_model_id="gpt-5",
        active_adapter=adapter,
        active_model_id="gpt-5",
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
    assert (
        adapter.estimate_request_input_tokens(tail, model_id="gpt-5")
        <= budget * TAIL_SOFT_LIMIT_PERCENT // 100
    )
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
    [quote] = user_quotes(effective)
    assert quoted_user(quote) == messages[3].to_dict()
