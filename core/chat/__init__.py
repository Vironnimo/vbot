"""Chat domain public API."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.chat._message_history import (
        checkpoint_ordinal,
        compaction_projection_without_active_skills,
        compaction_projection_without_provider_state,
        effective_compaction_messages,
        has_unconsumed_skill_activation,
        latest_compaction_checkpoint,
        reply_surface_from_note,
    )
    from core.chat.chat import (
        MAX_TOOL_ITERATIONS,
        ChatLoop,
        ChatLoopDependencies,
        parse_bare_model,
    )
    from core.chat.commands import (
        AgentArgument,
        CommandDispatcher,
        CommandExecutionContext,
        CommandFeedback,
        CommandNavigation,
        CommandOutcome,
        CommandResourceChange,
        CommandRun,
        CommandSpec,
        CommandUnavailability,
        ExtensionCommandContext,
        HandoffArgument,
        PreparedCommand,
        parse_agent_argument,
        parse_handoff_argument,
    )
    from core.chat.continuation import (
        ContinuationState,
    )
    from core.chat.errors import (
        ChatError,
        ChatMessageValidationError,
        ChatSessionError,
        CompactionUnavailableError,
        ToolIterationLimitError,
    )
    from core.chat.messages import (
        INPUT_ORIGIN_SPEECH_TRANSCRIPTION,
        ChatMessage,
        InputOrigin,
        MessageSender,
        ReplySurface,
        ToolCall,
        ToolCallRejection,
        queue_content_is_editable,
    )
    from core.chat.usage import (
        aggregate_session_usage,
        latest_session_context_usage,
    )
    from core.sessions import (
        ChatSession,
        ChatSessionManager,
    )

_EXPORT_MODULES = {
    "compaction_projection_without_active_skills": "core.chat._message_history",
    "compaction_projection_without_provider_state": "core.chat._message_history",
    "effective_compaction_messages": "core.chat._message_history",
    "latest_compaction_checkpoint": "core.chat._message_history",
    "checkpoint_ordinal": "core.chat._message_history",
    "has_unconsumed_skill_activation": "core.chat._message_history",
    "reply_surface_from_note": "core.chat._message_history",
    "AgentArgument": "core.chat.commands",
    "ChatError": "core.chat.errors",
    "ChatLoop": "core.chat.chat",
    "ChatLoopDependencies": "core.chat.chat",
    "ChatMessage": "core.chat.messages",
    "ChatMessageValidationError": "core.chat.errors",
    "ChatSession": "core.sessions",
    "ChatSessionError": "core.chat.errors",
    "ChatSessionManager": "core.sessions",
    "CompactionUnavailableError": "core.chat.errors",
    "CommandDispatcher": "core.chat.commands",
    "CommandExecutionContext": "core.chat.commands",
    "CommandFeedback": "core.chat.commands",
    "CommandNavigation": "core.chat.commands",
    "CommandOutcome": "core.chat.commands",
    "CommandResourceChange": "core.chat.commands",
    "CommandRun": "core.chat.commands",
    "CommandSpec": "core.chat.commands",
    "CommandUnavailability": "core.chat.commands",
    "ExtensionCommandContext": "core.chat.commands",
    "ContinuationState": "core.chat.continuation",
    "HandoffArgument": "core.chat.commands",
    "INPUT_ORIGIN_SPEECH_TRANSCRIPTION": "core.chat.messages",
    "InputOrigin": "core.chat.messages",
    "MAX_TOOL_ITERATIONS": "core.chat.chat",
    "MessageSender": "core.chat.messages",
    "ReplySurface": "core.chat.messages",
    "PreparedCommand": "core.chat.commands",
    "parse_agent_argument": "core.chat.commands",
    "parse_bare_model": "core.chat.chat",
    "parse_handoff_argument": "core.chat.commands",
    "queue_content_is_editable": "core.chat.messages",
    "ToolCall": "core.chat.messages",
    "ToolCallRejection": "core.chat.messages",
    "ToolIterationLimitError": "core.chat.errors",
    "aggregate_session_usage": "core.chat.usage",
    "latest_session_context_usage": "core.chat.usage",
}

__all__ = [
    "compaction_projection_without_active_skills",
    "compaction_projection_without_provider_state",
    "effective_compaction_messages",
    "latest_compaction_checkpoint",
    "checkpoint_ordinal",
    "has_unconsumed_skill_activation",
    "reply_surface_from_note",
    "AgentArgument",
    "ChatError",
    "ChatLoop",
    "ChatLoopDependencies",
    "ChatMessage",
    "ChatMessageValidationError",
    "ChatSession",
    "ChatSessionError",
    "ChatSessionManager",
    "CompactionUnavailableError",
    "CommandDispatcher",
    "CommandExecutionContext",
    "CommandFeedback",
    "CommandNavigation",
    "CommandOutcome",
    "CommandResourceChange",
    "CommandRun",
    "CommandSpec",
    "CommandUnavailability",
    "ExtensionCommandContext",
    "ContinuationState",
    "HandoffArgument",
    "INPUT_ORIGIN_SPEECH_TRANSCRIPTION",
    "InputOrigin",
    "MAX_TOOL_ITERATIONS",
    "MessageSender",
    "ReplySurface",
    "PreparedCommand",
    "parse_agent_argument",
    "parse_bare_model",
    "parse_handoff_argument",
    "queue_content_is_editable",
    "ToolCall",
    "ToolCallRejection",
    "ToolIterationLimitError",
    "aggregate_session_usage",
    "latest_session_context_usage",
]


def __getattr__(name: str) -> Any:
    try:
        module_name = _EXPORT_MODULES[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
