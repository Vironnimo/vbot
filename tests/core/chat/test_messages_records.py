"""Canonical Message records: construction, JSON round trip and validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from typing import Any

import pytest

from core.chat import ChatMessage, ChatMessageValidationError, MessageSender, ToolCall
from core.chat.content_blocks import FileBlock, TextBlock
from core.chat.messages import (
    ERROR_KIND_AUTH,
    ERROR_KIND_CONFIG,
    ERROR_KIND_NETWORK,
    ERROR_KIND_PROVIDER_ERROR,
    ERROR_KIND_PROVIDER_FATAL,
    ERROR_KIND_PROVIDER_OVERLOAD,
    ERROR_KIND_RATE_LIMIT,
    ERROR_KIND_TIMEOUT,
    ERROR_KIND_TOOL_ITERATIONS,
    ToolCallRejection,
    error_kind_llm_visible,
)
from core.chat.output_files import AssistantFileReference
from tests.core.chat.messages_test_support import FIXED_TIMESTAMP, FIXED_TIMING

_STAMP = "2026-05-03T14:30:00.000000Z"
_WEATHER_CALL = ToolCall(id="call_abc", name="get_weather", arguments={"city": "Berlin"})
_CHANGE_STATS = {
    "files": 2,
    "added": 5,
    "removed": 1,
    "paths": ["a.txt", "b.txt"],
    "file_stats": [
        {"path": "a.txt", "added": 4, "removed": 0},
        {"path": "b.txt", "added": 1, "removed": 1},
    ],
}


def test_tool_call_round_trips_rejections_and_argument_sequences() -> None:
    rejected = ToolCall(
        id="call_bad",
        name="write",
        arguments={},
        rejection=ToolCallRejection(
            code="malformed_tool_arguments",
            message="Arguments were malformed.",
            fingerprint="sha256",
        ),
    )
    batched = ToolCall(
        id="call_batch",
        name="bash",
        arguments={"command": "echo one"},
        argument_sequence_index=0,
        argument_sequence_length=2,
    )

    assert _WEATHER_CALL.to_dict() == {
        "id": "call_abc",
        "name": "get_weather",
        "arguments": {"city": "Berlin"},
    }
    for call in (_WEATHER_CALL, rejected, batched):
        assert ToolCall.from_dict(call.to_dict()) == call
    with pytest.raises(FrozenInstanceError):
        _WEATHER_CALL.name = "changed"  # type: ignore[misc]
    with pytest.raises(ChatMessageValidationError):
        ToolCall.from_dict({"id": "call_abc", "name": "get_weather", "arguments": []})
    with pytest.raises(ChatMessageValidationError):
        ToolCall(id="call_batch", name="bash", argument_sequence_index=0)


def test_message_sender_round_trips_and_reads_a_legacy_sender_as_member() -> None:
    member = MessageSender(id="50", display_name="Alice")
    admin = MessageSender(id="50", display_name="Alice", role="admin")

    assert member.to_dict() == {"id": "50", "display_name": "Alice", "role": "member"}
    assert MessageSender.from_dict(admin.to_dict()) == admin
    assert MessageSender.from_dict({"id": "50", "display_name": "Alice"}) == member
    with pytest.raises(FrozenInstanceError):
        member.display_name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "50", "display_name": "Alice", "role": "owner"},
        {"id": None, "display_name": "Alice"},
        {"id": "", "display_name": "Alice"},
        {"id": 50, "display_name": "Alice"},
        {"id": "50", "display_name": ""},
        {"id": "50", "display_name": ["Alice"]},
    ],
    ids=["unknown-role", "no-id", "empty-id", "int-id", "empty-name", "list-name"],
)
def test_message_sender_rejects_invalid_fields(payload: dict[str, Any]) -> None:
    with pytest.raises(ChatMessageValidationError):
        MessageSender.from_dict(payload)


_FACTORY_SHAPES: list[tuple[str, Callable[[], ChatMessage], dict[str, Any]]] = [
    (
        "system",
        lambda: ChatMessage.system(
            "You are an agent for vBot.", "anthropic/claude-sonnet-4", timestamp=FIXED_TIMESTAMP
        ),
        {
            "role": "system",
            "model": "anthropic/claude-sonnet-4",
            "content": "You are an agent for vBot.",
        },
    ),
    (
        "user",
        lambda: ChatMessage.user("What's the weather in Berlin?", timestamp=FIXED_TIMESTAMP),
        {"role": "user", "content": "What's the weather in Berlin?"},
    ),
    (
        "user-sender",
        lambda: ChatMessage.user(
            "Hello from the group.",
            sender=MessageSender(id="50", display_name="Alice"),
            timestamp=FIXED_TIMESTAMP,
        ),
        {
            "role": "user",
            "content": "Hello from the group.",
            "sender": {"id": "50", "display_name": "Alice", "role": "member"},
        },
    ),
    (
        "user-blocks",
        lambda: ChatMessage.user(
            [
                TextBlock(type="text", text="Please review the document."),
                FileBlock(
                    type="file",
                    attachment_id="att_123",
                    filename="report.pdf",
                    media_type="application/pdf",
                ),
            ],
            timestamp=FIXED_TIMESTAMP,
        ),
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Please review the document."},
                {
                    "type": "file",
                    "attachment_id": "att_123",
                    "filename": "report.pdf",
                    "media_type": "application/pdf",
                },
            ],
        },
    ),
    (
        "note",
        lambda: ChatMessage.note("Background task completed.", timestamp=FIXED_TIMESTAMP),
        {"role": "note", "content": "Background task completed."},
    ),
    (
        "error",
        lambda: ChatMessage.error(
            ERROR_KIND_RATE_LIMIT, "Provider rate limit exceeded.", timestamp=FIXED_TIMESTAMP
        ),
        {"role": "error", "content": "Provider rate limit exceeded.", "error_kind": "rate_limit"},
    ),
    (
        "assistant-tool-calls",
        lambda: ChatMessage.assistant(
            model="anthropic/claude-sonnet-4",
            content=None,
            reasoning="I need to call the weather tool.",
            reasoning_meta={"signature": "opaque"},
            tool_calls=[_WEATHER_CALL],
            timestamp=FIXED_TIMESTAMP,
        ),
        {
            "role": "assistant",
            "model": "anthropic/claude-sonnet-4",
            "reasoning": "I need to call the weather tool.",
            "reasoning_meta": {"signature": "opaque"},
            "tool_calls": [
                {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
            ],
        },
    ),
    (
        "tool",
        lambda: ChatMessage.tool(
            tool_call_id="call_abc",
            name="get_weather",
            content='{"temp":22,"condition":"sunny"}',
            timestamp=FIXED_TIMESTAMP,
        ),
        {
            "role": "tool",
            "content": '{"temp":22,"condition":"sunny"}',
            "tool_call_id": "call_abc",
            "name": "get_weather",
        },
    ),
    (
        "run-summary",
        lambda: ChatMessage.run_summary(
            run_id="run-one",
            work_id="sub-work-one",
            status="completed",
            timing=FIXED_TIMING,
            iteration_count=3,
            change_stats=_CHANGE_STATS,
            timestamp=FIXED_TIMESTAMP,
        ),
        {
            "role": "run_summary",
            "timing": FIXED_TIMING,
            "run_id": "run-one",
            "work_id": "sub-work-one",
            "status": "completed",
            "iteration_count": 3,
            "change_stats": _CHANGE_STATS,
        },
    ),
    (
        "agent-takeover",
        lambda: ChatMessage.agent_takeover(
            from_address="assistant", to_address="builder@vbot", timestamp=FIXED_TIMESTAMP
        ),
        {"role": "agent_takeover", "content": '{"from":"assistant","to":"builder@vbot"}'},
    ),
    (
        "history-edit",
        lambda: ChatMessage.history_edit("user-one", timestamp=FIXED_TIMESTAMP),
        {"role": "history_edit", "target_message_id": "user-one"},
    ),
]


@pytest.mark.parametrize(
    ("build", "shape"),
    [(build, shape) for _, build, shape in _FACTORY_SHAPES],
    ids=[name for name, _, _ in _FACTORY_SHAPES],
)
def test_factories_persist_only_their_role_fields_and_round_trip(
    build: Callable[[], ChatMessage], shape: dict[str, Any]
) -> None:
    message = build()

    assert message.to_dict() == {"id": message.id, "timestamp": _STAMP, **shape}
    assert ChatMessage.from_dict(message.to_dict()) == message


@pytest.mark.parametrize(
    "data",
    [
        {
            "id": "g7h8i9",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "assistant",
            "model": "openai/gpt-4.1",
            "content": "The weather is sunny.",
            "reasoning_meta": {"signature": "opaque"},
            "usage": {"input_tokens": 200, "output_tokens": 30},
        },
        {
            "id": "summary-one",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "run_summary",
            "run_id": "run-one",
            "work_id": "sub-work-one",
            "status": "completed",
            "timing": FIXED_TIMING,
            "change_stats": {"files": 1, "added": 2, "removed": 0, "paths": ["a.txt"]},
        },
        {
            "id": "error_unknown",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "role": "error",
            "content": "Future error kind.",
            "error_kind": "future_kind",
        },
        {"id": "d4e5f6", "timestamp": "2026-05-03T14:30:01Z", "role": "user", "content": "Hello"},
        {
            "id": "takeover-one",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "agent_takeover",
            "content": '{"from":"planner@acme","to":"assistant"}',
        },
        {
            "id": "reasoning-only",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "assistant",
            "model": "openai/gpt-4.1",
            "reasoning": "Thinking only.",
        },
        {
            "id": "reasoning-meta-only",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "assistant",
            "model": "openai/gpt-4.1",
            "reasoning_meta": {"provider": "opaque"},
        },
        {
            "id": "checkpoint-one",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "compaction_checkpoint",
            "content": "Summary.",
            "projection": [
                {
                    "id": "u2",
                    "timestamp": "2026-05-03T14:30:04+00:00",
                    "role": "user",
                    "content": "tail",
                }
            ],
            "compaction_policy": "summary_tail",
            "compaction_strategy": "summary_tail",
            "usage": {"compacted_token_count": 10},
        },
    ],
    ids=[
        "assistant-usage",
        "run-summary",
        "unknown-error-kind",
        "z-timestamp",
        "takeover",
        "assistant-reasoning-only",
        "assistant-reasoning-meta-only",
        "checkpoint",
    ],
)
def test_persisted_payloads_round_trip_exactly(data: dict[str, Any]) -> None:
    assert ChatMessage.from_dict(data).to_dict() == data


_DISPLAY = {"version": 1, "primary": [{"kind": "path", "value": "src/app.py"}], "facts": []}


@pytest.mark.parametrize(
    ("message", "persisted"),
    [
        (
            ChatMessage.assistant(
                model="openai/gpt-4.1",
                content="Partial answer",
                interrupted=True,
                interruption_cause="timeout",
            ),
            {"interrupted": True, "interruption_cause": "timeout"},
        ),
        (
            ChatMessage.assistant(
                model="openai/gpt-5.5",
                content="I will inspect this.",
                reasoning="Working.",
                reasoning_scope="openai/gpt-5.5::api-key",
                phase="commentary",
            ),
            {"phase": "commentary", "reasoning_scope": "openai/gpt-5.5::api-key"},
        ),
        (
            ChatMessage.assistant(
                model="openai/gpt-4.1",
                content="Answer",
                reasoning="Thought",
                reasoning_timing=FIXED_TIMING,
            ),
            {"reasoning_timing": FIXED_TIMING},
        ),
        (
            ChatMessage.assistant(
                model="openai/gpt-4.1",
                content="Hi",
                usage={
                    "input_tokens": 150,
                    "output_tokens": 12,
                    "cache_write_tokens": 20,
                    "reasoning_tokens": 7,
                    "output_tokens_estimated": True,
                    "estimated": True,
                },
            ),
            {
                "usage": {
                    "input_tokens": 150,
                    "output_tokens": 12,
                    "cache_write_tokens": 20,
                    "reasoning_tokens": 7,
                    "output_tokens_estimated": True,
                    "estimated": True,
                }
            },
        ),
        (
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content="Chart: file:C:\\work\\chart.png",
                output_files=[
                    AssistantFileReference(
                        line_index=0, path="C:\\work\\chart.png", start_index=7, end_index=29
                    )
                ],
            ),
            {
                "output_files": [
                    {
                        "line_index": 0,
                        "path": "C:\\work\\chart.png",
                        "start_index": 7,
                        "end_index": 29,
                    }
                ]
            },
        ),
        (
            replace(ChatMessage.assistant(model="test/model", content="Answer"), run_id="run-one"),
            {"run_id": "run-one"},
        ),
        (
            ChatMessage.tool(
                tool_call_id="call_abc",
                name="read",
                content='{"ok":true}',
                timing=FIXED_TIMING,
                tool_display=_DISPLAY,
            ),
            {"timing": FIXED_TIMING, "tool_display": _DISPLAY},
        ),
    ],
    ids=[
        "interrupted",
        "phase-scope",
        "reasoning-timing",
        "usage",
        "output-files",
        "run-id",
        "tool",
    ],
)
def test_optional_internal_fields_persist_and_round_trip(
    message: ChatMessage, persisted: dict[str, Any]
) -> None:
    serialized = message.to_dict()

    assert {key: serialized.get(key) for key in persisted} == persisted
    assert ChatMessage.from_dict(serialized) == message


def test_absent_optional_fields_are_omitted_and_unknown_fields_ignored() -> None:
    answer = ChatMessage.assistant(model="openai/gpt-4.1", content="Complete answer")
    user = ChatMessage.user("Hello")
    serialized = answer.to_dict()

    assert answer.usage is None and answer.interrupted is False and user.sender is None
    for key in ("usage", "interrupted", "interruption_cause", "reasoning_timing", "phase"):
        assert key not in serialized
    assert "sender" not in user.to_dict()
    future = {**user.to_dict(), "future_field": "ignored"}
    assert "future_field" not in ChatMessage.from_dict(future).to_dict()


def _payload(role: str, **fields: Any) -> dict[str, Any]:
    return {"id": f"msg-{role}", "timestamp": "2026-05-03T14:30:01+00:00", "role": role, **fields}


_SENDER = {"id": "50", "display_name": "Alice"}
_ASSISTANT = {"model": "openai/gpt-4.1", "content": "Result."}
_TOOL = {"tool_call_id": "call_abc", "name": "get_weather", "content": "{}"}
_SUMMARY = {"run_id": "run-one", "status": "completed", "timing": FIXED_TIMING}
_USAGE = {"input_tokens": 10, "output_tokens": 0}
_BLOCKS = [{"type": "text", "text": "x"}]
_CHECKPOINT = {
    "content": "Summary.",
    "projection": [],
    "compaction_policy": "custom",
    "compaction_strategy": "custom",
}


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (_payload("developer", content="Hello"), None),
        (_payload("user", content="Hello", model="openai/gpt-5.2"), None),
        (_payload("user", content=[]), None),
        (_payload("system", model="openai/gpt-4.1", content=_BLOCKS), None),
        (_payload("assistant", model="openai/gpt-4.1", content=_BLOCKS), None),
        (_payload("tool", **{**_TOOL, "content": _BLOCKS}), None),
        (_payload("note", content=_BLOCKS), None),
        (_payload("error", content=_BLOCKS, error_kind="provider_error"), None),
        (_payload("tool", name="get_weather", content="{}"), None),
        (_payload("assistant", **_ASSISTANT, usage="not a dict"), None),
        (_payload("assistant", **_ASSISTANT, usage=[1, 2, 3]), None),
        (_payload("user", content="Hello", usage=_USAGE), None),
        (_payload("tool", **_TOOL, usage=_USAGE), None),
        (_payload("note"), None),
        (_payload("error", error_kind="provider_error"), None),
        (_payload("error", content="Provider failed."), None),
        (_payload("error", content="Provider failed.", error_kind=""), None),
        (_payload("user", content="Hello", sender="Alice|50"), None),
        (_payload("user", content="Hello", sender={"display_name": "Alice"}), None),
        (_payload("assistant", **_ASSISTANT, sender=_SENDER), None),
        (_payload("run_summary", **_SUMMARY, sender=_SENDER), None),
        (_payload("compaction_checkpoint", **_CHECKPOINT, sender=_SENDER), None),
        (
            _payload(
                "compaction_checkpoint",
                **{key: value for key, value in _CHECKPOINT.items() if key != "projection"},
            ),
            "require a projection",
        ),
        (
            _payload(
                "run_summary",
                **_SUMMARY,
                change_stats={"files": -1, "added": 1, "removed": 0, "paths": []},
            ),
            None,
        ),
        (
            _payload(
                "run_summary",
                **_SUMMARY,
                change_stats={
                    "files": 1,
                    "added": 1,
                    "removed": 0,
                    "paths": ["a.txt"],
                    "file_stats": [{"path": "b.txt", "added": 1, "removed": 0}],
                },
            ),
            None,
        ),
        (_payload("run_summary", **_SUMMARY, iteration_count=-1), None),
        (_payload("run_summary", **_SUMMARY, iteration_count=True), None),
        (_payload("run_summary", **_SUMMARY, iteration_count="1"), None),
        (
            _payload("run_summary", **{**_SUMMARY, "timing": {**FIXED_TIMING, "duration_ms": -1}}),
            None,
        ),
        (_payload("user", content="hi", phase="commentary"), None),
        (_payload("user", content="hi", interrupted=True), None),
        (_payload("assistant", **_ASSISTANT, interrupted="yes"), None),
        (_payload("assistant", **_ASSISTANT, interruption_cause="timeout"), None),
        (_payload("assistant", **_ASSISTANT, interrupted=True, interruption_cause="mystery"), None),
        (
            _payload("assistant", **_ASSISTANT, usage={**_USAGE, "estimated": True}),
            "usage.estimated",
        ),
        (
            _payload("assistant", **_ASSISTANT, usage={**_USAGE, "estimated": False}),
            "usage.estimated",
        ),
        (
            _payload("assistant", **_ASSISTANT, usage={**_USAGE, "input_tokens_estimated": True}),
            "requires estimated: true",
        ),
        (_payload("agent_takeover"), None),
        (_payload("agent_takeover", content=""), None),
        (_payload("history_edit"), "target_message_id"),
        (_payload("history_edit", target_message_id="user-one", content="hidden"), "content"),
        (_payload("assistant", model="openai/gpt-4.1"), "require content, reasoning"),
    ],
    ids=[
        "unknown-role",
        "user-model",
        "user-empty-blocks",
        "system-blocks",
        "assistant-blocks",
        "tool-blocks",
        "note-blocks",
        "error-blocks",
        "tool-without-call-id",
        "usage-string",
        "usage-array",
        "usage-on-user",
        "usage-on-tool",
        "note-without-content",
        "error-without-content",
        "error-without-kind",
        "error-empty-kind",
        "sender-string",
        "sender-without-id",
        "sender-on-assistant",
        "sender-on-run-summary",
        "sender-on-checkpoint",
        "checkpoint-without-projection",
        "negative-change-stats",
        "file-stats-not-matching-paths",
        "negative-iteration-count",
        "bool-iteration-count",
        "string-iteration-count",
        "negative-duration",
        "phase-on-user",
        "interrupted-on-user",
        "interrupted-not-bool",
        "cause-without-interrupted",
        "unknown-cause",
        "estimated-without-field-provenance",
        "estimated-false",
        "field-provenance-without-summary",
        "takeover-without-content",
        "takeover-empty-content",
        "edit-without-target",
        "edit-with-content",
        "assistant-without-output",
    ],
)
def test_invalid_persisted_payloads_are_rejected(data: dict[str, Any], match: str | None) -> None:
    with pytest.raises(ChatMessageValidationError, match=match):
        ChatMessage.from_dict(data)


@pytest.mark.parametrize(
    ("role", "base", "field", "value"),
    [
        ("note", {"content": "Done."}, "model", "openai/gpt-4.1"),
        (
            "note",
            {"content": "Done."},
            "tool_calls",
            [{"id": "c", "name": "read", "arguments": {}}],
        ),
        ("error", {"content": "Failed.", "error_kind": "provider_error"}, "reasoning_meta", {}),
        ("error", {"content": "Failed.", "error_kind": "provider_error"}, "usage", _USAGE),
        ("agent_takeover", {"content": '{"from":"a","to":"b"}'}, "name", "get_weather"),
        ("agent_takeover", {"content": '{"from":"a","to":"b"}'}, "status", "completed"),
        ("agent_takeover", {"content": '{"from":"a","to":"b"}'}, "projection", []),
    ],
)
def test_kernel_roles_reject_model_facing_fields(
    role: str, base: dict[str, Any], field: str, value: object
) -> None:
    with pytest.raises(ChatMessageValidationError, match=field):
        ChatMessage.from_dict(_payload(role, **base, **{field: value}))


@pytest.mark.parametrize(
    "span",
    [{}, {"start_index": 7, "end_index": 7}, {"start_index": True, "end_index": 29}],
    ids=["missing", "empty", "bool"],
)
def test_assistant_output_files_require_a_marker_span(span: dict[str, Any]) -> None:
    data = ChatMessage.assistant(model="openai/gpt-5.2", content="Chart: file:chart.png").to_dict()
    data["output_files"] = [{"line_index": 0, "path": "chart.png", **span}]

    with pytest.raises(ChatMessageValidationError, match="start_index and end_index"):
        ChatMessage.from_dict(data)


_BAD_TIMING = {"started_at": "no-offset", "completed_at": "x", "duration_ms": 1}


@pytest.mark.parametrize(
    "build",
    [
        lambda: ChatMessage.assistant(model="m/x", content="Checking.", phase=1).to_dict(),  # type: ignore[arg-type]
        lambda: ChatMessage.assistant(
            model="m/x", content="Answer", reasoning_timing=FIXED_TIMING
        ).to_dict(),
        lambda: ChatMessage.assistant(
            model="m/x", content="Answer", reasoning="Thought", reasoning_timing=_BAD_TIMING
        ).to_dict(),
        lambda: ChatMessage(
            id="user-1",
            timestamp="2026-05-03T14:30:00+00:00",
            role="user",
            content="hello",
            reasoning_timing=FIXED_TIMING,
        ).to_dict(),
        lambda: ChatMessage(
            id="user-1",
            timestamp="2026-05-03T14:30:00+00:00",
            role="user",
            content="hello",
            tool_display={"version": 1},
        ).to_dict(),
        lambda: ChatMessage(
            id="tool-1",
            timestamp="2026-05-03T14:30:00+00:00",
            role="tool",
            content="{}",
            tool_call_id="call_abc",
            name="read",
            reasoning_timing=FIXED_TIMING,
        ).to_dict(),
        lambda: ChatMessage.user("hello", timestamp=datetime(2026, 5, 3, 14, 30)),
    ],
    ids=[
        "non-string-phase",
        "reasoning-timing-without-reasoning",
        "malformed-reasoning-timing",
        "reasoning-timing-on-user",
        "tool-display-on-user",
        "reasoning-timing-on-tool",
        "naive-timestamp",
    ],
)
def test_constructed_messages_are_validated(build: Callable[[], object]) -> None:
    with pytest.raises(ChatMessageValidationError):
        build()


@pytest.mark.parametrize(
    ("kind", "visible"),
    [
        (ERROR_KIND_RATE_LIMIT, True),
        (ERROR_KIND_TIMEOUT, True),
        (ERROR_KIND_NETWORK, True),
        (ERROR_KIND_PROVIDER_OVERLOAD, True),
        (ERROR_KIND_TOOL_ITERATIONS, True),
        (ERROR_KIND_PROVIDER_ERROR, True),
        (ERROR_KIND_AUTH, False),
        (ERROR_KIND_PROVIDER_FATAL, False),
        (ERROR_KIND_CONFIG, False),
        ("future_kind", False),
    ],
)
def test_only_recoverable_error_kinds_are_model_visible(kind: str, visible: bool) -> None:
    assert error_kind_llm_visible(kind) is visible


def test_checkpoint_stamps_add_context_tokens_and_duration_without_mutating() -> None:
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Earlier decisions.", projection=[], compacted_token_count=123
    )

    with_tokens = checkpoint.with_compaction_context_tokens(
        context_tokens_before=155_499, context_tokens_after=34_691
    )
    with_duration = checkpoint.with_compaction_duration_ms(duration_ms=54_000)

    assert with_tokens.usage == {
        "compacted_token_count": 123,
        "context_tokens_before": 155_499,
        "context_tokens_after": 34_691,
    }
    assert with_duration.usage == {"compacted_token_count": 123, "compaction_duration_ms": 54_000}
    assert checkpoint.usage == {"compacted_token_count": 123}
    for invalid in (-1, "54000", True):
        with pytest.raises(ChatMessageValidationError):
            checkpoint.with_compaction_duration_ms(duration_ms=invalid)  # type: ignore[arg-type]


def test_textual_checkpoint_projection_ends_provider_reasoning_state_but_keeps_phase() -> None:
    assistant = ChatMessage.assistant(
        model="openai/gpt-5.6-sol",
        content="Prior answer",
        reasoning="Readable summary",
        reasoning_meta={
            "response_output": [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}]
        },
        reasoning_scope="openai/gpt-5.6-sol::api-key",
        reasoning_timing={"first_delta_ms": 120, "last_delta_ms": 900},
        phase="final_answer",
    )

    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Earlier context.", projection=[assistant], compacted_token_count=10
    )

    assert checkpoint.projection is not None
    projected = ChatMessage.from_dict(checkpoint.projection[1])
    assert projected.reasoning is None
    assert projected.reasoning_meta is None
    assert projected.reasoning_scope is None
    assert projected.reasoning_timing is None
    assert projected.phase == "final_answer"
