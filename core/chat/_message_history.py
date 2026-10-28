"""Canonical history, checkpoint projection and note chronology."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast

from core.chat import messages as _records
from core.chat.errors import ChatError, ChatMessageValidationError
from core.sessions import (
    ChatSession,
    is_skill_context_note,
    skill_tool_activation,
)


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


def has_unconsumed_skill_activation(
    messages: Sequence[_records.ChatMessage], *, run_id: str
) -> bool:
    """Return whether Run *run_id*'s latest Assistant Tool batch freshly loaded a Skill.

    A batch written by an earlier Run never counts: that Run ended before a Model
    step consumed the Result, so waiting for consumption could wait forever.
    """
    assistant_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role == "assistant" and not messages[index].programmatic
        ),
        None,
    )
    if assistant_index is None:
        return False
    assistant_message = messages[assistant_index]
    if assistant_message.run_id != run_id:
        return False
    pending_call_ids = {call.id for call in assistant_message.tool_calls or []}
    if not pending_call_ids:
        return False
    if any(
        not message.programmatic
        and (
            is_skill_context_note(message)
            or (
                message.role == "tool"
                and message.tool_call_id in pending_call_ids
                and skill_tool_activation(message) is not None
            )
        )
        for message in messages[assistant_index + 1 :]
    ):
        return True
    # A streamed call may finish and persist its Skill note before the parent
    # Assistant is durable. Its child Result identifies that parent batch. Do
    # not cross the preceding normal batch: Providers may reuse call ids.
    for index in range(assistant_index - 1, -1, -1):
        message = messages[index]
        if not message.programmatic and message.role in {"assistant", "tool"}:
            break
        if (
            message.programmatic
            and message.run_id == run_id
            and message.parent_tool_call_id in pending_call_ids
            and skill_tool_activation(message) is not None
        ):
            return True
    return False


def _add_context_note(session: ChatSession, note: str | _records.ChatMessage | None) -> None:
    """Store a Run's context note before its input; a prepared note keeps its record."""
    if isinstance(note, _records.ChatMessage):
        session.add_note_message(note)
    elif note:
        session.add_note(note)


def _append_input_origin_note(
    session: ChatSession, input_origin: _records.InputOrigin | None
) -> None:
    if input_origin is None:
        return
    if input_origin == _records.INPUT_ORIGIN_SPEECH_TRANSCRIPTION:
        session.add_note(_records.SPEECH_TRANSCRIPTION_SYSTEM_REMINDER)
        return
    if input_origin == _records.INPUT_ORIGIN_LIVE_VOICE:
        session.add_note(_records.LIVE_VOICE_SYSTEM_REMINDER)
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
    if payload.get("kind") == "tui":
        return _records.ReplySurface.tui()
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


def checkpoint_ordinal(messages: Sequence[_records.ChatMessage], checkpoint_id: str) -> int | None:
    """Return a checkpoint's one-based chronological ordinal."""
    ordinal = 0
    matched = None
    for message in messages:
        if message.role != "compaction_checkpoint":
            continue
        ordinal += 1
        if message.id == checkpoint_id:
            matched = ordinal
    return matched


def finalize_checkpoint_guidance(
    checkpoint: _records.ChatMessage,
    *,
    ordinal: int,
) -> _records.ChatMessage:
    """Add the ordinal-specific checkpoint guidance to a new checkpoint once."""
    if checkpoint.role != "compaction_checkpoint" or checkpoint.projection is None:
        raise ChatMessageValidationError("checkpoint guidance requires a projected checkpoint")
    guidance = _records.COMPACTION_CHECKPOINT_GUIDANCE.format(ordinal=ordinal)
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
    """Return Model context, excluding persisted programmatic Tool activity."""
    messages = [message for message in messages if not message.programmatic]
    checkpoint_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role == "compaction_checkpoint"
        ),
        None,
    )
    if checkpoint_index is None:
        return messages
    checkpoint = messages[checkpoint_index]
    if checkpoint.projection is None:
        raise ChatMessageValidationError("compaction checkpoints require a projection")
    projection = [
        message
        for entry in checkpoint.projection
        if not (message := _records.ChatMessage.from_dict(entry)).programmatic
    ]
    appended = [
        message
        for message in messages[checkpoint_index + 1 :]
        if message.role != "compaction_checkpoint"
    ]
    return _overlay_pending_tool_batch(messages, projection, appended)


def _overlay_pending_tool_batch(
    canonical_messages: Sequence[_records.ChatMessage],
    projection: list[_records.ChatMessage],
    appended: list[_records.ChatMessage],
) -> list[_records.ChatMessage]:
    """Keep the latest complete unconsumed Tool batch in post-Compaction Context.

    The restored batch follows the checkpoint Projection and precedes every
    message appended after it in canonical order, such as steered User input.
    The checkpoint ended every Skill activation, so a restored Skill-loading
    Result becomes the same instruction-free digest a retained one gets.
    """
    effective_messages = [*projection, *appended]
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
    canonical_positions = {id(message): index for index, message in enumerate(canonical_messages)}
    kept_appended = [message for message in appended if message.id not in batch_ids]
    later = next(
        (
            index
            for index, message in enumerate(kept_appended)
            if canonical_positions.get(id(message), latest_assistant_index) > latest_assistant_index
        ),
        len(kept_appended),
    )
    return [
        *(message for message in projection if message.id not in batch_ids),
        *kept_appended[:later],
        *compaction_projection_without_active_skills(batch),
        *kept_appended[later:],
    ]
