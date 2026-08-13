"""Tests for the policy-driven compaction engine."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat.messages import (
    COMPACTION_SKILL_NOTE_PREFIX,
    COMPACTION_SUMMARY_NOTE_PREFIX,
    _effective_compaction_messages,
    _embed_notes_into_request,
)
from core.compaction import (
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    CompactionError,
    CompactionInsufficientReclaimError,
    CompactionService,
    CompactionSettings,
    find_tail_boundary,
    is_compacted_tool_result_content,
)
from core.compaction.compaction import (
    COMPACTION_TAIL_GUIDANCE,
    CompactionPlan,
    _plan_working_tail,
    _tail_soft_limit,
    _tail_token_span,
)
from core.sessions.sessions import _skill_context_note_content
from core.tools import tool_success
from core.utils.tokens import NATIVE_MEDIA_TOKEN_RESERVE

TIMESTAMP = "2026-05-19T12:00:00+00:00"


class StubStorage:
    def read_prompt_fragment(self, name: str) -> str:
        assert name == "compaction.md"
        return "Preserve decisions and unfinished work."


class StubAdapter:
    def __init__(self, text: str = "COMPACTED") -> None:
        self.text = text
        self.requests: list[dict[str, Any]] = []

    async def send(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
        self.requests.append({"messages": messages, **kwargs})
        return {"content": self.text}

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        return response


class RetainContextStrategy:
    id = "retain-context"

    def plan(self, context: Any, settings: Any) -> CompactionPlan:
        del settings
        return CompactionPlan(
            model_messages=None,
            model_target="summary",
            summary_text="RETAINED",
            after_summary=tuple(context.messages),
            compacted_token_count=1,
        )


def message(message_id: str, role: str, content: str, **extra: Any) -> ChatMessage:
    return ChatMessage.from_dict(
        {"id": message_id, "timestamp": TIMESTAMP, "role": role, "content": content, **extra}
    )


def user(message_id: str, content: str) -> ChatMessage:
    return message(message_id, "user", content)


def assistant(message_id: str, content: str) -> ChatMessage:
    return message(message_id, "assistant", content, model="openai/gpt-5")


def checkpoint(projection: list[ChatMessage], count: int = 10) -> ChatMessage:
    return ChatMessage.compaction_checkpoint(
        summary="PRIOR",
        projection=projection,
        compacted_token_count=count,
        policy="summary_tail",
        strategy="summary_tail",
    )


def provider_request(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    return [
        {"id": "system-1", "role": "system", "content": "system"},
        *(item.to_dict() for item in messages),
    ]


@pytest.mark.asyncio
async def test_compaction_reports_only_the_immediately_completed_skill_epoch() -> None:
    service = CompactionService(RetainContextStrategy())
    alpha_content = '<skill_content name="alpha">Alpha instructions</skill_content>'
    alpha_note = ChatMessage.note(_skill_context_note_content("alpha", alpha_content))
    first_messages = [
        user("u1", "First task"),
        alpha_note,
        assistant("a1", "First result"),
    ]

    first = await service.compact(
        first_messages,
        agent=object(),
        summary_adapter=StubAdapter(),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="retain-context"),
    )
    first_effective = _effective_compaction_messages([*first_messages, first])
    first_guidance = [
        item
        for item in first_effective
        if item.role == "note"
        and isinstance(item.content, str)
        and item.content.startswith(COMPACTION_SKILL_NOTE_PREFIX)
    ]

    assert len(first_guidance) == 1
    assert '["alpha"]' in str(first_guidance[0].content)
    assert all("Alpha instructions" not in str(item.content) for item in first_effective)

    beta_content = '<skill_content name="beta">Beta instructions</skill_content>'
    beta_note = ChatMessage.note(_skill_context_note_content("beta", beta_content))
    second_messages = [*first_messages, first, beta_note, assistant("a2", "Second result")]
    second = await service.compact(
        second_messages,
        agent=object(),
        summary_adapter=StubAdapter(),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="retain-context"),
    )
    second_effective = _effective_compaction_messages([*second_messages, second])
    second_guidance = [
        item
        for item in second_effective
        if item.role == "note"
        and isinstance(item.content, str)
        and item.content.startswith(COMPACTION_SKILL_NOTE_PREFIX)
    ]

    assert len(second_guidance) == 1
    assert '["beta"]' in str(second_guidance[0].content)
    assert '["alpha"]' not in str(second_guidance[0].content)
    assert all("Beta instructions" not in str(item.content) for item in second_effective)
    rendered_second = _embed_notes_into_request(second_effective)
    assert COMPACTION_SKILL_NOTE_PREFIX not in json.dumps(rendered_second)

    third_messages = [*second_messages, second, assistant("a3", "Third result")]
    third = await service.compact(
        third_messages,
        agent=object(),
        summary_adapter=StubAdapter(),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="retain-context"),
    )
    third_effective = _effective_compaction_messages([*third_messages, third])

    assert all(
        not (
            item.role == "note"
            and isinstance(item.content, str)
            and item.content.startswith(COMPACTION_SKILL_NOTE_PREFIX)
        )
        for item in third_effective
    )


@pytest.mark.asyncio
async def test_compaction_compacts_skill_tool_carrier_without_breaking_its_cycle() -> None:
    skill_result_content = json.dumps(
        tool_success(
            {
                "name": "docx",
                "status": "loaded",
                "content": "Full document instructions",
                "environment_access": "Use DOCX_KEY through bash env_keys.",
            }
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    messages = [
        user("u1", "Create a document"),
        message(
            "a-tools",
            "assistant",
            "",
            model="openai/gpt-5",
            tool_calls=[{"id": "call-skill", "name": "skill", "arguments": {"name": "docx"}}],
        ),
        message(
            "t-skill",
            "tool",
            skill_result_content,
            tool_call_id="call-skill",
            name="skill",
        ),
        assistant("a2", "Used the Skill"),
    ]

    result = await CompactionService(RetainContextStrategy()).compact(
        messages,
        agent=object(),
        summary_adapter=StubAdapter(),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="retain-context"),
    )
    effective = _effective_compaction_messages([*messages, result])
    projected_result = next(item for item in effective if item.id == "t-skill")
    projected_payload = json.loads(str(projected_result.content))

    assert is_compacted_tool_result_content(projected_result.content)
    assert projected_payload["outcome"] == {
        "name": "docx",
        "status": "loaded",
        "compacted": True,
    }
    assert "Full document instructions" not in str(projected_result.content)
    assert "DOCX_KEY" not in str(projected_result.content)


def test_find_tail_boundary_can_split_one_user_turn_at_assistant_boundary() -> None:
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


@pytest.mark.asyncio
async def test_summary_tail_executes_one_call_and_materializes_projection() -> None:
    summary_adapter = StubAdapter("NEW SUMMARY")
    active_adapter = StubAdapter("must not be used")
    messages = [
        user("u1", "old request " * 100),
        assistant("a1", "old response " * 100),
        user("u2", "recent request"),
        assistant("a2", "recent response"),
    ]
    request = provider_request(messages)
    tools = [{"name": "read", "description": "Read a file", "parameters": {}}]

    result = await CompactionService().compact(
        messages,
        agent=object(),
        summary_adapter=summary_adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=10),
        request_messages=request,
        active_adapter=active_adapter,
        active_model_id="openai/active",
        active_tools=tools,
    )

    assert len(summary_adapter.requests) == 1
    assert active_adapter.requests == []
    assert summary_adapter.requests[0]["model_id"] == "openai/summary"
    assert summary_adapter.requests[0]["messages"][:-1] == request[:4]
    assert summary_adapter.requests[0]["tools"] == tools
    assert "Compact" not in summary_adapter.requests[0]["messages"][1]["content"]
    assert summary_adapter.requests[0]["messages"][-1]["content"] == (
        "Preserve decisions and unfinished work."
    )
    effective = _effective_compaction_messages([*messages, result])
    assert effective[0].role == "note"
    assert effective[0].content == f"{COMPACTION_SUMMARY_NOTE_PREFIX}NEW SUMMARY"
    assert effective[1].role == "note"
    assert effective[1].content == COMPACTION_TAIL_GUIDANCE
    assert [item.id for item in effective[2:]] == ["u2", "a2"]
    request_projection = _embed_notes_into_request(effective)
    assert COMPACTION_TAIL_GUIDANCE in request_projection[0]["content"]
    assert request_projection[1]["content"] == "recent request"


@pytest.mark.asyncio
async def test_compaction_keeps_sync_transforms_off_loop_and_model_io_on_loop() -> None:
    loop_thread = threading.get_ident()
    strategy_threads: list[int] = []
    send_threads: list[int] = []
    normalize_threads: list[int] = []

    class RecordingStrategy:
        id = "recording"

        def plan(self, context: Any, settings: Any) -> CompactionPlan:
            strategy_threads.append(threading.get_ident())
            return CompactionPlan(
                model_messages=({"role": "user", "content": "compact"},),
                model_target="summary",
                compacted_token_count=1,
            )

    class RecordingAdapter:
        async def send(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
            send_threads.append(threading.get_ident())
            return {"content": "summary"}

        def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
            normalize_threads.append(threading.get_ident())
            return response

    adapter = RecordingAdapter()
    await CompactionService(RecordingStrategy()).compact(
        [user("u1", "old context")],
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="recording"),
    )

    assert strategy_threads and strategy_threads != [loop_thread]
    assert send_threads == [loop_thread]
    assert normalize_threads and normalize_threads != [loop_thread]


@pytest.mark.asyncio
async def test_summary_tail_preserves_exact_active_model_prefix_with_reasoning() -> None:
    adapter = StubAdapter("NEW SUMMARY")
    messages = [
        user("u1", "old request " * 100),
        assistant("a1", "old response " * 100),
        user("u2", "recent request"),
        assistant("a2", "recent response"),
    ]
    request = provider_request(messages)
    request[2]["reasoning"] = "provider-readable"
    request[2]["reasoning_meta"] = {"signature": "provider-opaque"}

    await CompactionService().compact(
        messages,
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="gpt-5",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=10),
        request_messages=request,
        active_adapter=adapter,
        active_model_id="gpt-5",
    )

    assert adapter.requests[0]["messages"][:-1] == request[:4]


@pytest.mark.asyncio
async def test_custom_summary_model_drops_active_provider_reasoning_state() -> None:
    summary_adapter = StubAdapter("NEW SUMMARY")
    active_adapter = StubAdapter("must not be used")
    messages = [
        user("u1", "old request " * 100),
        assistant("a1", "old response " * 100),
        user("u2", "recent request"),
        assistant("a2", "recent response"),
    ]
    request = provider_request(messages)
    request[2]["reasoning"] = "provider-readable"
    request[2]["reasoning_meta"] = {"signature": "provider-opaque"}

    await CompactionService().compact(
        messages,
        agent=object(),
        summary_adapter=summary_adapter,
        summary_model_id="claude-summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=10),
        request_messages=request,
        active_adapter=active_adapter,
        active_model_id="gpt-5",
    )

    sent_head = summary_adapter.requests[0]["messages"][:-1]
    assert [message["id"] for message in sent_head] == ["system-1", "u1", "a1", "u2"]
    assert sent_head[2]["content"] == request[2]["content"]
    assert "reasoning" not in sent_head[2]
    assert "reasoning_meta" not in sent_head[2]


@pytest.mark.asyncio
async def test_next_compaction_consumes_previous_projection_not_hidden_history() -> None:
    adapter = StubAdapter("NEXT")
    prior = checkpoint(
        [ChatMessage.note(f"{COMPACTION_SUMMARY_NOTE_PREFIX}PRIOR"), user("u2", "kept")]
    )
    hidden = user("u1", "hidden-secret-marker")

    latest = assistant("a2", "new")
    request = [
        {"id": "system-1", "role": "system", "content": "system"},
        {
            "role": "user",
            "content": f"{COMPACTION_SUMMARY_NOTE_PREFIX}PRIOR",
        },
        {"id": "u2", "role": "user", "content": "kept"},
        latest.to_dict(),
    ]

    await CompactionService().compact(
        [hidden, prior, latest],
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
        request_messages=request,
    )

    compact_request = adapter.requests[0]["messages"]
    assert compact_request[:-1] == request[:-1]
    assert "hidden-secret-marker" not in str(compact_request)
    assert str(compact_request).count(COMPACTION_SUMMARY_NOTE_PREFIX + "PRIOR") == 1
    assert "<previous_summary>" not in str(compact_request)


def test_summary_tail_auto_compaction_advances_inside_retained_user_turn() -> None:
    current_user = user("u1", "One long-running user turn")
    old_carrier = message(
        "a1",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-1", "name": "read", "arguments": {"path": "one"}}],
    )
    old_result = message(
        "t1",
        "tool",
        "result one",
        tool_call_id="call-1",
        name="read",
    )
    prior = checkpoint([current_user, old_carrier, old_result])
    next_carrier = message(
        "a2",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-2", "name": "edit", "arguments": {"path": "one"}}],
    )
    next_result = message(
        "t2",
        "tool",
        "result two",
        tool_call_id="call-2",
        name="edit",
    )

    can_compact = CompactionService().has_new_compactable_context(
        [current_user, old_carrier, old_result, prior, next_carrier, next_result],
        CompactionSettings(tail_tokens=1),
    )

    assert can_compact is True


def test_summary_tail_auto_compaction_waits_when_only_prior_summary_is_in_head() -> None:
    retained_user = user("u1", "Previously retained turn " * 100)
    prior = checkpoint([ChatMessage.note(COMPACTION_TAIL_GUIDANCE), retained_user])

    can_compact = CompactionService().has_new_compactable_context(
        [retained_user, prior],
        CompactionSettings(tail_tokens=1),
    )

    assert can_compact is False


def test_working_tail_fills_backward_across_active_user_anchor() -> None:
    messages = [
        assistant("a-before", "Useful work before the latest instruction. " * 200),
        user("u-active", "Finish the same task with these final constraints."),
        assistant("a-after", "I am applying those constraints now."),
    ]
    target = _tail_token_span(messages)

    plan = _plan_working_tail(messages, target)

    assert plan.boundary_id == "a-before"
    assert plan.pinned_user is None
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
    assert _tail_token_span(retained) > _tail_soft_limit(10)
    assert retained[1].tool_calls == active_carrier.tool_calls
    assert retained[2].content == active_result_content


def test_working_tail_compacts_consumed_tools_only_under_budget_pressure() -> None:
    active_user = user("u-active", "Keep implementing the task.")
    old_arguments = {"path": "old.txt", "query": "Q" * 20_000}
    old_carrier = message(
        "a-old",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-old", "name": "read", "arguments": old_arguments}],
    )
    old_result = message(
        "t-old",
        "tool",
        "old-output-" * 20_000,
        tool_call_id="call-old",
        name="read",
    )
    active_carrier = message(
        "a-active",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-active", "name": "edit", "arguments": {"path": "latest.txt"}}],
    )
    active_result = message(
        "t-active",
        "tool",
        "latest result",
        tool_call_id="call-active",
        name="edit",
    )
    messages = [active_user, old_carrier, old_result, active_carrier, active_result]
    original_snapshot = [item.to_dict() for item in messages]

    plan = _plan_working_tail(messages, tail_tokens=2_000)

    retained_by_id = {item.id: item for item in plan.retained_messages}
    compacted_result_content = retained_by_id["t-old"].content
    assert isinstance(compacted_result_content, str)
    compacted_result = json.loads(compacted_result_content)
    assert plan.boundary_id == "u-active"
    assert retained_by_id["u-active"].content == active_user.content
    assert _tail_token_span(plan.retained_messages) <= _tail_soft_limit(2_000)
    assert retained_by_id["a-old"].tool_calls != old_carrier.tool_calls
    assert is_compacted_tool_result_content(retained_by_id["t-old"].content)
    assert compacted_result["message_id"] == "t-old"
    assert retained_by_id["a-active"].tool_calls == active_carrier.tool_calls
    assert retained_by_id["t-active"].content == active_result.content
    assert plan.payload_reclaim_tokens >= MIN_AUTO_COMPACTION_RECLAIM_TOKENS
    assert [item.to_dict() for item in messages] == original_snapshot


@pytest.mark.asyncio
async def test_active_user_survives_repeated_compactions_unchanged() -> None:
    adapter = StubAdapter("FIRST")
    active_user = user("u-active", "Complete the whole task; do not stop after one checkpoint.")
    messages = [
        active_user,
        assistant("a-old", "Earlier implementation work. " * 1_000),
        assistant("a-latest", "Continuing with the next implementation step."),
    ]
    service = CompactionService()

    first = await service.compact(
        messages,
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=10),
        request_messages=provider_request(messages),
    )
    after_first = _effective_compaction_messages([*messages, first])
    continued = assistant("a-next", "Working beyond the first checkpoint.")
    adapter.text = "SECOND"

    second = await service.compact(
        [*messages, first, continued],
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=10),
        request_messages=provider_request([*after_first, continued]),
    )
    after_second = _effective_compaction_messages([*messages, first, continued, second])

    retained_users = [item for item in after_second if item.role == "user"]
    assert len(retained_users) == 1
    assert retained_users[0].id == active_user.id
    assert retained_users[0].content == active_user.content


@pytest.mark.asyncio
async def test_summary_tail_compacts_consumed_tool_batch_without_rewriting_request() -> None:
    adapter = StubAdapter("TOOL SUMMARY")
    old_arguments = {"path": "old.txt", "query": "Q" * 8_000}
    old_result_content = "sensitive-output-" * 5_000
    old_carrier = message(
        "a-old",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-old", "name": "read", "arguments": old_arguments}],
    )
    old_result = message(
        "t-old",
        "tool",
        old_result_content,
        tool_call_id="call-old",
        name="read",
    )
    latest_carrier = message(
        "a-latest",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-latest", "name": "edit", "arguments": {"path": "latest.txt"}}],
    )
    latest_result = message(
        "t-latest",
        "tool",
        "latest result",
        tool_call_id="call-latest",
        name="edit",
    )
    messages = [
        user("u1", "Keep working until the task is complete"),
        old_carrier,
        old_result,
        latest_carrier,
        latest_result,
    ]
    request = provider_request(messages)
    original_snapshot = [item.to_dict() for item in messages]
    service = CompactionService()

    assert service.has_new_compactable_context(
        messages,
        CompactionSettings(tail_tokens=1),
    )

    result = await service.compact(
        messages,
        agent=object(),
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
        request_messages=request,
        minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    )

    compact_request = adapter.requests[0]["messages"]
    assert compact_request[:-1] == request[:-2]
    assert compact_request[-2]["content"] == old_result_content
    assert compact_request[-3]["tool_calls"][0]["arguments"] == old_arguments
    assert [item.to_dict() for item in messages] == original_snapshot
    effective = _effective_compaction_messages([*messages, result])
    assert [item.id for item in effective[2:]] == ["u1", "a-latest", "t-latest"]
    assert all(item.id not in {"a-old", "t-old"} for item in effective)


@pytest.mark.asyncio
async def test_summary_tail_requires_active_request_context() -> None:
    with pytest.raises(CompactionError):
        await CompactionService().compact(
            [user("u1", "old"), assistant("a1", "tail")],
            agent=object(),
            summary_adapter=StubAdapter(),
            summary_model_id="openai/summary",
            storage=StubStorage(),
            settings=CompactionSettings(tail_tokens=1),
        )


@pytest.mark.asyncio
async def test_automatic_compaction_rejects_projection_below_minimum_reclaim() -> None:
    adapter = StubAdapter("A summary that is intentionally much larger " * 500)
    messages = [
        user("u1", "old"),
        assistant("a1", "short"),
        user("u2", "tail"),
    ]

    with pytest.raises(
        CompactionInsufficientReclaimError,
    ):
        await CompactionService().compact(
            messages,
            agent=object(),
            summary_adapter=adapter,
            summary_model_id="openai/summary",
            storage=StubStorage(),
            settings=CompactionSettings(tail_tokens=1),
            request_messages=provider_request(messages),
            minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
        )

    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_compaction_engine_leaves_context_projection_for_chat() -> None:
    service = CompactionService()
    messages = [
        user("u1", "old context " * 2_000),
        assistant("a1", "old answer " * 2_000),
        user("u2", "tail"),
        assistant("a2", "tail answer"),
    ]

    result = await service.compact(
        messages,
        agent=object(),
        summary_adapter=StubAdapter("Short retained summary."),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
        request_messages=provider_request(messages),
    )

    assert result.usage is not None
    assert set(result.usage) == {"compacted_token_count"}


@pytest.mark.asyncio
async def test_continuation_preserves_request_prefix_and_active_tools() -> None:
    active = StubAdapter("ACTIVE SUMMARY")
    summary = StubAdapter("must not be used")
    request = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer"},
    ]
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]

    result = await CompactionService().compact(
        [user("u1", "hello"), assistant("a1", "answer")],
        agent=object(),
        summary_adapter=summary,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="continuation"),
        request_messages=request,
        active_adapter=active,
        active_model_id="openai/active",
        active_tools=tools,
    )

    assert summary.requests == []
    assert len(active.requests) == 1
    assert active.requests[0]["messages"][:-1] == request
    assert "checkpoint" in active.requests[0]["messages"][-1]["content"]
    assert active.requests[0]["tools"] == tools
    assert result.projection is not None
    assert len(result.projection) == 1


@pytest.mark.asyncio
async def test_continuation_requires_active_request_and_target() -> None:
    with pytest.raises(CompactionError):
        await CompactionService().compact(
            [user("u1", "hello")],
            agent=object(),
            summary_adapter=StubAdapter(),
            summary_model_id="openai/summary",
            storage=StubStorage(),
            settings=CompactionSettings(strategy="continuation"),
        )


def test_checkpoint_round_trip_contains_projection_and_provenance() -> None:
    original = checkpoint([user("u2", "tail")])
    restored = ChatMessage.from_dict(original.to_dict())

    assert restored.role == "compaction_checkpoint"
    assert restored.projection is not None
    assert [entry["role"] for entry in restored.projection] == ["note", "user"]
    assert restored.compaction_policy == "summary_tail"
    assert restored.compaction_strategy == "summary_tail"


def test_legacy_checkpoint_is_read_only_input_to_the_new_projection_engine() -> None:
    old = user("u1", "hidden")
    tail = user("u2", "kept tail")
    legacy = ChatMessage.from_dict(
        {
            "id": "c1",
            "timestamp": TIMESTAMP,
            "role": "compaction_checkpoint",
            "content": "old checkpoint summary",
            "tail_boundary_id": "u2",
            "usage": {"compacted_token_count": 40},
        }
    )
    newer = assistant("a2", "new response")

    effective = _effective_compaction_messages([old, tail, legacy, newer])

    assert [message.role for message in effective] == ["note", "user", "assistant"]
    assert effective[0].content == (f"{COMPACTION_SUMMARY_NOTE_PREFIX}old checkpoint summary")
    assert effective[1:] == [tail, newer]
    assert legacy.to_dict()["tail_boundary_id"] == "u2"
