"""Canonical chat message data model, validation, and effective-history projection."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
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
from core.chat.output_files import AssistantFileReference
from core.providers.adapter import (
    TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD,
    TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD,
    TOOL_CALL_REJECTION_FIELD,
)
from core.sessions import (
    ChatSession,
    active_session_messages,
    is_skill_context_note,
    skill_tool_activation,
)

INTERRUPTION_CAUSES = frozenset(
    {
        "user",
        "provider",
        "network",
        "timeout",
        "process_restart",
        "internal",
    }
)

MessageRole = Literal[
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
]
InputOrigin = Literal["speech_transcription"]
ReplySurfaceKind = Literal["webui", "channel"]
ConversationKind = Literal["direct", "group"]
GroupRole = Literal["admin", "member"]
JsonObject = dict[str, Any]

USAGE_INPUT_TOKENS_ESTIMATED_FIELD = "input_tokens_estimated"
USAGE_OUTPUT_TOKENS_ESTIMATED_FIELD = "output_tokens_estimated"
_USAGE_ESTIMATION_FIELDS = {
    "input_tokens": USAGE_INPUT_TOKENS_ESTIMATED_FIELD,
    "output_tokens": USAGE_OUTPUT_TOKENS_ESTIMATED_FIELD,
}

TIMESTAMP_SUFFIX = "+00:00"
UTC_Z_SUFFIX = "Z"
COMPACTION_SUMMARY_NOTE_PREFIX = "[compaction-summary] "
COMPACTION_SKILL_NOTE_PREFIX = "[compaction-skills] "
TOOL_RESULT_COMPACTED_FIELD = "_vbot_compacted_tool_result"
HISTORY_COMPACTION_GUIDANCE = (
    "This is Compaction checkpoint {ordinal}. Some earlier original messages are no longer "
    "directly present in active Context. If current work depends on earlier decisions, "
    "requirements, exact wording, or completed work, use history to verify the relevant "
    "originals before proceeding. Use checkpoint {ordinal} for the section immediately before "
    "this checkpoint; omit checkpoint to access all earlier original history."
)
REPLY_SURFACE_NOTE_PREFIX = "[reply-surface] "

WEBUI_REPLY_SURFACE_REMINDER = (
    "To show the user an image or provide a file download, include "
    "file:<filesystem-path> in your reply; vBot renders it automatically."
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
class ToolCallRejection:
    """Why one canonical Provider Tool Call must not cross the dispatch boundary."""

    code: str
    message: str
    fingerprint: str

    def to_dict(self) -> JsonObject:
        return {
            "code": self.code,
            "message": self.message,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ToolCallRejection:
        if not isinstance(data, dict):
            raise ChatMessageValidationError("tool call rejection must be an object")
        return cls(
            code=_require_string(data, "code"),
            message=_require_string(data, "message"),
            fingerprint=_require_string(data, "fingerprint"),
        )


@dataclass(frozen=True)
class ToolCall:
    """A canonical assistant-requested tool call."""

    id: str
    name: str
    arguments: JsonObject = field(default_factory=dict)
    rejection: ToolCallRejection | None = None
    argument_sequence_index: int | None = None
    argument_sequence_length: int | None = None

    def __post_init__(self) -> None:
        _validate_tool_call_argument_sequence(
            self.argument_sequence_index,
            self.argument_sequence_length,
        )

    def to_dict(self) -> JsonObject:
        """Return a JSON-serializable tool call dictionary."""
        result: JsonObject = {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }
        if self.rejection is not None:
            result[TOOL_CALL_REJECTION_FIELD] = self.rejection.to_dict()
        if self.argument_sequence_index is not None:
            result[TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD] = self.argument_sequence_index
            result[TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD] = self.argument_sequence_length
        return result

    @classmethod
    def from_dict(cls, data: JsonObject) -> ToolCall:
        """Build a tool call from a JSON object."""
        tool_call_id = _require_string(data, "id")
        name = _require_string(data, "name")
        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ChatMessageValidationError("tool call arguments must be an object")
        rejection_data = data.get(TOOL_CALL_REJECTION_FIELD)
        rejection = (
            ToolCallRejection.from_dict(rejection_data) if rejection_data is not None else None
        )
        sequence_index, sequence_length = _parse_tool_call_argument_sequence(data)
        return cls(
            id=tool_call_id,
            name=name,
            arguments=dict(arguments),
            rejection=rejection,
            argument_sequence_index=sequence_index,
            argument_sequence_length=sequence_length,
        )


@dataclass(frozen=True)
class MessageSender:
    """Platform identity of the human who sent a user message.

    Captured from platform metadata (never from message text) so request-time
    attribution tags cannot be spoofed by typing a look-alike prefix.
    """

    id: str
    display_name: str
    role: GroupRole = "member"

    def to_dict(self) -> JsonObject:
        """Return a JSON-serializable sender dictionary."""
        return {"id": self.id, "display_name": self.display_name, "role": self.role}

    @classmethod
    def from_dict(cls, data: JsonObject) -> MessageSender:
        """Build a sender from a JSON object."""
        sender_id = data.get("id")
        if not isinstance(sender_id, str) or not sender_id:
            raise ChatMessageValidationError("sender id must be a non-empty string")
        display_name = data.get("display_name")
        if not isinstance(display_name, str) or not display_name:
            raise ChatMessageValidationError("sender display_name must be a non-empty string")
        role = data.get("role", "member")
        if role not in ("admin", "member"):
            raise ChatMessageValidationError("sender role must be admin or member")
        return cls(id=sender_id, display_name=display_name, role=cast(GroupRole, role))


@dataclass(frozen=True)
class ReplySurface:
    """Immutable identity and rendering facts for one interactive reply destination."""

    kind: ReplySurfaceKind
    platform: str | None = None
    platform_display_name: str | None = None
    channel_id: str | None = None
    conversation_kind: ConversationKind | None = None

    def __post_init__(self) -> None:
        if self.kind == "webui":
            if any(
                value is not None
                for value in (
                    self.platform,
                    self.platform_display_name,
                    self.channel_id,
                    self.conversation_kind,
                )
            ):
                raise ChatError("WebUI reply surfaces cannot include Channel fields")
            return
        if self.kind != "channel":
            raise ChatError(f"unsupported reply surface kind: {self.kind}")
        if not self.platform or not self.platform_display_name or not self.channel_id:
            raise ChatError("channel reply surface fields must be non-empty")
        if self.conversation_kind not in ("direct", "group"):
            raise ChatError("channel reply surface conversation_kind must be direct or group")

    @classmethod
    def webui(cls) -> ReplySurface:
        """Return the shared WebUI/Desktop reply surface."""
        return cls(kind="webui")

    @classmethod
    def channel(
        cls,
        *,
        platform: str,
        platform_display_name: str,
        channel_id: str,
        conversation_kind: ConversationKind = "direct",
    ) -> ReplySurface:
        """Return one configured Channel reply surface."""
        return cls(
            kind="channel",
            platform=platform,
            platform_display_name=platform_display_name,
            channel_id=channel_id,
            conversation_kind=conversation_kind,
        )

    @property
    def identity(self) -> tuple[str, ...]:
        """Return the stable identity used to detect reply-surface switches."""
        if self.kind == "webui":
            return (self.kind,)
        return (
            self.kind,
            cast(str, self.platform),
            cast(str, self.channel_id),
            cast(str, self.conversation_kind),
        )

    def to_note_content(self) -> str:
        """Encode this surface as one tagged append-only Session note."""
        payload: JsonObject = {"kind": self.kind}
        if self.kind == "channel":
            payload.update(
                {
                    "platform": self.platform,
                    "platform_display_name": self.platform_display_name,
                    "channel_id": self.channel_id,
                    "conversation_kind": self.conversation_kind,
                }
            )
        return REPLY_SURFACE_NOTE_PREFIX + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )

    def reminder_text(self) -> str:
        """Render the exact model-facing reminder for this destination."""
        if self.kind == "webui":
            return WEBUI_REPLY_SURFACE_REMINDER
        conversation_kind = cast(ConversationKind, self.conversation_kind)
        opening = (
            f"The current conversation is a group chat on {self.platform_display_name}."
            if conversation_kind == "group"
            else f"The current conversation is a direct message on {self.platform_display_name}."
        )
        return (
            f"{opening} Your reply to the following request will be delivered via "
            f"{self.platform_display_name} using channel `{self.channel_id}`. "
            "Return normal reply text; vBot delivers it automatically. To deliver any file, "
            "always call `channel_send` and include every file path in `file_paths`."
        )


def _compaction_projection_without_provider_state(
    projection: Sequence[ChatMessage],
) -> list[ChatMessage]:
    """Make a provider-neutral checkpoint projection.

    vBot Compaction is a textual Summary+Tail projection, not a Provider-native
    opaque-state compaction token. Reasoning artifacts therefore end at this
    boundary. Active-Run rebuilds restore their live reasoning fields separately;
    later Runs cannot accidentally treat a textual checkpoint as continuous
    Provider reasoning state.
    """

    projected: list[ChatMessage] = []
    for message in projection:
        if message.role != "assistant":
            projected.append(message)
            continue
        sanitized = replace(
            message,
            reasoning=None,
            reasoning_meta=None,
            reasoning_scope=None,
            reasoning_timing=None,
        )
        if sanitized.content is None and not sanitized.tool_calls:
            continue
        projected.append(sanitized)
    return projected


def _compaction_projection_without_active_skills(
    projection: Sequence[ChatMessage],
    *,
    activation_result_names: Mapping[str, str] | None = None,
) -> list[ChatMessage]:
    """Expire Skill instructions while preserving complete Tool-call cycles."""
    known_result_names = activation_result_names or {}
    projected: list[ChatMessage] = []
    for message in projection:
        if is_skill_context_note(message):
            continue
        activation = skill_tool_activation(message)
        skill_name = known_result_names.get(message.id)
        if skill_name is None and activation is not None:
            skill_name = activation[0]
        if skill_name is not None:
            projected.append(
                replace(
                    message,
                    content=_compacted_skill_activation_result(message, skill_name),
                )
            )
            continue
        projected.append(message)
    return projected


def _compacted_skill_activation_result(message: ChatMessage, skill_name: str) -> str:
    content = message.content if isinstance(message.content, str) else ""
    return json.dumps(
        {
            TOOL_RESULT_COMPACTED_FIELD: True,
            "message_id": message.id,
            "tool": message.name,
            "original_chars": len(content),
            "outcome": {
                "name": skill_name,
                "status": "loaded",
                "compacted": True,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def has_unconsumed_skill_activation(messages: Sequence[ChatMessage]) -> bool:
    """Return whether the latest Assistant Tool batch freshly loaded a Skill."""
    assistant_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role == "assistant"
        ),
        None,
    )
    if assistant_index is None:
        return False
    assistant_message = messages[assistant_index]
    pending_call_ids = {call.id for call in assistant_message.tool_calls or []}
    if not pending_call_ids:
        return False
    return any(
        message.role == "tool"
        and message.tool_call_id in pending_call_ids
        and skill_tool_activation(message) is not None
        for message in messages[assistant_index + 1 :]
    )


@dataclass(frozen=True)
class ChatMessage:
    """Canonical message persisted in the Session database."""

    id: str
    timestamp: str
    role: MessageRole
    content: str | list[ContentBlock] | None = None
    model: str | None = None
    reasoning: str | None = None
    reasoning_meta: JsonObject | None = None
    reasoning_scope: str | None = None
    reasoning_timing: JsonObject | None = None
    phase: str | None = None
    usage: JsonObject | None = None
    timing: JsonObject | None = None
    tool_display: JsonObject | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    error_kind: str | None = None
    tail_boundary_id: str | None = None
    projection: list[JsonObject] | None = None
    compaction_policy: str | None = None
    compaction_strategy: str | None = None
    run_id: str | None = None
    work_id: str | None = None
    status: str | None = None
    iteration_count: int | None = None
    change_stats: JsonObject | None = None
    target_message_id: str | None = None
    sender: MessageSender | None = None
    interrupted: bool = False
    interruption_cause: str | None = None
    output_files: list[AssistantFileReference] | None = None

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
        reasoning_scope: str | None = None,
        reasoning_timing: JsonObject | None = None,
        phase: str | None = None,
        usage: JsonObject | None = None,
        tool_calls: list[ToolCall] | None = None,
        interrupted: bool = False,
        interruption_cause: str | None = None,
        output_files: list[AssistantFileReference] | None = None,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create an assistant message.

        ``interrupted`` marks a turn whose provider stream broke after visible
        output was emitted: the accumulated answer is preserved, but the turn did
        not finish, so the next request continues it (see chat domain map).
        ``interruption_cause`` keeps the normalized reason with that durable
        partial turn for result consumers; neither field reaches Provider wires.
        ``reasoning_timing`` carries the measured first-to-last reasoning delta
        span of a streamed turn; it is presentation metadata like Tool timing.
        """
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="assistant",
            model=model,
            content=content,
            reasoning=reasoning,
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            reasoning_scope=reasoning_scope,
            reasoning_timing=(dict(reasoning_timing) if reasoning_timing is not None else None),
            phase=phase,
            usage=dict(usage) if usage is not None else None,
            tool_calls=list(tool_calls) if tool_calls is not None else None,
            interrupted=interrupted,
            interruption_cause=interruption_cause,
            output_files=list(output_files) if output_files is not None else None,
        )

    @classmethod
    def tool(
        cls,
        *,
        tool_call_id: str,
        name: str,
        content: str,
        timing: JsonObject | None = None,
        tool_display: JsonObject | None = None,
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
            tool_display=dict(tool_display) if tool_display is not None else None,
        )

    @classmethod
    def run_summary(
        cls,
        *,
        run_id: str,
        work_id: str | None = None,
        status: str,
        timing: JsonObject,
        iteration_count: int,
        change_stats: JsonObject | None = None,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create an append-only run summary annotation."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="run_summary",
            run_id=run_id,
            work_id=work_id,
            status=status,
            timing=dict(timing),
            iteration_count=iteration_count,
            change_stats=dict(change_stats) if change_stats is not None else None,
        )

    @classmethod
    def compaction_checkpoint(
        cls,
        *,
        summary: str,
        projection: list[ChatMessage],
        compacted_token_count: int,
        context_tokens_before: int | None = None,
        context_tokens_after: int | None = None,
        policy: str = "custom",
        strategy: str = "custom",
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a self-contained compaction checkpoint projection."""
        if (context_tokens_before is None) != (context_tokens_after is None):
            raise ChatMessageValidationError(
                "compaction checkpoints require both context token counts or neither"
            )
        summary_note = f"{COMPACTION_SUMMARY_NOTE_PREFIX}{summary}"
        projected = _compaction_projection_without_provider_state(
            _compaction_projection_without_active_skills(projection)
        )
        leading_summary = (
            projected[0].content
            if projected and projected[0].role == "note" and isinstance(projected[0].content, str)
            else None
        )
        if not (
            leading_summary == summary_note
            or (
                isinstance(leading_summary, str)
                and leading_summary.startswith(f"{summary_note}\n\n")
            )
        ):
            projected.insert(0, cls.note(summary_note, timestamp=timestamp))
        usage = {"compacted_token_count": compacted_token_count}
        if context_tokens_before is not None and context_tokens_after is not None:
            usage["context_tokens_before"] = context_tokens_before
            usage["context_tokens_after"] = context_tokens_after
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="compaction_checkpoint",
            content=summary,
            usage=usage,
            projection=[message.to_dict() for message in projected],
            compaction_policy=policy,
            compaction_strategy=strategy,
        )

    def with_compaction_context_tokens(
        self,
        *,
        context_tokens_before: int,
        context_tokens_after: int,
    ) -> ChatMessage:
        """Stamp Chat-owned Context Usage onto a completed checkpoint."""
        if self.role != "compaction_checkpoint":
            raise ChatMessageValidationError(
                "context token counts can only be stamped onto compaction checkpoints"
            )
        for field_name, value in (
            ("context_tokens_before", context_tokens_before),
            ("context_tokens_after", context_tokens_after),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ChatMessageValidationError(f"{field_name} must be a non-negative integer")
        stamped = replace(
            self,
            usage={
                **(self.usage or {}),
                "context_tokens_before": context_tokens_before,
                "context_tokens_after": context_tokens_after,
            },
        )
        stamped.validate()
        return stamped

    def with_compaction_duration_ms(self, *, duration_ms: int) -> ChatMessage:
        """Stamp the observed Compaction wall-clock duration onto a checkpoint."""
        if self.role != "compaction_checkpoint":
            raise ChatMessageValidationError(
                "compaction duration can only be stamped onto compaction checkpoints"
            )
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ChatMessageValidationError("duration_ms must be a non-negative integer")
        stamped = replace(
            self,
            usage={
                **(self.usage or {}),
                "compaction_duration_ms": duration_ms,
            },
        )
        stamped.validate()
        return stamped

    @classmethod
    def agent_takeover(
        cls,
        *,
        from_address: str,
        to_address: str,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a persisted takeover divider marking a session move between agents.

        Both endpoints are stored as a compact JSON object in ``content``
        (``{"from": ..., "to": ...}``) using the raw ``agent@projekt`` addresses;
        the accessor composes the localized label from them. Like ``run_summary``
        it is skipped from provider requests, but it stays visible in loaded
        history so the WebUI renders it as a timeline divider.
        """
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="agent_takeover",
            content=json.dumps(
                {"from": from_address, "to": to_address},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    @classmethod
    def history_edit(
        cls,
        target_message_id: str,
        *,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create one append-only active-lineage edit boundary."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="history_edit",
            target_message_id=target_message_id,
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
        _add_if_not_none(message, "reasoning_scope", self.reasoning_scope)
        _add_if_not_none(message, "reasoning_timing", self.reasoning_timing)
        _add_if_not_none(message, "phase", self.phase)
        _add_if_not_none(message, "usage", self.usage)
        _add_if_not_none(message, "timing", self.timing)
        _add_if_not_none(message, "tool_display", self.tool_display)
        if self.tool_calls is not None:
            message["tool_calls"] = [tool_call.to_dict() for tool_call in self.tool_calls]
        _add_if_not_none(message, "tool_call_id", self.tool_call_id)
        _add_if_not_none(message, "name", self.name)
        _add_if_not_none(message, "error_kind", self.error_kind)
        _add_if_not_none(message, "tail_boundary_id", self.tail_boundary_id)
        if self.projection is not None:
            message["projection"] = [dict(entry) for entry in self.projection]
        _add_if_not_none(message, "compaction_policy", self.compaction_policy)
        _add_if_not_none(message, "compaction_strategy", self.compaction_strategy)
        _add_if_not_none(message, "run_id", self.run_id)
        _add_if_not_none(message, "work_id", self.work_id)
        _add_if_not_none(message, "status", self.status)
        _add_if_not_none(message, "iteration_count", self.iteration_count)
        _add_if_not_none(message, "change_stats", self.change_stats)
        _add_if_not_none(message, "target_message_id", self.target_message_id)
        if self.sender is not None:
            message["sender"] = self.sender.to_dict()
        if self.interrupted:
            message["interrupted"] = True
        _add_if_not_none(message, "interruption_cause", self.interruption_cause)
        if self.output_files is not None:
            message["output_files"] = [reference.to_dict() for reference in self.output_files]
        return message

    @classmethod
    def from_dict(cls, data: JsonObject) -> ChatMessage:
        """Build a chat message from a canonical JSON object."""
        role = _require_role(data)
        tool_calls = _parse_tool_calls(data.get("tool_calls"))
        reasoning_meta = data.get("reasoning_meta")
        if reasoning_meta is not None and not isinstance(reasoning_meta, dict):
            raise ChatMessageValidationError("reasoning_meta must be an object")
        reasoning_timing = data.get("reasoning_timing")
        if reasoning_timing is not None and not isinstance(reasoning_timing, dict):
            raise ChatMessageValidationError("reasoning_timing must be an object")
        usage = data.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise ChatMessageValidationError("usage must be an object")
        timing = data.get("timing")
        if timing is not None and not isinstance(timing, dict):
            raise ChatMessageValidationError("timing must be an object")
        tool_display = data.get("tool_display")
        if tool_display is not None and not isinstance(tool_display, dict):
            raise ChatMessageValidationError("tool_display must be an object")
        sender_data = data.get("sender")
        if sender_data is not None and not isinstance(sender_data, dict):
            raise ChatMessageValidationError("sender must be an object")
        interrupted = data.get("interrupted", False)
        if not isinstance(interrupted, bool):
            raise ChatMessageValidationError("interrupted must be a boolean")
        interruption_cause = _optional_string(data, "interruption_cause")
        iteration_count = data.get("iteration_count")
        if "iteration_count" in data and (
            isinstance(iteration_count, bool)
            or not isinstance(iteration_count, int)
            or iteration_count < 0
        ):
            raise ChatMessageValidationError("iteration_count must be a non-negative integer")
        change_stats = data.get("change_stats")
        if change_stats is not None and not isinstance(change_stats, dict):
            raise ChatMessageValidationError("change_stats must be an object")

        projection_data = data.get("projection")
        if projection_data is not None:
            if not isinstance(projection_data, list):
                raise ChatMessageValidationError("projection must be an array")
            if not all(isinstance(entry, dict) for entry in projection_data):
                raise ChatMessageValidationError("projection entries must be objects")

        output_files_data = data.get("output_files")
        if output_files_data is not None and not isinstance(output_files_data, list):
            raise ChatMessageValidationError("output_files must be an array")
        output_files = (
            [AssistantFileReference.from_dict(entry) for entry in output_files_data]
            if output_files_data is not None
            else None
        )

        message = cls(
            id=_require_string(data, "id"),
            timestamp=_require_string(data, "timestamp"),
            role=role,
            content=_parse_content(data),
            model=_optional_string(data, "model"),
            reasoning=_optional_string(data, "reasoning"),
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            reasoning_scope=_optional_string(data, "reasoning_scope"),
            reasoning_timing=(dict(reasoning_timing) if reasoning_timing is not None else None),
            phase=_optional_string(data, "phase"),
            usage=dict(usage) if usage is not None else None,
            timing=dict(timing) if timing is not None else None,
            tool_display=dict(tool_display) if tool_display is not None else None,
            tool_calls=tool_calls,
            tool_call_id=_optional_string(data, "tool_call_id"),
            name=_optional_string(data, "name"),
            error_kind=_optional_string(data, "error_kind"),
            tail_boundary_id=_optional_string(data, "tail_boundary_id"),
            projection=(
                [dict(entry) for entry in projection_data] if projection_data is not None else None
            ),
            compaction_policy=_optional_string(data, "compaction_policy"),
            compaction_strategy=_optional_string(data, "compaction_strategy"),
            run_id=_optional_string(data, "run_id"),
            work_id=_optional_string(data, "work_id"),
            status=_optional_string(data, "status"),
            iteration_count=iteration_count,
            change_stats=dict(change_stats) if change_stats is not None else None,
            target_message_id=_optional_string(data, "target_message_id"),
            sender=MessageSender.from_dict(sender_data) if sender_data is not None else None,
            interrupted=interrupted,
            interruption_cause=interruption_cause,
            output_files=output_files,
        )
        message.validate()
        return message

    def validate(self) -> None:
        """Validate this message against the role-specific canonical schema."""
        _validate_core_fields(self)
        if self.interrupted and self.role != "assistant":
            raise ChatMessageValidationError(f"{self.role} messages cannot include interrupted")
        if self.interruption_cause is not None:
            if self.role != "assistant" or not self.interrupted:
                raise ChatMessageValidationError(
                    "interruption_cause requires an interrupted assistant message"
                )
            if self.interruption_cause not in INTERRUPTION_CAUSES:
                raise ChatMessageValidationError(
                    f"invalid interruption_cause: {self.interruption_cause}"
                )
        if self.iteration_count is not None and self.role != "run_summary":
            raise ChatMessageValidationError(f"{self.role} messages cannot include iteration_count")
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
            case "agent_takeover":
                _validate_agent_takeover_message(self)
            case "history_edit":
                _validate_history_edit_message(self)


def error_kind_llm_visible(kind: str) -> bool:
    """Return whether an error kind should be included in later provider context."""
    return ERROR_KIND_LLM_VISIBLE.get(kind, False)


QUEUE_DISPLAY_CONTENT_LIMIT = 500


def queue_content_is_editable(content: str | list[ContentBlock]) -> bool:
    """Return whether the Queue preview preserves the complete editable content."""
    return isinstance(content, str) and len(content) <= QUEUE_DISPLAY_CONTENT_LIMIT


def _display_content_preview(content: str | list[ContentBlock]) -> str:
    if isinstance(content, str):
        return content[:QUEUE_DISPLAY_CONTENT_LIMIT]

    text_blocks = [block.text for block in content if isinstance(block, TextBlock) and block.text]
    if not text_blocks:
        return "[attachment]"
    return " ".join(text_blocks)[:QUEUE_DISPLAY_CONTENT_LIMIT]


def _append_input_origin_note(session: ChatSession, input_origin: InputOrigin | None) -> None:
    if input_origin is None:
        return
    if input_origin == INPUT_ORIGIN_SPEECH_TRANSCRIPTION:
        session.add_note(SPEECH_TRANSCRIPTION_SYSTEM_REMINDER)
        return
    raise ChatError(f"unsupported input origin: {input_origin}")


def reply_surface_from_note(message: ChatMessage) -> ReplySurface | None:
    """Recover a reply surface from one tagged note, ignoring every older note form."""
    if message.role != "note" or not isinstance(message.content, str):
        return None
    if not message.content.startswith(REPLY_SURFACE_NOTE_PREFIX):
        return None
    try:
        payload = json.loads(message.content.removeprefix(REPLY_SURFACE_NOTE_PREFIX))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") == "webui":
        return ReplySurface.webui()
    if payload.get("kind") != "channel":
        return None
    platform = payload.get("platform")
    platform_display_name = payload.get("platform_display_name")
    channel_id = payload.get("channel_id")
    conversation_kind = payload.get("conversation_kind", "direct")
    if not isinstance(platform, str) or not platform:
        return None
    if not isinstance(platform_display_name, str) or not platform_display_name:
        return None
    if not isinstance(channel_id, str) or not channel_id:
        return None
    if conversation_kind not in ("direct", "group"):
        return None
    return ReplySurface.channel(
        platform=platform,
        platform_display_name=platform_display_name,
        channel_id=channel_id,
        conversation_kind=cast(ConversationKind, conversation_kind),
    )


def should_append_reply_surface_note(messages: list[ChatMessage], incoming: ReplySurface) -> bool:
    """Return whether an interactive Run needs a fresh surface reminder."""
    latest_surface: ReplySurface | None = None
    latest_surface_index = -1
    latest_checkpoint_index = -1
    for index, message in enumerate(messages):
        if message.role == "compaction_checkpoint":
            latest_checkpoint_index = index
        recovered = reply_surface_from_note(message)
        if recovered is not None:
            latest_surface = recovered
            latest_surface_index = index
    return (
        latest_surface is None
        or latest_surface.identity != incoming.identity
        or latest_checkpoint_index > latest_surface_index
    )


def _append_reply_surface_note(
    session: ChatSession,
    surface: ReplySurface | None,
    *,
    messages: list[ChatMessage],
) -> None:
    if surface is None:
        return
    if should_append_reply_surface_note(messages, surface):
        session.add_note(surface.to_note_content())


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


def _latest_compaction_checkpoint(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role == "compaction_checkpoint":
            return message
    return None


def history_available(messages: Sequence[ChatMessage]) -> bool:
    """Return whether persisted Session history grants the History tool."""
    return any(
        message.role == "compaction_checkpoint" for message in active_session_messages(messages)
    )


def checkpoint_ordinal(messages: Sequence[ChatMessage], checkpoint_id: str) -> int | None:
    """Return a checkpoint's one-based chronological ordinal."""
    ordinal = 0
    for message in active_session_messages(messages):
        if message.role != "compaction_checkpoint":
            continue
        ordinal += 1
        if message.id == checkpoint_id:
            return ordinal
    return None


def finalize_checkpoint_history_guidance(
    checkpoint: ChatMessage,
    *,
    ordinal: int,
) -> ChatMessage:
    """Add the ordinal-specific History guidance to a new checkpoint once."""
    if checkpoint.role != "compaction_checkpoint" or checkpoint.projection is None:
        raise ChatMessageValidationError("History guidance requires a projected checkpoint")
    guidance = HISTORY_COMPACTION_GUIDANCE.format(ordinal=ordinal)
    projection = [dict(entry) for entry in checkpoint.projection]
    leading = ChatMessage.from_dict(projection[0])
    if leading.role != "note" or not isinstance(leading.content, str):
        raise ChatMessageValidationError("checkpoint projection must begin with a summary note")
    if guidance not in leading.content:
        leading = replace(leading, content=f"{leading.content}\n\n{guidance}")
        projection[0] = leading.to_dict()
    return replace(checkpoint, projection=projection)


def _effective_compaction_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Return the latest checkpoint projection plus messages appended after it."""
    messages = active_session_messages(messages)
    checkpoint = _latest_compaction_checkpoint(messages)
    if checkpoint is None:
        return list(messages)
    checkpoint_index = next(
        (index for index, message in enumerate(messages) if message.id == checkpoint.id),
        len(messages),
    )
    if checkpoint.projection is not None:
        projection = [ChatMessage.from_dict(entry) for entry in checkpoint.projection]
    else:
        projection = _legacy_checkpoint_projection(messages, checkpoint, checkpoint_index)
    appended = [
        message
        for message in messages[checkpoint_index + 1 :]
        if message.role != "compaction_checkpoint"
    ]
    effective = [*projection, *appended]
    return _overlay_pending_tool_batch(messages, effective)


def _overlay_pending_tool_batch(
    canonical_messages: Sequence[ChatMessage],
    effective_messages: list[ChatMessage],
) -> list[ChatMessage]:
    """Keep the latest complete unconsumed Tool batch in post-Compaction Context."""
    latest_assistant_index = next(
        (
            index
            for index in range(len(canonical_messages) - 1, -1, -1)
            if canonical_messages[index].role == "assistant"
        ),
        None,
    )
    if latest_assistant_index is None:
        return effective_messages
    carrier = canonical_messages[latest_assistant_index]
    if not carrier.tool_calls:
        return effective_messages

    expected_ids = [tool_call.id for tool_call in carrier.tool_calls]
    results_by_id: dict[str, ChatMessage] = {}
    for message in canonical_messages[latest_assistant_index + 1 :]:
        if message.role == "assistant":
            return effective_messages
        if message.role == "tool" and message.tool_call_id in expected_ids:
            results_by_id.setdefault(cast(str, message.tool_call_id), message)
    if any(tool_call_id not in results_by_id for tool_call_id in expected_ids):
        return effective_messages

    batch = [carrier, *(results_by_id[tool_call_id] for tool_call_id in expected_ids)]
    batch_ids = {message.id for message in batch}
    if batch_ids.issubset({message.id for message in effective_messages}):
        return effective_messages
    without_partial_batch = [
        message for message in effective_messages if message.id not in batch_ids
    ]
    return [*without_partial_batch, *batch]


def _legacy_checkpoint_projection(
    messages: list[ChatMessage], checkpoint: ChatMessage, checkpoint_index: int
) -> list[ChatMessage]:
    """Materialize one old boundary-based checkpoint without rewriting it."""
    summary = checkpoint.content if isinstance(checkpoint.content, str) else ""
    projection = [
        ChatMessage.note(
            f"{COMPACTION_SUMMARY_NOTE_PREFIX}{summary}",
            timestamp=datetime.fromisoformat(checkpoint.timestamp),
        )
    ]
    boundary_id = checkpoint.tail_boundary_id
    boundary_index = next(
        (
            index
            for index, message in enumerate(messages[:checkpoint_index])
            if message.id == boundary_id
        ),
        checkpoint_index,
    )
    projection.extend(
        message
        for message in messages[boundary_index:checkpoint_index]
        if message.role != "compaction_checkpoint"
    )
    return projection


def usage_token_is_estimated(
    usage: Mapping[str, Any],
    token_field: Literal["input_tokens", "output_tokens"],
) -> bool:
    """Return field-level provenance with legacy whole-turn compatibility."""

    estimation_field = _USAGE_ESTIMATION_FIELDS[token_field]
    if estimation_field in usage:
        return usage.get(estimation_field) is True
    if any(field in usage for field in _USAGE_ESTIMATION_FIELDS.values()):
        return False
    return usage.get("estimated") is True


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
        "agent_takeover",
        "history_edit",
    ):
        raise ChatMessageValidationError(
            "role must be system, user, assistant, tool, note, error, "
            "compaction_checkpoint, run_summary, agent_takeover, or history_edit"
        )
    return cast(MessageRole, role)


def _parse_tool_calls(value: Any) -> list[ToolCall] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ChatMessageValidationError("tool_calls must be an array")
    return [ToolCall.from_dict(item) for item in value if _is_tool_call_object(item)]


def _parse_tool_call_argument_sequence(data: JsonObject) -> tuple[int | None, int | None]:
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


def _validate_assistant_message(message: ChatMessage) -> None:
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


def _validate_tool_message(message: ChatMessage) -> None:
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


def _validate_compaction_checkpoint_message(message: ChatMessage) -> None:
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
            projected = ChatMessage.from_dict(entry)
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


def _validate_run_summary_message(message: ChatMessage) -> None:
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


def _validate_change_stats(change_stats: JsonObject) -> None:
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


def _validate_agent_takeover_message(message: ChatMessage) -> None:
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


def _validate_history_edit_message(message: ChatMessage) -> None:
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


def _reject_fields(message: ChatMessage, *fields: str) -> None:
    for field_name in fields:
        if getattr(message, field_name) is not None:
            raise ChatMessageValidationError(f"{message.role} messages cannot include {field_name}")
