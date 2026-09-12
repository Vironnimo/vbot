"""Canonical history, checkpoint projection and note chronology."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import cast

from core.chat import messages as _records
from core.chat.errors import ChatError, ChatMessageValidationError
from core.sessions import (
    ChatSession,
    active_session_messages,
    is_skill_context_note,
    skill_tool_activation,
)


def compaction_projection_without_provider_state(
    projection: Sequence[_records.ChatMessage],
) -> list[_records.ChatMessage]:
    """Make a provider-neutral checkpoint projection.

    vBot Compaction is a textual Summary+Tail projection, not a Provider-native
    opaque-state compaction token. Reasoning artifacts therefore end at this
    boundary. Active-Run rebuilds restore their live reasoning fields separately;
    later Runs cannot accidentally treat a textual checkpoint as continuous
    Provider reasoning state.
    """

    projected: list[_records.ChatMessage] = []
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


def compaction_projection_without_active_skills(
    projection: Sequence[_records.ChatMessage],
    *,
    activation_result_names: Mapping[str, str] | None = None,
) -> list[_records.ChatMessage]:
    """Expire Skill instructions while preserving complete Tool-call cycles."""
    known_result_names = activation_result_names or {}
    projected: list[_records.ChatMessage] = []
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


def _compacted_skill_activation_result(message: _records.ChatMessage, skill_name: str) -> str:
    content = message.content if isinstance(message.content, str) else ""
    return json.dumps(
        {
            _records.TOOL_RESULT_COMPACTED_FIELD: True,
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


def has_unconsumed_skill_activation(messages: Sequence[_records.ChatMessage]) -> bool:
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


def _append_input_origin_note(
    session: ChatSession, input_origin: _records.InputOrigin | None
) -> None:
    if input_origin is None:
        return
    if input_origin == _records.INPUT_ORIGIN_SPEECH_TRANSCRIPTION:
        session.add_note(_records.SPEECH_TRANSCRIPTION_SYSTEM_REMINDER)
        return
    raise ChatError(f"unsupported input origin: {input_origin}")


def reply_surface_from_note(message: _records.ChatMessage) -> _records.ReplySurface | None:
    """Recover a reply surface from one tagged note, ignoring every older note form."""
    if message.role != "note" or not isinstance(message.content, str):
        return None
    if not message.content.startswith(_records.REPLY_SURFACE_NOTE_PREFIX):
        return None
    try:
        payload = json.loads(message.content.removeprefix(_records.REPLY_SURFACE_NOTE_PREFIX))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") == "webui":
        return _records.ReplySurface.webui()
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
    return _records.ReplySurface.channel(
        platform=platform,
        platform_display_name=platform_display_name,
        channel_id=channel_id,
        conversation_kind=cast(_records.ConversationKind, conversation_kind),
    )


def should_append_reply_surface_note(
    messages: list[_records.ChatMessage], incoming: _records.ReplySurface
) -> bool:
    """Return whether an interactive Run needs a fresh surface reminder."""
    latest_surface: _records.ReplySurface | None = None
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
    surface: _records.ReplySurface | None,
    *,
    messages: list[_records.ChatMessage],
) -> None:
    if surface is None:
        return
    if should_append_reply_surface_note(messages, surface):
        session.add_note(surface.to_note_content())


def _last_user_message_with_content_blocks(
    messages: list[_records.ChatMessage],
) -> _records.ChatMessage | None:
    for message in reversed(messages):
        if message.role != "user":
            continue
        if isinstance(message.content, list):
            return message
        return None
    return None


def _last_user_message(messages: list[_records.ChatMessage]) -> _records.ChatMessage | None:
    """Return the most recently appended user message regardless of content type."""
    for message in reversed(messages):
        if message.role == "user":
            return message
    return None


def _session_has_any_content_blocks(messages: list[_records.ChatMessage]) -> bool:
    """Return True if any user message in the session carries list content."""
    return any(message.role == "user" and isinstance(message.content, list) for message in messages)


def latest_compaction_checkpoint(
    messages: list[_records.ChatMessage],
) -> _records.ChatMessage | None:
    for message in reversed(messages):
        if message.role == "compaction_checkpoint":
            return message
    return None


def history_available(messages: Sequence[_records.ChatMessage]) -> bool:
    """Return whether persisted Session history grants the History tool."""
    return any(
        message.role == "compaction_checkpoint" for message in active_session_messages(messages)
    )


def checkpoint_ordinal(messages: Sequence[_records.ChatMessage], checkpoint_id: str) -> int | None:
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
    checkpoint: _records.ChatMessage,
    *,
    ordinal: int,
) -> _records.ChatMessage:
    """Add the ordinal-specific History guidance to a new checkpoint once."""
    if checkpoint.role != "compaction_checkpoint" or checkpoint.projection is None:
        raise ChatMessageValidationError("History guidance requires a projected checkpoint")
    guidance = _records.HISTORY_COMPACTION_GUIDANCE.format(ordinal=ordinal)
    projection = [dict(entry) for entry in checkpoint.projection]
    leading = _records.ChatMessage.from_dict(projection[0])
    if leading.role != "note" or not isinstance(leading.content, str):
        raise ChatMessageValidationError("checkpoint projection must begin with a summary note")
    if guidance not in leading.content:
        content = leading.content
        if content.endswith(_records.COMPACTION_SUMMARY_END_MARKER):
            summary = content.removesuffix(_records.COMPACTION_SUMMARY_END_MARKER).rstrip()
            content = f"{summary}\n\n{guidance}\n{_records.COMPACTION_SUMMARY_END_MARKER}"
        else:
            content = f"{content}\n\n{guidance}"
        leading = replace(leading, content=content)
        projection[0] = leading.to_dict()
    return replace(checkpoint, projection=projection)


def effective_compaction_messages(
    messages: list[_records.ChatMessage],
) -> list[_records.ChatMessage]:
    """Return the latest checkpoint projection plus messages appended after it."""
    messages = active_session_messages(messages)
    checkpoint = latest_compaction_checkpoint(messages)
    if checkpoint is None:
        return list(messages)
    checkpoint_index = next(
        (index for index, message in enumerate(messages) if message.id == checkpoint.id),
        len(messages),
    )
    if checkpoint.projection is not None:
        projection = [_records.ChatMessage.from_dict(entry) for entry in checkpoint.projection]
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
    canonical_messages: Sequence[_records.ChatMessage],
    effective_messages: list[_records.ChatMessage],
) -> list[_records.ChatMessage]:
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
    results_by_id: dict[str, _records.ChatMessage] = {}
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
    messages: list[_records.ChatMessage], checkpoint: _records.ChatMessage, checkpoint_index: int
) -> list[_records.ChatMessage]:
    """Materialize one old boundary-based checkpoint without rewriting it."""
    summary = checkpoint.content if isinstance(checkpoint.content, str) else ""
    projection = [
        _records.ChatMessage.note(
            f"{_records.COMPACTION_SUMMARY_NOTE_PREFIX}{summary}",
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
