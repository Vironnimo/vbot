"""Canonical chat message data model and provider request projection."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from core.chat.content_blocks import (
    ContentBlock,
    ContentBlockError,
    FileBlock,
    MediaBlock,
    TextBlock,
    content_block_from_dict,
    content_block_to_dict,
)
from core.chat.errors import ChatError, ChatMessageValidationError
from core.sessions import ChatSession, is_skill_context_note
from core.utils.tokens import estimate_message_tokens

MessageRole = Literal[
    "system",
    "user",
    "assistant",
    "tool",
    "note",
    "error",
    "compaction_checkpoint",
    "run_summary",
]
InputOrigin = Literal["speech_transcription"]
JsonObject = dict[str, Any]

TIMESTAMP_SUFFIX = "+00:00"
UTC_Z_SUFFIX = "Z"
SYSTEM_REMINDER_OPEN_TAG = "<system-reminder>"
SYSTEM_REMINDER_CLOSE_TAG = "</system-reminder>"
INPUT_ORIGIN_SPEECH_TRANSCRIPTION: InputOrigin = "speech_transcription"
SPEECH_TRANSCRIPTION_SYSTEM_REMINDER = (
    "The following user message was produced by speech-to-text transcription. "
    "It may contain transcription errors, missing punctuation, or misheard words. "
    "Infer the user's likely intent when appropriate, but do not mention this unless it matters."
)
ERROR_KIND_RATE_LIMIT = "rate_limit"
ERROR_KIND_TIMEOUT = "timeout"
ERROR_KIND_NETWORK = "network_error"
ERROR_KIND_PROVIDER_OVERLOAD = "provider_overloaded"
ERROR_KIND_TOOL_ITERATIONS = "tool_iterations_exceeded"
ERROR_KIND_AUTH = "auth_error"
ERROR_KIND_PROVIDER_FATAL = "provider_fatal"
ERROR_KIND_CONFIG = "config_error"
ERROR_KIND_PROVIDER_ERROR = "provider_error"
ERROR_KIND_LLM_VISIBLE: dict[str, bool] = {
    ERROR_KIND_RATE_LIMIT: True,
    ERROR_KIND_TIMEOUT: True,
    ERROR_KIND_NETWORK: True,
    ERROR_KIND_PROVIDER_OVERLOAD: True,
    ERROR_KIND_TOOL_ITERATIONS: True,
    ERROR_KIND_AUTH: False,
    ERROR_KIND_PROVIDER_FATAL: False,
    ERROR_KIND_CONFIG: False,
    ERROR_KIND_PROVIDER_ERROR: True,
}


@dataclass(frozen=True)
class ToolCall:
    """A canonical assistant-requested tool call."""

    id: str
    name: str
    arguments: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        """Return a JSON-serializable tool call dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }

    @classmethod
    def from_dict(cls, data: JsonObject) -> ToolCall:
        """Build a tool call from a JSON object."""
        tool_call_id = _require_string(data, "id")
        name = _require_string(data, "name")
        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ChatMessageValidationError("tool call arguments must be an object")
        return cls(id=tool_call_id, name=name, arguments=dict(arguments))


@dataclass(frozen=True)
class ChatMessage:
    """Canonical message persisted to session JSONL files."""

    id: str
    timestamp: str
    role: MessageRole
    content: str | list[ContentBlock] | None = None
    model: str | None = None
    reasoning: str | None = None
    reasoning_meta: JsonObject | None = None
    usage: JsonObject | None = None
    timing: JsonObject | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    error_kind: str | None = None
    tail_boundary_id: str | None = None
    run_id: str | None = None
    status: str | None = None

    @classmethod
    def system(cls, content: str, model: str, *, timestamp: datetime | None = None) -> ChatMessage:
        """Create a system message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="system",
            model=model,
            content=content,
        )

    @classmethod
    def user(
        cls,
        content: str | list[ContentBlock],
        *,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a user message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="user",
            content=content,
        )

    @classmethod
    def note(cls, content: str, *, timestamp: datetime | None = None) -> ChatMessage:
        """Create a kernel-internal note message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="note",
            content=content,
        )

    @classmethod
    def error(
        cls,
        error_kind: str,
        content: str,
        *,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a persisted error message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="error",
            content=content,
            error_kind=error_kind,
        )

    @classmethod
    def assistant(
        cls,
        *,
        model: str,
        content: str | None,
        reasoning: str | None = None,
        reasoning_meta: JsonObject | None = None,
        usage: JsonObject | None = None,
        tool_calls: list[ToolCall] | None = None,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create an assistant message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="assistant",
            model=model,
            content=content,
            reasoning=reasoning,
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            usage=dict(usage) if usage is not None else None,
            tool_calls=list(tool_calls) if tool_calls is not None else None,
        )

    @classmethod
    def tool(
        cls,
        *,
        tool_call_id: str,
        name: str,
        content: str,
        timing: JsonObject | None = None,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a tool result message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            name=name,
            timing=dict(timing) if timing is not None else None,
        )

    @classmethod
    def run_summary(
        cls,
        *,
        run_id: str,
        status: str,
        timing: JsonObject,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create an append-only run summary annotation."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="run_summary",
            run_id=run_id,
            status=status,
            timing=dict(timing),
        )

    @classmethod
    def compaction_checkpoint(
        cls,
        *,
        summary: str,
        tail_boundary_id: str,
        compacted_token_count: int,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a compaction checkpoint message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="compaction_checkpoint",
            content=summary,
            usage={"compacted_token_count": compacted_token_count},
            tail_boundary_id=tail_boundary_id,
        )

    def to_dict(self) -> JsonObject:
        """Return a canonical JSON-serializable message dictionary."""
        self.validate()
        message: JsonObject = {
            "id": self.id,
            "timestamp": self.timestamp,
            "role": self.role,
        }
        _add_if_not_none(message, "model", self.model)
        if self.content is not None:
            if isinstance(self.content, list):
                message["content"] = [content_block_to_dict(block) for block in self.content]
            else:
                message["content"] = self.content
        _add_if_not_none(message, "reasoning", self.reasoning)
        _add_if_not_none(message, "reasoning_meta", self.reasoning_meta)
        _add_if_not_none(message, "usage", self.usage)
        _add_if_not_none(message, "timing", self.timing)
        if self.tool_calls is not None:
            message["tool_calls"] = [tool_call.to_dict() for tool_call in self.tool_calls]
        _add_if_not_none(message, "tool_call_id", self.tool_call_id)
        _add_if_not_none(message, "name", self.name)
        _add_if_not_none(message, "error_kind", self.error_kind)
        _add_if_not_none(message, "tail_boundary_id", self.tail_boundary_id)
        _add_if_not_none(message, "run_id", self.run_id)
        _add_if_not_none(message, "status", self.status)
        return message

    @classmethod
    def from_dict(cls, data: JsonObject) -> ChatMessage:
        """Build a chat message from a canonical JSON object."""
        role = _require_role(data)
        tool_calls = _parse_tool_calls(data.get("tool_calls"))
        reasoning_meta = data.get("reasoning_meta")
        if reasoning_meta is not None and not isinstance(reasoning_meta, dict):
            raise ChatMessageValidationError("reasoning_meta must be an object")
        usage = data.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise ChatMessageValidationError("usage must be an object")
        timing = data.get("timing")
        if timing is not None and not isinstance(timing, dict):
            raise ChatMessageValidationError("timing must be an object")

        message = cls(
            id=_require_string(data, "id"),
            timestamp=_require_string(data, "timestamp"),
            role=role,
            content=_parse_content(data),
            model=_optional_string(data, "model"),
            reasoning=_optional_string(data, "reasoning"),
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            usage=dict(usage) if usage is not None else None,
            timing=dict(timing) if timing is not None else None,
            tool_calls=tool_calls,
            tool_call_id=_optional_string(data, "tool_call_id"),
            name=_optional_string(data, "name"),
            error_kind=_optional_string(data, "error_kind"),
            tail_boundary_id=_optional_string(data, "tail_boundary_id"),
            run_id=_optional_string(data, "run_id"),
            status=_optional_string(data, "status"),
        )
        message.validate()
        return message

    def validate(self) -> None:
        """Validate this message against the role-specific canonical schema."""
        _validate_core_fields(self)
        match self.role:
            case "system":
                _validate_system_message(self)
            case "user":
                _validate_user_message(self)
            case "assistant":
                _validate_assistant_message(self)
            case "tool":
                _validate_tool_message(self)
            case "note":
                _validate_note_message(self)
            case "error":
                _validate_error_message(self)
            case "compaction_checkpoint":
                _validate_compaction_checkpoint_message(self)
            case "run_summary":
                _validate_run_summary_message(self)


def error_kind_llm_visible(kind: str) -> bool:
    """Return whether an error kind should be included in later provider context."""
    return ERROR_KIND_LLM_VISIBLE.get(kind, False)


def _display_content_preview(content: str | list[ContentBlock]) -> str:
    if isinstance(content, str):
        return content[:500]

    text_blocks = [block.text for block in content if isinstance(block, TextBlock) and block.text]
    if not text_blocks:
        return "[attachment]"
    return " ".join(text_blocks)[:500]


def _append_input_origin_note(session: ChatSession, input_origin: InputOrigin | None) -> None:
    if input_origin is None:
        return
    if input_origin == INPUT_ORIGIN_SPEECH_TRANSCRIPTION:
        session.add_note(SPEECH_TRANSCRIPTION_SYSTEM_REMINDER)
        return
    raise ChatError(f"unsupported input origin: {input_origin}")


def _last_user_message_with_content_blocks(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role != "user":
            continue
        if isinstance(message.content, list):
            return message
        return None
    return None


def _last_user_message(messages: list[ChatMessage]) -> ChatMessage | None:
    """Return the most recently appended user message regardless of content type."""
    for message in reversed(messages):
        if message.role == "user":
            return message
    return None


def _session_has_any_content_blocks(messages: list[ChatMessage]) -> bool:
    """Return True if any user message in the session carries list content."""
    return any(message.role == "user" and isinstance(message.content, list) for message in messages)


def _message_to_request_dict(message: ChatMessage) -> JsonObject:
    data = message.to_dict()
    if data.get("role") == "assistant":
        data.pop("reasoning", None)
        data.pop("reasoning_meta", None)
        data.pop("usage", None)
    data.pop("timing", None)
    return data


def _assistant_continuation_dict(message: ChatMessage) -> JsonObject:
    """Return the live current-turn assistant dict for provider continuation.

    Keeps readable ``reasoning`` and opaque ``reasoning_meta`` so reasoning-aware
    adapters can round-trip the active tool-use turn, but drops ``usage`` because
    token accounting is never part of the provider request contract.
    """
    data = message.to_dict()
    data.pop("usage", None)
    data.pop("timing", None)
    return data


def _restore_active_tool_continuation(
    rebuilt_messages: list[JsonObject],
    current_messages: list[JsonObject],
) -> list[JsonObject]:
    active_assistant = _active_tool_continuation_assistant(current_messages)
    if active_assistant is None:
        return rebuilt_messages

    active_assistant_id = active_assistant.get("id")
    if not isinstance(active_assistant_id, str):
        return rebuilt_messages

    restored_messages: list[JsonObject] = []
    restored = False
    for message in rebuilt_messages:
        if message.get("role") == "assistant" and message.get("id") == active_assistant_id:
            restored_messages.append(dict(active_assistant))
            restored = True
            continue
        restored_messages.append(message)
    return restored_messages if restored else rebuilt_messages


def _active_tool_continuation_assistant(messages: list[JsonObject]) -> JsonObject | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        if not message.get("tool_calls"):
            return None
        if _is_active_tool_continuation_suffix(messages[index + 1 :]):
            return dict(message)
        return None
    return None


def _is_active_tool_continuation_suffix(messages: list[JsonObject]) -> bool:
    return bool(messages) and all(message.get("role") == "tool" for message in messages)


def _latest_compaction_checkpoint(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role == "compaction_checkpoint":
            return message
    return None


def _messages_from_boundary(messages: list[ChatMessage], boundary_id: str) -> list[ChatMessage]:
    for index, message in enumerate(messages):
        if message.id == boundary_id:
            return messages[index:]
    raise ChatError(f"compaction boundary id not found: {boundary_id}")


def _embed_notes_into_request(messages: list[ChatMessage]) -> list[JsonObject]:
    request_messages: list[JsonObject] = []
    pending_notes: list[ChatMessage] = []
    deferred_until_after_tools: list[ChatMessage] = []

    for message in messages:
        if message.role == "note":
            if is_skill_context_note(message):
                continue
            pending_notes.append(message)
            continue

        if message.role == "error":
            if message.error_kind is not None and error_kind_llm_visible(message.error_kind):
                pending_notes.append(message)
            continue

        if message.role == "run_summary":
            continue

        if message.role == "tool":
            if pending_notes:
                deferred_until_after_tools.extend(pending_notes)
                pending_notes = []
            request_messages.append(_message_to_request_dict(message))
            continue

        # Reasoning-only assistant turns lose reasoning fields for follow-up turns.
        # Skip them so request history never contains empty assistant entries.
        if _is_empty_assistant_history_message(message):
            continue

        if deferred_until_after_tools:
            request_messages.append(_notes_to_synthetic_user_message(deferred_until_after_tools))
            deferred_until_after_tools = []

        if pending_notes:
            request_messages.append(_notes_to_synthetic_user_message(pending_notes))
            pending_notes = []
        request_messages.append(_message_to_request_dict(message))

    if deferred_until_after_tools:
        request_messages.append(_notes_to_synthetic_user_message(deferred_until_after_tools))

    if pending_notes:
        request_messages.append(_notes_to_synthetic_user_message(pending_notes))

    return request_messages


def _notes_to_synthetic_user_message(notes: list[ChatMessage]) -> JsonObject:
    return {
        "role": "user",
        "content": "\n".join(_system_reminder_block(note) for note in notes),
    }


def _is_empty_assistant_history_message(message: ChatMessage) -> bool:
    return message.role == "assistant" and message.content is None and not message.tool_calls


def _system_reminder_block(message: ChatMessage) -> str:
    message.validate()
    return f"{SYSTEM_REMINDER_OPEN_TAG}\n{message.content}\n{SYSTEM_REMINDER_CLOSE_TAG}"


def _assistant_message_from_response(model: str, response: JsonObject) -> ChatMessage:
    tool_calls = _parse_tool_calls(response.get("tool_calls"))
    return ChatMessage.assistant(
        model=model,
        content=_nullable_response_string(response, "content"),
        reasoning=_nullable_response_string(response, "reasoning"),
        reasoning_meta=_response_reasoning_meta(response),
        usage=response.get("usage"),
        tool_calls=tool_calls,
    )


def _apply_usage_estimation(
    message: ChatMessage,
    request_messages: list[JsonObject],
) -> ChatMessage:
    """Estimate token usage when the provider doesn't supply usage data."""
    estimated_input = sum(
        estimate_message_tokens(request_message)[0] for request_message in request_messages
    )
    estimated_output, _ = estimate_message_tokens(message.to_dict())
    usage: JsonObject = {
        "input_tokens": estimated_input,
        "output_tokens": estimated_output,
        "estimated": True,
    }
    return replace(message, usage=usage)


def _nullable_response_string(response: JsonObject, key: str) -> str | None:
    value = response.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ChatMessageValidationError(f"assistant response {key} must be a string or null")


def _response_reasoning_meta(response: JsonObject) -> JsonObject | None:
    reasoning_meta = response.get("reasoning_meta")
    if reasoning_meta is None:
        return None
    if not isinstance(reasoning_meta, dict):
        raise ChatMessageValidationError("assistant response reasoning_meta must be an object")
    return dict(reasoning_meta)


def _new_message_id() -> str:
    return str(uuid.uuid4())


def _format_timestamp(timestamp: datetime | None) -> str:
    value = timestamp or datetime.now(UTC)
    if value.tzinfo is None:
        raise ChatMessageValidationError("timestamp must include timezone information")
    return value.astimezone(UTC).isoformat()


def _add_if_not_none(message: JsonObject, key: str, value: Any) -> None:
    if value is not None:
        message[key] = value


def _require_string(data: JsonObject, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ChatMessageValidationError(f"{key} must be a non-empty string")
    return value


def _optional_string(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChatMessageValidationError(f"{key} must be a string")
    return value


def _parse_content(data: JsonObject) -> str | list[ContentBlock] | None:
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


def _require_role(data: JsonObject) -> MessageRole:
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
    ):
        raise ChatMessageValidationError(
            "role must be system, user, assistant, tool, note, error, "
            "compaction_checkpoint, or run_summary"
        )
    return cast(MessageRole, role)


def _parse_tool_calls(value: Any) -> list[ToolCall] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ChatMessageValidationError("tool_calls must be an array")
    return [ToolCall.from_dict(item) for item in value if _is_tool_call_object(item)]


def _is_content_block(value: Any) -> bool:
    return isinstance(value, (TextBlock, MediaBlock, FileBlock))


def _is_tool_call_object(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise ChatMessageValidationError("tool_calls entries must be objects")
    return value


def _validate_core_fields(message: ChatMessage) -> None:
    if not message.id:
        raise ChatMessageValidationError("id must be a non-empty string")
    if not message.timestamp:
        raise ChatMessageValidationError("timestamp must be a non-empty string")
    if not _has_explicit_utc_offset(message.timestamp):
        raise ChatMessageValidationError("timestamp must include explicit UTC offset")


def _has_explicit_utc_offset(timestamp: str) -> bool:
    if timestamp.endswith(UTC_Z_SUFFIX):
        return _is_valid_iso_utc_timestamp(timestamp[:-1] + TIMESTAMP_SUFFIX)
    if TIMESTAMP_SUFFIX in timestamp:
        return _is_valid_iso_utc_timestamp(timestamp)
    return False


def _is_valid_iso_utc_timestamp(timestamp: str) -> bool:
    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


def _validate_timing_payload(timing: JsonObject | None) -> None:
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


def _validate_system_message(message: ChatMessage) -> None:
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
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "tail_boundary_id",
        "run_id",
        "status",
    )


def _validate_user_message(message: ChatMessage) -> None:
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
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "tail_boundary_id",
        "run_id",
        "status",
    )


def _validate_assistant_message(message: ChatMessage) -> None:
    if message.model is None:
        raise ChatMessageValidationError("assistant messages require model")
    if message.content is not None and not isinstance(message.content, str):
        raise ChatMessageValidationError("assistant messages content must be a string")
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
        "tail_boundary_id",
        "run_id",
        "status",
    )
    if message.reasoning_meta is not None and not isinstance(message.reasoning_meta, dict):
        raise ChatMessageValidationError("reasoning_meta must be an object")
    if message.usage is not None and not isinstance(message.usage, dict):
        raise ChatMessageValidationError("usage must be an object")


def _validate_tool_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("tool messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("tool messages content must be a string")
    if message.tool_call_id is None:
        raise ChatMessageValidationError("tool messages require tool_call_id")
    if message.name is None:
        raise ChatMessageValidationError("tool messages require name")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "usage",
        "tool_calls",
        "error_kind",
        "tail_boundary_id",
        "run_id",
        "status",
    )
    _validate_timing_payload(message.timing)


def _validate_note_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("note messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("note messages content must be a string")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "tail_boundary_id",
        "run_id",
        "status",
    )


def _validate_error_message(message: ChatMessage) -> None:
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
        "usage",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "tail_boundary_id",
        "run_id",
        "status",
    )


def _validate_compaction_checkpoint_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("compaction checkpoints require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("compaction checkpoints content must be a string")
    if message.tail_boundary_id is None:
        raise ChatMessageValidationError("compaction checkpoints require tail_boundary_id")
    if not message.tail_boundary_id:
        raise ChatMessageValidationError(
            "compaction checkpoints tail_boundary_id must be a non-empty string"
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

    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "timing",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "run_id",
        "status",
    )


def _validate_run_summary_message(message: ChatMessage) -> None:
    if not message.run_id:
        raise ChatMessageValidationError("run summaries require run_id")
    if message.status not in {"completed", "failed", "cancelled"}:
        raise ChatMessageValidationError(
            "run summaries status must be completed, failed, or cancelled"
        )
    if message.timing is None:
        raise ChatMessageValidationError("run summaries require timing")
    _validate_timing_payload(message.timing)
    _reject_fields(
        message,
        "content",
        "model",
        "reasoning",
        "reasoning_meta",
        "usage",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "tail_boundary_id",
    )


def _reject_fields(message: ChatMessage, *fields: str) -> None:
    for field_name in fields:
        if getattr(message, field_name) is not None:
            raise ChatMessageValidationError(f"{message.role} messages cannot include {field_name}")
