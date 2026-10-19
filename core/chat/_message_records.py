"""Canonical Message value records, validation and JSON round-trip."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args

from core.chat import _message_validation
from core.chat.errors import ChatError, ChatMessageValidationError
from core.providers.adapter import (
    TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD,
    TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD,
    TOOL_CALL_REJECTION_FIELD,
)
from core.utils.timestamps import is_canonical_timestamp

# Why a streamed Assistant turn or a Run stopped before it was complete.
InterruptionCause = Literal["user", "provider", "network", "timeout", "process_restart", "internal"]
INTERRUPTION_CAUSES: frozenset[str] = frozenset(get_args(InterruptionCause))
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
InputOrigin = Literal["speech_transcription", "live_voice"]
ReplySurfaceKind = Literal["webui", "tui", "channel"]
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
COMPACTION_CHECKPOINT_GUIDANCE = (
    "This is Compaction checkpoint {ordinal}. Some earlier original messages are no longer "
    "directly present in active Context; the Session still stores them unchanged."
)
REPLY_SURFACE_NOTE_PREFIX = "[reply-surface] "
WEBUI_REPLY_SURFACE_REMINDER = (
    "Reply surface: WebUI/Desktop. To show an image or provide a file download, include "
    "file:<filesystem-path> in your reply; vBot renders it automatically."
)
TUI_REPLY_SURFACE_REMINDER = (
    "Reply surface: terminal TUI. Markdown and code render in the terminal; images, "
    "HTML previews, audio and video are not embedded. To provide a file download, "
    "include file:<filesystem-path> in your reply."
)
INPUT_ORIGIN_SPEECH_TRANSCRIPTION: InputOrigin = "speech_transcription"
INPUT_ORIGIN_LIVE_VOICE: InputOrigin = "live_voice"
INPUT_ORIGINS: frozenset[str] = frozenset(
    (INPUT_ORIGIN_SPEECH_TRANSCRIPTION, INPUT_ORIGIN_LIVE_VOICE)
)
SPEECH_TRANSCRIPTION_SYSTEM_REMINDER = (
    "The following user message was produced by speech-to-text transcription. "
    "It may contain transcription errors, missing punctuation, or misheard words. "
    "Infer the user's likely intent when appropriate, but do not mention this unless it matters."
)
LIVE_VOICE_SYSTEM_REMINDER = (
    "The following user message was passed on by vBot's voice assistant from a spoken "
    "conversation with the user. It may contain speech recognition errors or the assistant's "
    "rewording of what the user said. Infer the user's likely intent when appropriate, but do "
    "not mention this unless it matters."
)
# Precede input the Parent Agent sends into its Sub-Agent's Session.
PARENT_AGENT_INPUT_SYSTEM_REMINDER = (
    "The following message is from your Parent Agent, the Agent that delegated work to you. "
    "It is not from the user. Your final answers now go to your Parent Agent."
)
PARENT_AGENT_STEERING_SYSTEM_REMINDER = (
    "The following message from your Parent Agent, the Agent that delegated work to you, "
    "arrived while you were working. It is not from the user. Apply its corrections and "
    "additional instructions to your current task. If it asks a question or requests a "
    "status update, answer it with `message_parent` and then resume any unfinished work. "
    "Your final answers now go to your Parent Agent."
)
# Precede a user message that switches a Sub-Agent's answer destination.
SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER = (
    "The user wrote the following message in this Session. Your final answers now go to the "
    "user. You remain connected to your Parent Agent: when it writes to you or you send it "
    "a message with `message_parent`, your final answers go to it again."
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


# Usage field on Assistant steps and Compaction checkpoints: how their Context
# was estimated (the Model's input estimate factor and, for steps, the measured
# anchor), so later readers continue on the same basis (``core/chat/usage.py``).
CONTEXT_ESTIMATION_FIELD = "context_estimation"


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

    Captured from platform metadata, never from message text. The request prefix
    uses this identity; look-alike tags in the message body do not change it or
    the sender role used for authorization.
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
class ModelFallback:
    """The Model switch a fallback note records, for display in the Run's history.

    Presentation only: the note's text is what the Model reads; this record
    never enters a Provider request. ``retry_at`` is the canonical timestamp
    from which new Runs try ``from_model`` again, when it cools down.
    """

    from_model: str
    to_model: str
    retry_at: str | None = None

    def __post_init__(self) -> None:
        for key, value in (("from_model", self.from_model), ("to_model", self.to_model)):
            if not isinstance(value, str) or not value:
                raise ChatMessageValidationError(f"model fallback {key} must be a non-empty string")
        if self.retry_at is not None and not is_canonical_timestamp(self.retry_at):
            raise ChatMessageValidationError("model fallback retry_at must be a timestamp")

    def to_dict(self) -> JsonObject:
        """Return a JSON-serializable fallback dictionary."""
        data: JsonObject = {"from_model": self.from_model, "to_model": self.to_model}
        if self.retry_at is not None:
            data["retry_at"] = self.retry_at
        return data

    @classmethod
    def from_dict(cls, data: Any) -> ModelFallback:
        """Build a fallback record from a JSON object, ignoring unknown fields."""
        if not isinstance(data, dict):
            raise ChatMessageValidationError("model_fallback must be an object")
        # The constructor validates both Models.
        return cls(
            from_model=cast(str, data.get("from_model")),
            to_model=cast(str, data.get("to_model")),
            retry_at=cast("str | None", data.get("retry_at")),
        )


@dataclass(frozen=True)
class GoalEvent:
    """A Session goal change a note records, for display in the conversation.

    Presentation only: the note's text, empty when the Model needs none, is what
    the Model reads; this record never enters a Provider request. ``reason`` is
    a pause reason, ``objective`` the goal a ``set`` names, ``turn`` the number
    of an automatic turn and ``max_turns`` the goal's limit.
    """

    event: str
    reason: str | None = None
    objective: str | None = None
    turn: int | None = None
    max_turns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event, str) or not self.event:
            raise ChatMessageValidationError("goal event must be a non-empty string")
        for key, text in (("reason", self.reason), ("objective", self.objective)):
            if text is not None and (not isinstance(text, str) or not text):
                raise ChatMessageValidationError(f"goal event {key} must be a non-empty string")
        for key, number in (("turn", self.turn), ("max_turns", self.max_turns)):
            if number is not None and (
                isinstance(number, bool) or not isinstance(number, int) or number < 1
            ):
                raise ChatMessageValidationError(f"goal event {key} must be a positive integer")

    def to_dict(self) -> JsonObject:
        """Return a JSON-serializable goal event dictionary without empty fields."""
        data: JsonObject = {"event": self.event}
        for key in ("reason", "objective", "turn", "max_turns"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: Any) -> GoalEvent:
        """Build a goal event from a JSON object, ignoring unknown fields."""
        if not isinstance(data, dict):
            raise ChatMessageValidationError("goal_event must be an object")
        return cls(
            event=cast(str, data.get("event")),
            reason=data.get("reason"),
            objective=data.get("objective"),
            turn=data.get("turn"),
            max_turns=data.get("max_turns"),
        )


@dataclass(frozen=True)
class ReplySurface:
    """Immutable identity and rendering facts for one interactive reply destination."""

    kind: ReplySurfaceKind
    platform: str | None = None
    platform_display_name: str | None = None
    channel_id: str | None = None
    conversation_kind: ConversationKind | None = None
    # Delivery facts captured by Channel ingress for this Run only. They do not
    # change surface identity or enter the persisted reminder/Model context.
    platform_target: str | None = field(default=None, compare=False, repr=False)
    thread_id: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.kind in {"webui", "tui"}:
            if any(
                value is not None
                for value in (
                    self.platform,
                    self.platform_display_name,
                    self.channel_id,
                    self.conversation_kind,
                    self.platform_target,
                    self.thread_id,
                )
            ):
                raise ChatError(f"{self.kind} reply surfaces cannot include Channel fields")
            return
        if self.kind != "channel":
            raise ChatError(f"unsupported reply surface kind: {self.kind}")
        if not self.platform or not self.platform_display_name or not self.channel_id:
            raise ChatError("channel reply surface fields must be non-empty")
        if self.conversation_kind not in ("direct", "group"):
            raise ChatError("channel reply surface conversation_kind must be direct or group")
        if self.platform_target is not None and not self.platform_target:
            raise ChatError("channel reply destination must be non-empty")
        if self.thread_id is not None and (not self.platform_target or not self.thread_id):
            raise ChatError("channel reply thread needs a destination and a non-empty id")

    @classmethod
    def webui(cls) -> ReplySurface:
        """Return the shared WebUI/Desktop reply surface."""
        return cls(kind="webui")

    @classmethod
    def tui(cls) -> ReplySurface:
        """Return the terminal conversation reply surface."""
        return cls(kind="tui")

    @classmethod
    def channel(
        cls,
        *,
        platform: str,
        platform_display_name: str,
        channel_id: str,
        conversation_kind: ConversationKind = "direct",
        platform_target: str | None = None,
        thread_id: str | None = None,
    ) -> ReplySurface:
        """Return one configured Channel reply surface."""
        return cls(
            kind="channel",
            platform=platform,
            platform_display_name=platform_display_name,
            channel_id=channel_id,
            conversation_kind=conversation_kind,
            platform_target=platform_target,
            thread_id=thread_id,
        )

    @property
    def identity(self) -> tuple[str, ...]:
        """Return the stable identity used to detect reply-surface switches."""
        if self.kind in {"webui", "tui"}:
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
        if self.kind == "tui":
            return TUI_REPLY_SURFACE_REMINDER
        conversation_kind = cast(ConversationKind, self.conversation_kind)
        conversation = "group chat" if conversation_kind == "group" else "direct message"
        return (
            f"Reply surface: {self.platform_display_name} {conversation}, "
            f"Channel `{self.channel_id}`. vBot delivers your final text automatically. "
            "Ordinary replies do not attach files."
        )
