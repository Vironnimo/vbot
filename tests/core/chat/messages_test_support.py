"""Shared imports and canonical fixtures for chat message tests."""

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any

import pytest

from core.chat import (
    ChatError,
    ChatMessage,
    ChatMessageValidationError,
    MessageSender,
    ReplySurface,
    ToolCall,
)
from core.chat.chat import (
    ERROR_KIND_AUTH,
    ERROR_KIND_CONFIG,
    ERROR_KIND_PROVIDER_ERROR,
    ERROR_KIND_PROVIDER_FATAL,
    ERROR_KIND_PROVIDER_OVERLOAD,
    ERROR_KIND_RATE_LIMIT,
    ERROR_KIND_TIMEOUT,
    ERROR_KIND_TOOL_ITERATIONS,
    error_kind_llm_visible,
)
from core.chat.content_blocks import FileBlock, TextBlock
from core.chat.messages import (
    COMPACTION_SUMMARY_END_MARKER,
    HISTORY_COMPACTION_GUIDANCE,
    _effective_compaction_messages,
    checkpoint_ordinal,
    finalize_checkpoint_history_guidance,
    history_available,
    reply_surface_from_note,
    should_append_reply_surface_note,
)
from core.chat.wire_shaping import (
    INTERRUPTED_TOOL_RESULT_CODE,
    INTERRUPTED_TOOL_RESULT_MESSAGE,
    _assistant_continuation_dict,
    _embed_notes_into_request,
    _message_to_request_dict,
    _repair_dangling_tool_calls,
    _restore_in_run_assistant_reasoning,
)
from core.providers.reasoning import (
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_NONE,
)

__all__ = [
    "asyncio",
    "json",
    "FrozenInstanceError",
    "UTC",
    "datetime",
    "Any",
    "pytest",
    "ChatError",
    "ChatMessage",
    "ChatMessageValidationError",
    "MessageSender",
    "ReplySurface",
    "ToolCall",
    "ERROR_KIND_AUTH",
    "ERROR_KIND_CONFIG",
    "ERROR_KIND_PROVIDER_ERROR",
    "ERROR_KIND_PROVIDER_FATAL",
    "ERROR_KIND_PROVIDER_OVERLOAD",
    "ERROR_KIND_RATE_LIMIT",
    "ERROR_KIND_TIMEOUT",
    "ERROR_KIND_TOOL_ITERATIONS",
    "error_kind_llm_visible",
    "FileBlock",
    "TextBlock",
    "COMPACTION_SUMMARY_END_MARKER",
    "HISTORY_COMPACTION_GUIDANCE",
    "INTERRUPTED_TOOL_RESULT_CODE",
    "INTERRUPTED_TOOL_RESULT_MESSAGE",
    "_assistant_continuation_dict",
    "_effective_compaction_messages",
    "_embed_notes_into_request",
    "_message_to_request_dict",
    "_repair_dangling_tool_calls",
    "_restore_in_run_assistant_reasoning",
    "checkpoint_ordinal",
    "finalize_checkpoint_history_guidance",
    "history_available",
    "reply_surface_from_note",
    "should_append_reply_surface_note",
    "REASONING_REPLAY_FULL_HISTORY",
    "REASONING_REPLAY_NONE",
    "FIXED_TIMESTAMP",
    "FIXED_TIMING",
]

FIXED_TIMESTAMP = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)
FIXED_TIMING = {
    "started_at": "2026-05-03T14:30:01+00:00",
    "completed_at": "2026-05-03T14:30:02+00:00",
    "duration_ms": 1234,
}
