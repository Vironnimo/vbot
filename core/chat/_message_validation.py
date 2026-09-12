"""Canonical role and payload validation for Message records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from core.chat import messages as _records
from core.chat.content_blocks import (
    ContentBlock,
    ContentBlockError,
    FileBlock,
    FileMentionBlock,
    MediaBlock,
    TextBlock,
    content_block_from_dict,
)
from core.chat.errors import ChatMessageValidationError
from core.chat.output_files import AssistantFileReference
from core.providers.adapter import (
    TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD,
    TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD,
)


def _require_string(data: _records.JsonObject, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ChatMessageValidationError(f"{key} must be a non-empty string")
    return value


def _optional_string(data: _records.JsonObject, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChatMessageValidationError(f"{key} must be a string")
    return value


def _parse_content(data: _records.JsonObject) -> str | list[ContentBlock] | None:
    value = data.get("content")
    if value is None or isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ChatMessageValidationError(
            "content must be a string, an array of content blocks, or null"
        )

    blocks: list[ContentBlock] = []
    for item in value:
        if not isinstance(item, dict):
            raise ChatMessageValidationError("content list entries must be objects")
        try:
            blocks.append(content_block_from_dict(item))
        except ContentBlockError as exc:
            raise ChatMessageValidationError(f"invalid content block: {exc}") from exc
    return blocks


def _require_role(data: _records.JsonObject) -> _records.MessageRole:
    role = data.get("role")
    if role not in (
        "system",
        "user",
        "assistant",
        "tool",
        "note",
        "error",
        "compaction_checkpoint",
        "run_summary",
        "agent_takeover",
        "history_edit",
    ):
        raise ChatMessageValidationError(
            "role must be system, user, assistant, tool, note, error, "
            "compaction_checkpoint, run_summary, agent_takeover, or history_edit"
        )
    return cast(_records.MessageRole, role)


def _parse_tool_calls(value: Any) -> list[_records.ToolCall] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ChatMessageValidationError("tool_calls must be an array")
    return [_records.ToolCall.from_dict(item) for item in value if _is_tool_call_object(item)]


def _parse_tool_call_argument_sequence(data: _records.JsonObject) -> tuple[int | None, int | None]:
    index = data.get(TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD)
    length = data.get(TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD)
    _validate_tool_call_argument_sequence(index, length)
    if index is None:
        return None, None
    return cast(int, index), cast(int, length)


def _validate_tool_call_argument_sequence(index: Any, length: Any) -> None:
    if index is None and length is None:
        return
    if (
        not isinstance(index, int)
        or isinstance(index, bool)
        or not isinstance(length, int)
        or isinstance(length, bool)
        or length <= 1
        or index < 0
        or index >= length
    ):
        raise ChatMessageValidationError("tool call argument sequence metadata is invalid")


def _is_content_block(value: Any) -> bool:
    return isinstance(value, (TextBlock, MediaBlock, FileBlock, FileMentionBlock))


def _is_tool_call_object(value: Any) -> _records.JsonObject:
    if not isinstance(value, dict):
        raise ChatMessageValidationError("tool_calls entries must be objects")
    return value


def _validate_core_fields(message: _records.ChatMessage) -> None:
    if not message.id:
        raise ChatMessageValidationError("id must be a non-empty string")
    if not message.timestamp:
        raise ChatMessageValidationError("timestamp must be a non-empty string")
    if not _has_explicit_utc_offset(message.timestamp):
        raise ChatMessageValidationError("timestamp must include explicit UTC offset")
    if message.role != "assistant" and message.phase is not None:
        raise ChatMessageValidationError(f"{message.role} messages cannot include phase")
    if message.role != "assistant" and message.reasoning_scope is not None:
        raise ChatMessageValidationError(f"{message.role} messages cannot include reasoning_scope")
    if message.role != "tool" and message.tool_display is not None:
        raise ChatMessageValidationError(f"{message.role} messages cannot include tool_display")
    if message.role != "assistant" and message.output_files is not None:
        raise ChatMessageValidationError(f"{message.role} messages cannot include output_files")
    if message.role != "run_summary" and message.change_stats is not None:
        raise ChatMessageValidationError(f"{message.role} messages cannot include change_stats")
    if message.role != "history_edit" and message.target_message_id is not None:
        raise ChatMessageValidationError(
            f"{message.role} messages cannot include target_message_id"
        )
    if message.role != "compaction_checkpoint":
        _reject_fields(
            message,
            "tail_boundary_id",
            "projection",
            "compaction_policy",
            "compaction_strategy",
        )


def _has_explicit_utc_offset(timestamp: str) -> bool:
    if timestamp.endswith(_records.UTC_Z_SUFFIX):
        return _is_valid_iso_utc_timestamp(timestamp[:-1] + _records.TIMESTAMP_SUFFIX)
    if _records.TIMESTAMP_SUFFIX in timestamp:
        return _is_valid_iso_utc_timestamp(timestamp)
    return False


def _is_valid_iso_utc_timestamp(timestamp: str) -> bool:
    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


def _validate_timing_payload(timing: _records.JsonObject | None) -> None:
    if timing is None:
        return
    if not isinstance(timing, dict):
        raise ChatMessageValidationError("timing must be an object")
    started_at = timing.get("started_at")
    completed_at = timing.get("completed_at")
    duration_ms = timing.get("duration_ms")
    if not isinstance(started_at, str) or not started_at:
        raise ChatMessageValidationError("timing.started_at must be a non-empty string")
    if not isinstance(completed_at, str) or not completed_at:
        raise ChatMessageValidationError("timing.completed_at must be a non-empty string")
    if not _has_explicit_utc_offset(started_at):
        raise ChatMessageValidationError("timing.started_at must include explicit UTC offset")
    if not _has_explicit_utc_offset(completed_at):
        raise ChatMessageValidationError("timing.completed_at must include explicit UTC offset")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
        raise ChatMessageValidationError("timing.duration_ms must be a non-negative integer")


def _validate_system_message(message: _records.ChatMessage) -> None:
    if message.model is None:
        raise ChatMessageValidationError("system messages require model")
    if message.content is None:
        raise ChatMessageValidationError("system messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("system messages content must be a string")
    _reject_fields(
        message,
        "reasoning",
        "reasoning_meta",
        "reasoning_timing",
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "run_id",
        "work_id",
        "status",
        "sender",
    )


def _validate_user_message(message: _records.ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("user messages require content")
    if isinstance(message.content, list):
        if not message.content:
            raise ChatMessageValidationError("user content block lists must not be empty")
        if not all(_is_content_block(block) for block in message.content):
            raise ChatMessageValidationError(
                "user content block lists must contain only content blocks"
            )
    elif not isinstance(message.content, str):
        raise ChatMessageValidationError("user messages content must be a string")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "reasoning_timing",
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "run_id",
        "work_id",
        "status",
    )


def _validate_assistant_message(message: _records.ChatMessage) -> None:
    if message.model is None:
        raise ChatMessageValidationError("assistant messages require model")
    if message.content is not None and not isinstance(message.content, str):
        raise ChatMessageValidationError("assistant messages content must be a string")
    if message.phase is not None and (not isinstance(message.phase, str) or not message.phase):
        raise ChatMessageValidationError("assistant messages phase must be a non-empty string")
    if message.reasoning_scope is not None and (
        not isinstance(message.reasoning_scope, str) or not message.reasoning_scope
    ):
        raise ChatMessageValidationError(
            "assistant messages reasoning_scope must be a non-empty string"
        )
    has_tool_calls = bool(message.tool_calls)
    has_visible_reasoning = message.reasoning is not None
    has_reasoning_meta = message.reasoning_meta is not None
    if (
        message.content is None
        and not has_tool_calls
        and not has_visible_reasoning
        and not has_reasoning_meta
    ):
        raise ChatMessageValidationError(
            "assistant messages require content, reasoning, reasoning_meta, or tool_calls"
        )
    _reject_fields(
        message,
        "timing",
        "tool_call_id",
        "name",
        "error_kind",
        "run_id",
        "work_id",
        "status",
        "sender",
    )
    if message.reasoning_meta is not None and not isinstance(message.reasoning_meta, dict):
        raise ChatMessageValidationError("reasoning_meta must be an object")
    if message.reasoning_timing is not None:
        if message.reasoning is None:
            raise ChatMessageValidationError(
                "assistant messages reasoning_timing requires reasoning"
            )
        _validate_timing_payload(message.reasoning_timing)
    if message.phase is not None and not message.phase:
        raise ChatMessageValidationError("assistant phase must be a non-empty string")
    if message.usage is not None and not isinstance(message.usage, dict):
        raise ChatMessageValidationError("usage must be an object")
    if message.output_files is not None:
        if not message.output_files:
            raise ChatMessageValidationError("assistant output_files must not be empty")
        if not all(
            isinstance(reference, AssistantFileReference) for reference in message.output_files
        ):
            raise ChatMessageValidationError(
                "assistant output_files must contain AssistantFileReference values"
            )
        content_lines = message.content.splitlines() if isinstance(message.content, str) else []
        if any(
            reference.line_index < 0 or reference.line_index >= len(content_lines)
            for reference in message.output_files
        ):
            raise ChatMessageValidationError(
                "assistant output_files line indexes must identify content lines"
            )
        spans_by_line: dict[int, list[tuple[int, int]]] = {}
        legacy_lines: set[int] = set()
        for reference in message.output_files:
            if reference.start_index is None or reference.end_index is None:
                if reference.start_index is not None or reference.end_index is not None:
                    raise ChatMessageValidationError(
                        "assistant output_files spans must be provided together"
                    )
                legacy_lines.add(reference.line_index)
                continue
            if (
                isinstance(reference.start_index, bool)
                or isinstance(reference.end_index, bool)
                or reference.start_index < 0
                or reference.end_index <= reference.start_index
                or reference.end_index > len(content_lines[reference.line_index])
            ):
                raise ChatMessageValidationError(
                    "assistant output_files spans must identify content text"
                )
            spans_by_line.setdefault(reference.line_index, []).append(
                (reference.start_index, reference.end_index)
            )
        if any(line_index in spans_by_line for line_index in legacy_lines) or len(
            legacy_lines
        ) != sum(reference.start_index is None for reference in message.output_files):
            raise ChatMessageValidationError(
                "assistant output_files legacy references must be unique per line"
            )
        for spans in spans_by_line.values():
            ordered = sorted(spans)
            if any(
                current[0] < previous[1]
                for previous, current in zip(ordered, ordered[1:], strict=False)
            ):
                raise ChatMessageValidationError("assistant output_files spans must not overlap")


def _validate_tool_message(message: _records.ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("tool messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("tool messages content must be a string")
    if message.tool_call_id is None:
        raise ChatMessageValidationError("tool messages require tool_call_id")
    if message.name is None:
        raise ChatMessageValidationError("tool messages require name")
    if message.tool_display is not None and not isinstance(message.tool_display, dict):
        raise ChatMessageValidationError("tool_display must be an object")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "reasoning_timing",
        "usage",
        "tool_calls",
        "error_kind",
        "run_id",
        "work_id",
        "status",
        "sender",
    )
    _validate_timing_payload(message.timing)


def _validate_note_message(message: _records.ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("note messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("note messages content must be a string")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "reasoning_timing",
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "run_id",
        "work_id",
        "status",
        "sender",
    )


def _validate_error_message(message: _records.ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("error messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("error messages content must be a string")
    if not message.error_kind:
        raise ChatMessageValidationError("error messages require error_kind")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "reasoning_timing",
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "run_id",
        "work_id",
        "status",
        "sender",
    )


def _validate_compaction_checkpoint_message(message: _records.ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("compaction checkpoints require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("compaction checkpoints content must be a string")
    if message.projection is None:
        if not message.tail_boundary_id:
            raise ChatMessageValidationError(
                "legacy compaction checkpoints require tail_boundary_id"
            )
        if message.compaction_policy or message.compaction_strategy:
            raise ChatMessageValidationError(
                "legacy compaction checkpoints cannot include Policy provenance"
            )
    else:
        if message.tail_boundary_id is not None:
            raise ChatMessageValidationError(
                "projected compaction checkpoints cannot include tail_boundary_id"
            )
        if not message.compaction_policy:
            raise ChatMessageValidationError("compaction checkpoints require compaction_policy")
        if not message.compaction_strategy:
            raise ChatMessageValidationError("compaction checkpoints require compaction_strategy")
        for entry in message.projection:
            projected = _records.ChatMessage.from_dict(entry)
            if projected.role == "compaction_checkpoint":
                raise ChatMessageValidationError(
                    "compaction checkpoint projections cannot contain checkpoints"
                )

    if message.usage is not None:
        compacted_count = message.usage.get("compacted_token_count")
        if (
            isinstance(compacted_count, bool)
            or not isinstance(compacted_count, int)
            or compacted_count < 0
        ):
            raise ChatMessageValidationError(
                "compaction checkpoints usage.compacted_token_count must be a non-negative integer"
            )
        before_present = "context_tokens_before" in message.usage
        after_present = "context_tokens_after" in message.usage
        if before_present != after_present:
            raise ChatMessageValidationError(
                "compaction checkpoints require both context token counts or neither"
            )
        for field_name in ("context_tokens_before", "context_tokens_after"):
            if field_name not in message.usage:
                continue
            token_count = message.usage[field_name]
            if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0:
                raise ChatMessageValidationError(
                    f"compaction checkpoints usage.{field_name} must be a non-negative integer"
                )

    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "reasoning_timing",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "run_id",
        "work_id",
        "status",
        "sender",
    )


def _validate_run_summary_message(message: _records.ChatMessage) -> None:
    if not message.run_id:
        raise ChatMessageValidationError("run summaries require run_id")
    if message.status not in {"completed", "failed", "cancelled", "interrupted"}:
        raise ChatMessageValidationError(
            "run summaries status must be completed, failed, cancelled, or interrupted"
        )
    if message.timing is None:
        raise ChatMessageValidationError("run summaries require timing")
    if message.iteration_count is not None and (
        isinstance(message.iteration_count, bool)
        or not isinstance(message.iteration_count, int)
        or message.iteration_count < 0
    ):
        raise ChatMessageValidationError("run summaries iteration_count must be non-negative")
    if message.change_stats is not None:
        _validate_change_stats(message.change_stats)
    _validate_timing_payload(message.timing)
    _reject_fields(
        message,
        "content",
        "model",
        "reasoning",
        "reasoning_meta",
        "reasoning_timing",
        "usage",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "sender",
    )


def _validate_change_stats(change_stats: _records.JsonObject) -> None:
    """Validate the git-style change statistics carried by a run summary."""
    files = change_stats.get("files")
    if isinstance(files, bool) or not isinstance(files, int) or files < 0:
        raise ChatMessageValidationError("change_stats.files must be a non-negative integer")
    added = change_stats.get("added")
    if isinstance(added, bool) or not isinstance(added, int) or added < 0:
        raise ChatMessageValidationError("change_stats.added must be a non-negative integer")
    removed = change_stats.get("removed")
    if isinstance(removed, bool) or not isinstance(removed, int) or removed < 0:
        raise ChatMessageValidationError("change_stats.removed must be a non-negative integer")
    paths = change_stats.get("paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ChatMessageValidationError("change_stats.paths must be an array of strings")


def _validate_agent_takeover_message(message: _records.ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("agent takeover messages require content")
    if not isinstance(message.content, str) or not message.content:
        raise ChatMessageValidationError(
            "agent takeover messages content must be a non-empty string"
        )
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "reasoning_timing",
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "run_id",
        "work_id",
        "status",
        "sender",
    )


def _validate_history_edit_message(message: _records.ChatMessage) -> None:
    if not message.target_message_id:
        raise ChatMessageValidationError("history edit messages require target_message_id")
    _reject_fields(
        message,
        "content",
        "model",
        "reasoning",
        "reasoning_meta",
        "reasoning_timing",
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "run_id",
        "work_id",
        "status",
        "sender",
    )


def _reject_fields(message: _records.ChatMessage, *fields: str) -> None:
    for field_name in fields:
        if getattr(message, field_name) is not None:
            raise ChatMessageValidationError(f"{message.role} messages cannot include {field_name}")
