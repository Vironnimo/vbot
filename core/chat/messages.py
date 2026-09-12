"""Canonical Message records, constructors and JSON round-trip."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from core.chat import _message_history, _message_validation
from core.chat.content_blocks import (
    ContentBlock,
    TextBlock,
    content_block_to_dict,
)
from core.chat.errors import ChatError, ChatMessageValidationError
from core.chat.output_files import AssistantFileReference
from core.providers.adapter import (
    TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD,
    TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD,
    TOOL_CALL_REJECTION_FIELD,
)
from core.utils.ids import new_id

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
COMPACTION_SUMMARY_END_MARKER = "--- END OF CONTEXT SUMMARY ---"
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
            code=_message_validation._require_string(data, "code"),
            message=_message_validation._require_string(data, "message"),
            fingerprint=_message_validation._require_string(data, "fingerprint"),
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
        _message_validation._validate_tool_call_argument_sequence(
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
        tool_call_id = _message_validation._require_string(data, "id")
        name = _message_validation._require_string(data, "name")
        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ChatMessageValidationError("tool call arguments must be an object")
        rejection_data = data.get(TOOL_CALL_REJECTION_FIELD)
        rejection = (
            ToolCallRejection.from_dict(rejection_data) if rejection_data is not None else None
        )
        sequence_index, sequence_length = _message_validation._parse_tool_call_argument_sequence(
            data
        )
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
        projected = _message_history.compaction_projection_without_provider_state(
            _message_history.compaction_projection_without_active_skills(projection)
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
        role = _message_validation._require_role(data)
        tool_calls = _message_validation._parse_tool_calls(data.get("tool_calls"))
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
        interruption_cause = _message_validation._optional_string(data, "interruption_cause")
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
            id=_message_validation._require_string(data, "id"),
            timestamp=_message_validation._require_string(data, "timestamp"),
            role=role,
            content=_message_validation._parse_content(data),
            model=_message_validation._optional_string(data, "model"),
            reasoning=_message_validation._optional_string(data, "reasoning"),
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            reasoning_scope=_message_validation._optional_string(data, "reasoning_scope"),
            reasoning_timing=(dict(reasoning_timing) if reasoning_timing is not None else None),
            phase=_message_validation._optional_string(data, "phase"),
            usage=dict(usage) if usage is not None else None,
            timing=dict(timing) if timing is not None else None,
            tool_display=dict(tool_display) if tool_display is not None else None,
            tool_calls=tool_calls,
            tool_call_id=_message_validation._optional_string(data, "tool_call_id"),
            name=_message_validation._optional_string(data, "name"),
            error_kind=_message_validation._optional_string(data, "error_kind"),
            tail_boundary_id=_message_validation._optional_string(data, "tail_boundary_id"),
            projection=(
                [dict(entry) for entry in projection_data] if projection_data is not None else None
            ),
            compaction_policy=_message_validation._optional_string(data, "compaction_policy"),
            compaction_strategy=_message_validation._optional_string(data, "compaction_strategy"),
            run_id=_message_validation._optional_string(data, "run_id"),
            work_id=_message_validation._optional_string(data, "work_id"),
            status=_message_validation._optional_string(data, "status"),
            iteration_count=iteration_count,
            change_stats=dict(change_stats) if change_stats is not None else None,
            target_message_id=_message_validation._optional_string(data, "target_message_id"),
            sender=MessageSender.from_dict(sender_data) if sender_data is not None else None,
            interrupted=interrupted,
            interruption_cause=interruption_cause,
            output_files=output_files,
        )
        message.validate()
        return message

    def validate(self) -> None:
        """Validate this message against the role-specific canonical schema."""
        _message_validation._validate_core_fields(self)
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
                _message_validation._validate_system_message(self)
            case "user":
                _message_validation._validate_user_message(self)
            case "assistant":
                _message_validation._validate_assistant_message(self)
            case "tool":
                _message_validation._validate_tool_message(self)
            case "note":
                _message_validation._validate_note_message(self)
            case "error":
                _message_validation._validate_error_message(self)
            case "compaction_checkpoint":
                _message_validation._validate_compaction_checkpoint_message(self)
            case "run_summary":
                _message_validation._validate_run_summary_message(self)
            case "agent_takeover":
                _message_validation._validate_agent_takeover_message(self)
            case "history_edit":
                _message_validation._validate_history_edit_message(self)


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
    return new_id("msg")


def _format_timestamp(timestamp: datetime | None) -> str:
    value = timestamp or datetime.now(UTC)
    if value.tzinfo is None:
        raise ChatMessageValidationError("timestamp must include timezone information")
    return value.astimezone(UTC).isoformat()


def _add_if_not_none(message: JsonObject, key: str, value: Any) -> None:
    if value is not None:
        message[key] = value
