"""Canonical history projections for Provider requests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace

from core.chat._message_history import (
    _last_user_message,
    _last_user_message_with_content_blocks,
    _session_has_any_content_blocks,
    effective_compaction_messages,
)
from core.chat.content_blocks import ContentBlock, MediaBlock, content_block_to_dict
from core.chat.messages import (
    ChatMessage,
    JsonObject,
)
from core.chat.tool_dispatch import _read_media_outputs
from core.chat.wire_shaping import (
    RequestImageBudget,
    _embed_notes_into_request,
    limit_request_images,
)
from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.reasoning import ReasoningReplayPolicy


@dataclass(frozen=True)
class _PreparedRequestMessages:
    """CPU-built provider request projection plus its effective canonical source."""

    messages: list[JsonObject]
    effective_messages: list[ChatMessage]


def _prepare_request_messages(
    *,
    system_prompt: str,
    agent_model: str,
    session_messages: list[ChatMessage],
    replay_policy: ReasoningReplayPolicy,
    reasoning_scope_model: str,
) -> _PreparedRequestMessages:
    """Project canonical history without consuming Event-Loop time on large Sessions."""
    system_messages = (
        [ChatMessage.system(system_prompt, agent_model).to_dict()] if system_prompt.strip() else []
    )
    effective_messages = effective_compaction_messages(session_messages)
    history = _embed_notes_into_request(
        effective_messages,
        replay_policy=replay_policy,
        agent_model=reasoning_scope_model,
    )
    return _PreparedRequestMessages(
        messages=[
            *system_messages,
            *history,
        ],
        effective_messages=effective_messages,
    )


def _request_content_resolution_inputs(
    effective_messages: list[ChatMessage],
    session_messages: list[ChatMessage],
) -> tuple[ChatMessage | None, list[JsonObject]]:
    """Find attachment boundaries and Run-local media without loop-bound scans."""
    current_user_message: ChatMessage | None = None
    if _session_has_any_content_blocks(effective_messages):
        current_user_message = _last_user_message_with_content_blocks(
            effective_messages
        ) or _last_user_message(effective_messages)
    return current_user_message, _current_run_read_media_outputs(session_messages)


def _assign_session_image_references(
    content: str | list[ContentBlock],
    session_messages: Sequence[ChatMessage],
) -> str | list[ContentBlock]:
    """Give incoming images their next durable, Session-local reference number."""

    if isinstance(content, str):
        return content

    existing_images = 0
    highest_reference = 0
    for message in session_messages:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if not isinstance(block, MediaBlock) or not block.media_type.startswith("image/"):
                continue
            existing_images += 1
            highest_reference = max(highest_reference, block.image_reference or 0)

    next_reference = max(existing_images, highest_reference) + 1
    assigned: list[ContentBlock] = []
    for block in content:
        if isinstance(block, MediaBlock) and block.media_type.startswith("image/"):
            assigned.append(replace(block, image_reference=next_reference))
            next_reference += 1
        else:
            assigned.append(block)
    return assigned


def _current_run_read_media_outputs(
    messages: list[ChatMessage],
) -> list[JsonObject]:
    """Recover compact media references from the active Run's Tool Results."""

    tail_start = 0
    for index, message in enumerate(messages):
        if message.role == "run_summary":
            tail_start = index + 1

    outputs: list[JsonObject] = []
    for message in messages[tail_start:]:
        if (
            message.role != "tool"
            or not isinstance(message.tool_call_id, str)
            or not isinstance(message.content, str)
        ):
            continue
        try:
            result = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if isinstance(result, dict):
            outputs.extend(
                _read_media_outputs(
                    result,
                    tool_call_id=message.tool_call_id,
                )
            )
    return outputs


def _restore_in_run_tool_result_content(
    rebuilt_messages: list[JsonObject],
    live_messages: list[JsonObject],
    *,
    input_modalities: frozenset[str] | None = None,
    wire_media_types: frozenset[str] | None = None,
    image_budget: RequestImageBudget | None = None,
) -> list[JsonObject]:
    """Restore live Tool pixels after Compaction or a capability-aware fallback."""

    from core.compaction import is_compacted_tool_result_content

    rich_content_by_call_id = {
        message["tool_call_id"]: message[TOOL_RESULT_CONTENT_BLOCKS_FIELD]
        for message in live_messages
        if message.get("role") == "tool"
        and isinstance(message.get("tool_call_id"), str)
        and isinstance(message.get(TOOL_RESULT_CONTENT_BLOCKS_FIELD), list)
    }
    for message in rebuilt_messages:
        tool_call_id = message.get("tool_call_id")
        if (
            message.get("role") == "tool"
            and isinstance(tool_call_id, str)
            and tool_call_id in rich_content_by_call_id
            and not is_compacted_tool_result_content(message.get("content"))
            and not (input_modalities is not None and TOOL_RESULT_CONTENT_BLOCKS_FIELD in message)
        ):
            message[TOOL_RESULT_CONTENT_BLOCKS_FIELD] = [
                block
                for block in rich_content_by_call_id[tool_call_id]
                if block.get("type") != "media"
                or (
                    (input_modalities is None or "image" in input_modalities)
                    and (wire_media_types is None or block.get("media_type") in wire_media_types)
                )
            ]
    return limit_request_images(rebuilt_messages, budget=image_budget)


def _serialize_continuation_request(
    content: str | list[ContentBlock] | None,
) -> str | list[JsonObject] | None:
    """Return the canonical JSON form stored by the continuation journal."""
    if isinstance(content, list):
        return [content_block_to_dict(block) for block in content]
    return content
