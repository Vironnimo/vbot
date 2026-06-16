"""Canonical chat message data model and provider request projection."""

from __future__ import annotations

import json
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
from core.chat.model_resolution import parse_bare_model
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_NONE,
    ReasoningReplayPolicy,
)
from core.sessions import (
    PARTIAL_THINKING_NOTE_PREFIX,
    ChatSession,
    is_partial_thinking_note,
    is_skill_context_note,
)
from core.tools import tool_failure
from core.utils.tokens import estimate_message_tokens

INTERRUPTED_TOOL_RESULT_CODE = "result_unavailable"
INTERRUPTED_TOOL_RESULT_MESSAGE = "Tool run was interrupted before a result was recorded."

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

# Appended to a checkpoint summary when the preserved tail could not be anchored
# on its recorded boundary and was recovered from post-checkpoint history.
COMPACTION_TAIL_RECOVERED_HINT = (
    "Part of the recent verbatim history could not be restored after a data issue "
    "and is omitted below. The summary above still covers earlier context; the "
    "conversation continues from the messages that follow."
)
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
class MessageSender:
    """Platform identity of the human who sent a user message.

    Captured from platform metadata (never from message text) so request-time
    attribution tags cannot be spoofed by typing a look-alike prefix.
    """

    id: str
    display_name: str

    def to_dict(self) -> JsonObject:
        """Return a JSON-serializable sender dictionary."""
        return {"id": self.id, "display_name": self.display_name}

    @classmethod
    def from_dict(cls, data: JsonObject) -> MessageSender:
        """Build a sender from a JSON object."""
        sender_id = data.get("id")
        if not isinstance(sender_id, str) or not sender_id:
            raise ChatMessageValidationError("sender id must be a non-empty string")
        display_name = data.get("display_name")
        if not isinstance(display_name, str) or not display_name:
            raise ChatMessageValidationError("sender display_name must be a non-empty string")
        return cls(id=sender_id, display_name=display_name)


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
    sender: MessageSender | None = None

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
        sender: MessageSender | None = None,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a user message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="user",
            content=content,
            sender=sender,
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
        if self.sender is not None:
            message["sender"] = self.sender.to_dict()
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
        sender_data = data.get("sender")
        if sender_data is not None and not isinstance(sender_data, dict):
            raise ChatMessageValidationError("sender must be an object")

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
            sender=MessageSender.from_dict(sender_data) if sender_data is not None else None,
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


def _message_to_request_dict(
    message: ChatMessage,
    *,
    replay_policy: ReasoningReplayPolicy = REASONING_REPLAY_CURRENT_RUN,
    agent_model: str | None = None,
) -> JsonObject:
    data = message.to_dict()
    if data.get("role") == "assistant":
        if not _replays_assistant_reasoning(message, replay_policy, agent_model):
            data.pop("reasoning", None)
            data.pop("reasoning_meta", None)
        data.pop("usage", None)
    data.pop("timing", None)
    # Sender attribution exists only in the provider request: persisted content stays
    # clean and the tag cannot be spoofed by typing a look-alike prefix in message text.
    data.pop("sender", None)
    if message.role == "user" and message.sender is not None:
        _apply_sender_attribution(data, message.sender)
    return data


def _replays_assistant_reasoning(
    message: ChatMessage,
    replay_policy: ReasoningReplayPolicy,
    agent_model: str | None,
) -> bool:
    """Return whether history shaping keeps this assistant turn's reasoning fields.

    Only ``full_history`` replays persisted reasoning across runs, and only when
    the entry's persisted model passes the same-model gate against the agent's
    current model (optional ``::<connection>[:<account>]`` suffixes stripped on
    both sides). A mismatch means the reasoning belongs to a different model and
    is stripped exactly like under ``current_run``.
    """
    if replay_policy != REASONING_REPLAY_FULL_HISTORY:
        return False
    if agent_model is None or message.model is None:
        return False
    return parse_bare_model(message.model) == parse_bare_model(agent_model)


# Characters removed from sender tag parts so a display name cannot forge the
# tag delimiters of another participant.
_SENDER_TAG_UNSAFE_CHARACTERS = str.maketrans("", "", "[]|\r\n")


def _apply_sender_attribution(data: JsonObject, sender: MessageSender) -> None:
    tag = _sender_attribution_tag(sender)
    content = data.get("content")
    if isinstance(content, str):
        data["content"] = f"{tag}: {content}"
    elif isinstance(content, list):
        data["content"] = [{"type": "text", "text": f"{tag}:"}, *content]


def _sender_attribution_tag(sender: MessageSender) -> str:
    display_name = _sanitize_sender_tag_part(sender.display_name)
    sender_id = _sanitize_sender_tag_part(sender.id)
    return f"[{display_name}|{sender_id}]"


def _sanitize_sender_tag_part(value: str) -> str:
    sanitized = value.translate(_SENDER_TAG_UNSAFE_CHARACTERS).strip()
    return sanitized or "unknown"


def _assistant_continuation_dict(
    message: ChatMessage,
    *,
    replay_policy: ReasoningReplayPolicy = REASONING_REPLAY_CURRENT_RUN,
) -> JsonObject:
    """Return the live current-turn assistant dict for provider continuation.

    Keeps readable ``reasoning`` and opaque ``reasoning_meta`` so reasoning-aware
    adapters can round-trip the active tool-use turn, but drops ``usage`` because
    token accounting is never part of the provider request contract. Under the
    ``none`` replay policy even the live turn loses its reasoning fields.
    """
    data = message.to_dict()
    data.pop("usage", None)
    data.pop("timing", None)
    if replay_policy == REASONING_REPLAY_NONE:
        data.pop("reasoning", None)
        data.pop("reasoning_meta", None)
    return data


def _strip_assistant_reasoning_fields(messages: list[JsonObject]) -> None:
    """Remove ``reasoning``/``reasoning_meta`` from assistant request entries.

    Used when a Run switches providers mid-run: reasoning metadata produced by
    the old provider is stale by definition and must never be replayed to the
    new provider.
    """
    for message in messages:
        if message.get("role") == "assistant":
            message.pop("reasoning", None)
            message.pop("reasoning_meta", None)


def _restore_in_run_assistant_reasoning(
    rebuilt_messages: list[JsonObject],
    current_messages: list[JsonObject],
) -> list[JsonObject]:
    """Carry in-run assistant reasoning fields into a rebuilt request list.

    Mid-run rebuilds (auto-compaction) re-shape history through the same
    policy-aware path as fresh runs, which strips current-run reasoning under
    ``current_run``. The live request list still carries those fields, so every
    rebuilt assistant entry whose ``id`` matches a live entry gets its
    ``reasoning``/``reasoning_meta`` restored — all current-run turns, not just
    the latest tool continuation. Under ``none`` the live entries carry no
    reasoning, so this is a no-op.
    """
    reasoning_by_id: dict[str, JsonObject] = {}
    for message in current_messages:
        if message.get("role") != "assistant":
            continue
        message_id = message.get("id")
        if not isinstance(message_id, str):
            continue
        reasoning_fields = {
            key: message[key] for key in ("reasoning", "reasoning_meta") if message.get(key)
        }
        if reasoning_fields:
            reasoning_by_id[message_id] = reasoning_fields
    if not reasoning_by_id:
        return rebuilt_messages

    restored_messages: list[JsonObject] = []
    for message in rebuilt_messages:
        fields: JsonObject | None = None
        if message.get("role") == "assistant":
            message_id = message.get("id")
            if isinstance(message_id, str):
                fields = reasoning_by_id.get(message_id)
        restored_messages.append({**message, **fields} if fields else message)
    return restored_messages


def _latest_compaction_checkpoint(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role == "compaction_checkpoint":
            return message
    return None


def _resolve_preserved_tail(
    messages: list[ChatMessage], checkpoint: ChatMessage
) -> tuple[list[ChatMessage], bool]:
    """Resolve the verbatim tail to replay after a compaction checkpoint.

    The tail normally starts at the checkpoint's recorded ``tail_boundary_id``.
    When that anchor message is no longer present in the loaded history (a
    corrupted or partial write can truncate it, or the id may be absent), the
    start falls back to the position right after the checkpoint itself, which is
    always locatable. That keeps a compacted session usable instead of
    permanently failing every request build on a dangling boundary reference.

    Returns ``(tail_messages, recovered)`` where ``recovered`` signals that the
    fallback anchor was used. Checkpoint markers are excluded from the tail.
    """
    start_index, recovered = _tail_start_index(messages, checkpoint)
    tail_messages = [
        message for message in messages[start_index:] if message.role != "compaction_checkpoint"
    ]
    return tail_messages, recovered


def _tail_start_index(messages: list[ChatMessage], checkpoint: ChatMessage) -> tuple[int, bool]:
    boundary_id = checkpoint.tail_boundary_id
    if boundary_id is not None:
        for index, message in enumerate(messages):
            if message.id == boundary_id:
                return index, False
    return _index_after_checkpoint(messages, checkpoint), True


def _index_after_checkpoint(messages: list[ChatMessage], checkpoint: ChatMessage) -> int:
    for index, message in enumerate(messages):
        if message.id == checkpoint.id:
            return index + 1
    return len(messages)


def _embed_notes_into_request(
    messages: list[ChatMessage],
    *,
    replay_policy: ReasoningReplayPolicy = REASONING_REPLAY_CURRENT_RUN,
    agent_model: str | None = None,
) -> list[JsonObject]:
    request_messages = _assemble_request_history(
        messages,
        replay_policy=replay_policy,
        agent_model=agent_model,
    )
    return _repair_dangling_tool_calls(request_messages)


def _last_assistant_index(messages: list[ChatMessage]) -> int:
    """Return the index of the last assistant message, or -1 if none exists."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "assistant":
            return index
    return -1


def _assemble_request_history(
    messages: list[ChatMessage],
    *,
    replay_policy: ReasoningReplayPolicy = REASONING_REPLAY_CURRENT_RUN,
    agent_model: str | None = None,
) -> list[JsonObject]:
    request_messages: list[JsonObject] = []
    pending_notes: list[ChatMessage] = []
    deferred_until_after_tools: list[ChatMessage] = []

    last_assistant_index = _last_assistant_index(messages)

    for index, message in enumerate(messages):
        if message.role == "note":
            if is_skill_context_note(message):
                continue
            # A partial-thinking note is the only trace of an interrupted run
            # (no assistant message is persisted for it). Embed it one-shot:
            # only while no assistant turn follows it; once the next run
            # produced output it is stale and skipped (it stays in JSONL).
            if is_partial_thinking_note(message) and index < last_assistant_index:
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

        # Reasoning-only assistant turns whose reasoning is not replayed would
        # become empty request entries — skip them. Under ``full_history`` a
        # same-model reasoning-only turn keeps its (signed) reasoning blocks
        # and must stay in the request history.
        if _is_empty_assistant_history_message(
            message,
            replay_policy=replay_policy,
            agent_model=agent_model,
        ):
            continue

        if deferred_until_after_tools:
            request_messages.append(_notes_to_synthetic_user_message(deferred_until_after_tools))
            deferred_until_after_tools = []

        if pending_notes:
            request_messages.append(_notes_to_synthetic_user_message(pending_notes))
            pending_notes = []
        request_messages.append(
            _message_to_request_dict(
                message,
                replay_policy=replay_policy,
                agent_model=agent_model,
            )
        )

    if deferred_until_after_tools:
        request_messages.append(_notes_to_synthetic_user_message(deferred_until_after_tools))

    if pending_notes:
        request_messages.append(_notes_to_synthetic_user_message(pending_notes))

    return request_messages


def _repair_dangling_tool_calls(request_messages: list[JsonObject]) -> list[JsonObject]:
    """Ensure every assistant tool_call_id is answered before the next non-tool message.

    If a session history contains an assistant turn with ``tool_calls`` whose
    results were never persisted (e.g. cancelled run, process kill, or write-side
    bug), providers reject the malformed history with HTTP 400 and the session
    becomes unusable. This post-pass synthesizes a stable failure envelope for
    every missing ``tool_call_id`` immediately after the dangling assistant
    turn, in the assistant's original tool-call order. The synthesized entries
    exist only in the request payload — they are never written to JSONL.
    """
    repaired: list[JsonObject] = []
    pending_tool_calls: list[JsonObject] = []
    for message in request_messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            _flush_pending_tool_calls(repaired, pending_tool_calls)
            pending_tool_calls = list(_iter_assistant_tool_calls(message))
            repaired.append(message)
            continue
        if message.get("role") == "tool":
            repaired.append(message)
            continue
        _flush_pending_tool_calls(repaired, pending_tool_calls)
        pending_tool_calls = []
        repaired.append(message)
    _flush_pending_tool_calls(repaired, pending_tool_calls)
    return repaired


def _flush_pending_tool_calls(
    output: list[JsonObject], pending_tool_calls: list[JsonObject]
) -> None:
    """Synthesize a tool result for every pending call not yet answered by output."""
    answered_ids = _answered_tool_call_ids(output)
    for tool_call in pending_tool_calls:
        if tool_call.get("id") in answered_ids:
            continue
        output.append(_synthesize_interrupted_tool_result(tool_call))


def _answered_tool_call_ids(messages: list[JsonObject]) -> set[str]:
    answered: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            answered.add(tool_call_id)
    return answered


def _iter_assistant_tool_calls(message: JsonObject) -> list[JsonObject]:
    raw_tool_calls = message.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []
    return [tool_call for tool_call in raw_tool_calls if isinstance(tool_call, dict)]


def _synthesize_interrupted_tool_result(tool_call: JsonObject) -> JsonObject:
    tool_name = tool_call.get("name")
    name = tool_name if isinstance(tool_name, str) and tool_name else "unknown"
    envelope = tool_failure(
        INTERRUPTED_TOOL_RESULT_CODE,
        INTERRUPTED_TOOL_RESULT_MESSAGE,
    )
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", ""),
        "name": name,
        "content": json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
    }


def _notes_to_synthetic_user_message(notes: list[ChatMessage]) -> JsonObject:
    return {
        "role": "user",
        "content": "\n".join(_system_reminder_block(note) for note in notes),
    }


def _is_empty_assistant_history_message(
    message: ChatMessage,
    *,
    replay_policy: ReasoningReplayPolicy = REASONING_REPLAY_CURRENT_RUN,
    agent_model: str | None = None,
) -> bool:
    if message.role != "assistant" or message.content is not None or message.tool_calls:
        return False
    has_reasoning = message.reasoning is not None or message.reasoning_meta is not None
    return not (has_reasoning and _replays_assistant_reasoning(message, replay_policy, agent_model))


def _system_reminder_block(message: ChatMessage) -> str:
    message.validate()
    content = message.content
    if isinstance(content, str) and content.startswith(PARTIAL_THINKING_NOTE_PREFIX):
        content = content.removeprefix(PARTIAL_THINKING_NOTE_PREFIX)
    return f"{SYSTEM_REMINDER_OPEN_TAG}\n{content}\n{SYSTEM_REMINDER_CLOSE_TAG}"


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
        "sender",
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
        "sender",
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
        "sender",
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
        "sender",
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
        "sender",
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
        "sender",
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
        "sender",
    )


def _reject_fields(message: ChatMessage, *fields: str) -> None:
    for field_name in fields:
        if getattr(message, field_name) is not None:
            raise ChatMessageValidationError(f"{message.role} messages cannot include {field_name}")
