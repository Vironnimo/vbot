"""Canonical Message records, constructors and JSON round-trip."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from core.chat import _message_history, _message_validation
from core.chat._message_records import (
    _USAGE_ESTIMATION_FIELDS as _USAGE_ESTIMATION_FIELDS,
)
from core.chat._message_records import (
    COMPACTION_CHECKPOINT_GUIDANCE as COMPACTION_CHECKPOINT_GUIDANCE,
)
from core.chat._message_records import (
    COMPACTION_SKILL_NOTE_PREFIX as COMPACTION_SKILL_NOTE_PREFIX,
)
from core.chat._message_records import (
    COMPACTION_SUMMARY_END_MARKER as COMPACTION_SUMMARY_END_MARKER,
)
from core.chat._message_records import (
    COMPACTION_SUMMARY_NOTE_PREFIX as COMPACTION_SUMMARY_NOTE_PREFIX,
)
from core.chat._message_records import (
    CONTEXT_ESTIMATION_FIELD as CONTEXT_ESTIMATION_FIELD,
)
from core.chat._message_records import (
    ERROR_KIND_AUTH as ERROR_KIND_AUTH,
)
from core.chat._message_records import (
    ERROR_KIND_CONFIG as ERROR_KIND_CONFIG,
)
from core.chat._message_records import (
    ERROR_KIND_LLM_VISIBLE as ERROR_KIND_LLM_VISIBLE,
)
from core.chat._message_records import (
    ERROR_KIND_NETWORK as ERROR_KIND_NETWORK,
)
from core.chat._message_records import (
    ERROR_KIND_PROVIDER_ERROR as ERROR_KIND_PROVIDER_ERROR,
)
from core.chat._message_records import (
    ERROR_KIND_PROVIDER_FATAL as ERROR_KIND_PROVIDER_FATAL,
)
from core.chat._message_records import (
    ERROR_KIND_PROVIDER_OVERLOAD as ERROR_KIND_PROVIDER_OVERLOAD,
)
from core.chat._message_records import (
    ERROR_KIND_RATE_LIMIT as ERROR_KIND_RATE_LIMIT,
)
from core.chat._message_records import (
    ERROR_KIND_TIMEOUT as ERROR_KIND_TIMEOUT,
)
from core.chat._message_records import (
    ERROR_KIND_TOOL_ITERATIONS as ERROR_KIND_TOOL_ITERATIONS,
)
from core.chat._message_records import (
    INPUT_ORIGIN_LIVE_VOICE as INPUT_ORIGIN_LIVE_VOICE,
)
from core.chat._message_records import (
    INPUT_ORIGIN_SPEECH_TRANSCRIPTION as INPUT_ORIGIN_SPEECH_TRANSCRIPTION,
)
from core.chat._message_records import (
    INPUT_ORIGINS as INPUT_ORIGINS,
)
from core.chat._message_records import (
    INTERRUPTION_CAUSES as INTERRUPTION_CAUSES,
)
from core.chat._message_records import (
    LIVE_VOICE_SYSTEM_REMINDER as LIVE_VOICE_SYSTEM_REMINDER,
)
from core.chat._message_records import (
    PARENT_AGENT_INPUT_SYSTEM_REMINDER as PARENT_AGENT_INPUT_SYSTEM_REMINDER,
)
from core.chat._message_records import (
    PARENT_AGENT_STEERING_SYSTEM_REMINDER as PARENT_AGENT_STEERING_SYSTEM_REMINDER,
)
from core.chat._message_records import (
    REPLY_SURFACE_NOTE_PREFIX as REPLY_SURFACE_NOTE_PREFIX,
)
from core.chat._message_records import (
    SPEECH_TRANSCRIPTION_SYSTEM_REMINDER as SPEECH_TRANSCRIPTION_SYSTEM_REMINDER,
)
from core.chat._message_records import (
    SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER as SUBAGENT_REPLY_TO_USER_SYSTEM_REMINDER,
)
from core.chat._message_records import (
    TIMESTAMP_SUFFIX as TIMESTAMP_SUFFIX,
)
from core.chat._message_records import (
    TOOL_RESULT_COMPACTED_FIELD as TOOL_RESULT_COMPACTED_FIELD,
)
from core.chat._message_records import (
    TUI_REPLY_SURFACE_REMINDER as TUI_REPLY_SURFACE_REMINDER,
)
from core.chat._message_records import (
    USAGE_INPUT_TOKENS_ESTIMATED_FIELD as USAGE_INPUT_TOKENS_ESTIMATED_FIELD,
)
from core.chat._message_records import (
    USAGE_OUTPUT_TOKENS_ESTIMATED_FIELD as USAGE_OUTPUT_TOKENS_ESTIMATED_FIELD,
)
from core.chat._message_records import (
    UTC_Z_SUFFIX as UTC_Z_SUFFIX,
)
from core.chat._message_records import (
    WEBUI_REPLY_SURFACE_REMINDER as WEBUI_REPLY_SURFACE_REMINDER,
)
from core.chat._message_records import (
    ConversationKind as ConversationKind,
)
from core.chat._message_records import (
    GoalEvent as GoalEvent,
)
from core.chat._message_records import (
    GroupRole as GroupRole,
)
from core.chat._message_records import (
    InputOrigin as InputOrigin,
)
from core.chat._message_records import (
    InterruptionCause as InterruptionCause,
)
from core.chat._message_records import (
    JsonObject as JsonObject,
)
from core.chat._message_records import (
    MessageRole as MessageRole,
)
from core.chat._message_records import (
    MessageSender as MessageSender,
)
from core.chat._message_records import (
    ModelFallback as ModelFallback,
)
from core.chat._message_records import ReplySurface as ReplySurface
from core.chat._message_records import (
    ReplySurfaceKind as ReplySurfaceKind,
)
from core.chat._message_records import (
    ToolCall as ToolCall,
)
from core.chat._message_records import (
    ToolCallRejection as ToolCallRejection,
)
from core.chat.content_blocks import (
    ContentBlock,
    TextBlock,
    content_block_to_dict,
)
from core.chat.errors import ChatMessageValidationError
from core.chat.output_files import AssistantFileReference
from core.skills.policy import SkillPackageRef
from core.utils.ids import new_id
from core.utils.timestamps import format_canonical_timestamp


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
    reasoning_summary: list[str] | None = None
    phase: str | None = None
    usage: JsonObject | None = None
    timing: JsonObject | None = None
    tool_display: JsonObject | None = None
    # Stored copies of the images a Tool returned, sent with its Result in later requests.
    tool_media: list[JsonObject] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    error_kind: str | None = None
    projection: list[JsonObject] | None = None
    compaction_policy: str | None = None
    compaction_strategy: str | None = None
    run_id: str | None = None
    work_id: str | None = None
    status: str | None = None
    iteration_count: int | None = None
    change_stats: JsonObject | None = None
    # Why a cancelled or interrupted Run ended: a cancel reason or an interruption cause.
    completion_reason: str | None = None
    target_message_id: str | None = None
    sender: MessageSender | None = None
    # How a user message was entered: dictated, or passed on by Live voice.
    input_origin: InputOrigin | None = None
    interrupted: bool = False
    interruption_cause: str | None = None
    output_files: list[AssistantFileReference] | None = None
    model_fallback: ModelFallback | None = None
    goal_event: GoalEvent | None = None
    # Internal destination captured by Sessions when this entry is persisted.
    subagent_reply_target: str | None = None
    # Actual package loaded by a Skill activation; never sent to the Model.
    skill_package: SkillPackageRef | None = None

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
        input_origin: InputOrigin | None = None,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a user message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="user",
            content=content,
            sender=sender,
            input_origin=input_origin,
        )

    @classmethod
    def note(
        cls,
        content: str,
        *,
        model_fallback: ModelFallback | None = None,
        goal_event: GoalEvent | None = None,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a kernel-internal note message.

        A note that records a Model fallback switch or a goal change also
        carries that record, which History shows in place of the note.
        """
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="note",
            content=content,
            model_fallback=model_fallback,
            goal_event=goal_event,
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
        reasoning_summary: list[str] | None = None,
        phase: str | None = None,
        usage: JsonObject | None = None,
        tool_calls: list[ToolCall] | None = None,
        interrupted: bool = False,
        interruption_cause: str | None = None,
        output_files: list[AssistantFileReference] | None = None,
        timestamp: datetime | None = None,
        message_id: str | None = None,
    ) -> ChatMessage:
        """Create an assistant message.

        ``message_id`` is a pre-allocated id: Chat names the Assistant turn before
        its Tool Calls start, so their events and the turn share one id.

        ``interrupted`` marks a turn whose provider stream broke after visible
        output was emitted: the accumulated answer is preserved, but the turn did
        not finish, so the next request continues it (see chat domain map).
        ``interruption_cause`` keeps the normalized reason with that durable
        partial turn for result consumers; neither field reaches Provider wires.
        ``reasoning_timing`` carries the measured first-to-last reasoning delta
        span of a streamed turn; it is presentation metadata like Tool timing.
        """
        return cls(
            id=message_id or _new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="assistant",
            model=model,
            content=content,
            reasoning=reasoning,
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            reasoning_summary=list(reasoning_summary) if reasoning_summary is not None else None,
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
        tool_media: list[JsonObject] | None = None,
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
            tool_media=[dict(item) for item in tool_media] if tool_media else None,
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
        completion_reason: str | None = None,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a Timeline projection of a persisted Run completion."""
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
            completion_reason=completion_reason,
        )

    @staticmethod
    def validate_change_stats(change_stats: JsonObject) -> None:
        """Validate Run change statistics in the shape a ``run_summary`` carries them."""
        _message_validation._validate_change_stats(change_stats)

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
        projected = _message_history.compaction_projection_without_active_skills(projection)
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
        estimate_factor: float | None = None,
    ) -> ChatMessage:
        """Stamp Chat-owned Context Usage onto a completed checkpoint.

        ``estimate_factor`` is the correction ``context_tokens_after`` used; later
        readers estimate newer messages with it (``CONTEXT_ESTIMATION_FIELD``).
        """
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
        usage = {
            **(self.usage or {}),
            "context_tokens_before": context_tokens_before,
            "context_tokens_after": context_tokens_after,
        }
        if estimate_factor is not None:
            usage[CONTEXT_ESTIMATION_FIELD] = {"factor": estimate_factor}
        stamped = replace(self, usage=usage)
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
        _add_if_not_none(message, "subagent_reply_target", self.subagent_reply_target)
        if self.skill_package is not None:
            message["skill_package"] = self.skill_package.to_dict()
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
        _add_if_not_none(message, "reasoning_summary", self.reasoning_summary)
        _add_if_not_none(message, "phase", self.phase)
        _add_if_not_none(message, "usage", self.usage)
        _add_if_not_none(message, "timing", self.timing)
        _add_if_not_none(message, "tool_display", self.tool_display)
        if self.tool_media is not None:
            message["tool_media"] = [dict(item) for item in self.tool_media]
        if self.tool_calls is not None:
            message["tool_calls"] = [tool_call.to_dict() for tool_call in self.tool_calls]
        _add_if_not_none(message, "tool_call_id", self.tool_call_id)
        _add_if_not_none(message, "name", self.name)
        _add_if_not_none(message, "error_kind", self.error_kind)
        if self.projection is not None:
            message["projection"] = [dict(entry) for entry in self.projection]
        _add_if_not_none(message, "compaction_policy", self.compaction_policy)
        _add_if_not_none(message, "compaction_strategy", self.compaction_strategy)
        _add_if_not_none(message, "run_id", self.run_id)
        _add_if_not_none(message, "work_id", self.work_id)
        _add_if_not_none(message, "status", self.status)
        _add_if_not_none(message, "iteration_count", self.iteration_count)
        _add_if_not_none(message, "change_stats", self.change_stats)
        _add_if_not_none(message, "completion_reason", self.completion_reason)
        _add_if_not_none(message, "target_message_id", self.target_message_id)
        if self.sender is not None:
            message["sender"] = self.sender.to_dict()
        _add_if_not_none(message, "input_origin", self.input_origin)
        if self.interrupted:
            message["interrupted"] = True
        _add_if_not_none(message, "interruption_cause", self.interruption_cause)
        if self.output_files is not None:
            message["output_files"] = [reference.to_dict() for reference in self.output_files]
        if self.model_fallback is not None:
            message["model_fallback"] = self.model_fallback.to_dict()
        if self.goal_event is not None:
            message["goal_event"] = self.goal_event.to_dict()
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
        tool_media = data.get("tool_media")
        if tool_media is not None and (
            not isinstance(tool_media, list)
            or not all(isinstance(item, dict) for item in tool_media)
        ):
            raise ChatMessageValidationError("tool_media must be an array of objects")
        sender_data = data.get("sender")
        if sender_data is not None and not isinstance(sender_data, dict):
            raise ChatMessageValidationError("sender must be an object")
        interrupted = data.get("interrupted", False)
        if not isinstance(interrupted, bool):
            raise ChatMessageValidationError("interrupted must be a boolean")
        interruption_cause = _message_validation._optional_string(data, "interruption_cause")
        input_origin = _message_validation._optional_string(data, "input_origin")
        if input_origin is not None and input_origin not in INPUT_ORIGINS:
            raise ChatMessageValidationError(
                f"input_origin must be one of: {', '.join(sorted(INPUT_ORIGINS))}"
            )
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
        model_fallback_data = data.get("model_fallback")
        model_fallback = (
            ModelFallback.from_dict(model_fallback_data)
            if model_fallback_data is not None
            else None
        )
        goal_event_data = data.get("goal_event")
        goal_event = GoalEvent.from_dict(goal_event_data) if goal_event_data is not None else None
        try:
            package_data = data.get("skill_package")
            skill_package = (
                SkillPackageRef.from_dict(package_data) if package_data is not None else None
            )
        except ValueError as error:
            raise ChatMessageValidationError(str(error)) from error
        if skill_package is not None and role not in {"note", "tool"}:
            raise ChatMessageValidationError("skill_package requires a Note or Tool Result")

        message = cls(
            id=_message_validation._require_string(data, "id"),
            timestamp=_message_validation._require_string(data, "timestamp"),
            role=role,
            subagent_reply_target=_message_validation._optional_string(
                data, "subagent_reply_target"
            ),
            skill_package=skill_package,
            content=_message_validation._parse_content(data),
            model=_message_validation._optional_string(data, "model"),
            reasoning=_message_validation._optional_string(data, "reasoning"),
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            reasoning_summary=data.get("reasoning_summary"),
            reasoning_scope=_message_validation._optional_string(data, "reasoning_scope"),
            reasoning_timing=(dict(reasoning_timing) if reasoning_timing is not None else None),
            phase=_message_validation._optional_string(data, "phase"),
            usage=dict(usage) if usage is not None else None,
            timing=dict(timing) if timing is not None else None,
            tool_display=dict(tool_display) if tool_display is not None else None,
            tool_media=[dict(item) for item in tool_media] if tool_media is not None else None,
            tool_calls=tool_calls,
            tool_call_id=_message_validation._optional_string(data, "tool_call_id"),
            name=_message_validation._optional_string(data, "name"),
            error_kind=_message_validation._optional_string(data, "error_kind"),
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
            completion_reason=_message_validation._optional_string(data, "completion_reason"),
            target_message_id=_message_validation._optional_string(data, "target_message_id"),
            sender=MessageSender.from_dict(sender_data) if sender_data is not None else None,
            input_origin=cast(InputOrigin | None, input_origin),
            interrupted=interrupted,
            interruption_cause=interruption_cause,
            output_files=output_files,
            model_fallback=model_fallback,
            goal_event=goal_event,
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
        if self.completion_reason is not None and (
            self.role != "run_summary"
            or self.status not in {"cancelled", "interrupted"}
            or not self.completion_reason
        ):
            raise ChatMessageValidationError(
                "completion_reason requires a cancelled or interrupted run summary"
            )
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


def queue_content_is_editable(content: str | list[ContentBlock]) -> bool:
    """Return whether the Queue preview preserves the complete editable content."""
    return isinstance(content, str)


def _display_content_preview(content: str | list[ContentBlock]) -> str:
    if isinstance(content, str):
        return content

    text_blocks = [block.text for block in content if isinstance(block, TextBlock) and block.text]
    if not text_blocks:
        return "[attachment]"
    return "\n".join(text_blocks)


def usage_token_is_estimated(
    usage: Mapping[str, Any],
    token_field: Literal["input_tokens", "output_tokens"],
) -> bool:
    """Return whether the Usage marks this primary counter as estimated."""

    return usage.get(_USAGE_ESTIMATION_FIELDS[token_field]) is True


def _new_message_id() -> str:
    return new_id("msg")


def _format_timestamp(timestamp: datetime | None) -> str:
    value = timestamp or datetime.now(UTC)
    if value.tzinfo is None:
        raise ChatMessageValidationError("timestamp must include timezone information")
    return format_canonical_timestamp(value)


def _add_if_not_none(message: JsonObject, key: str, value: Any) -> None:
    if value is not None:
        message[key] = value
