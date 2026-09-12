"""Semantic Message history and Skill activation projections."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.chat.errors import ChatSessionError
from core.sessions._types import (
    CHANNEL_MESSAGE_NOTE_PREFIX,
    PROJECT_TOOL_LOADED_STATUS,
    PROJECT_TOOL_MESSAGE_NAME,
    SKILL_AVAILABLE_NOTE_PREFIX,
    SKILL_CONTEXT_NOTE_PREFIX,
    SKILL_TOOL_LOADED_STATUS,
    SKILL_TOOL_MESSAGE_NAME,
)
from core.skills.skills import format_skill_activation_context

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage


def active_session_messages(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    active: list[ChatMessage] = []
    for message in messages:
        if message.role == "history_edit":
            active = active[: editable_session_message_index(active, message.target_message_id)]
        else:
            active.append(message)
    return active


def editable_session_message_index(messages: Sequence[ChatMessage], message_id: str | None) -> int:
    if not isinstance(message_id, str) or not message_id:
        raise ChatSessionError("history edit target must be a non-empty message id")
    index = next(
        (index for index, message in enumerate(messages) if message.id == message_id), None
    )
    if index is None:
        raise ChatSessionError(f"history edit target is not active: {message_id}")
    target = messages[index]
    if target.role != "user" or not isinstance(target.content, str) or target.sender is not None:
        raise ChatSessionError("history edit target must be an own plain-text user message")
    latest_takeover = max(
        (index for index, message in enumerate(messages) if message.role == "agent_takeover"),
        default=-1,
    )
    if index <= latest_takeover:
        raise ChatSessionError("history edit target cannot precede the latest agent takeover")
    return index


def editable_session_message_ids(messages: Sequence[ChatMessage]) -> frozenset[str]:
    active = active_session_messages(messages)
    takeover = max(
        (index for index, message in enumerate(active) if message.role == "agent_takeover"),
        default=-1,
    )
    return frozenset(
        message.id
        for index, message in enumerate(active)
        if index > takeover
        and message.role == "user"
        and isinstance(message.content, str)
        and message.sender is None
    )


def _skill_context_note_content(name: str, content: str) -> str:
    return SKILL_CONTEXT_NOTE_PREFIX + json.dumps(
        {"name": name, "content": content}, ensure_ascii=False, separators=(",", ":")
    )


def is_skill_context_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(SKILL_CONTEXT_NOTE_PREFIX)
    )


def skill_context_note_payload(message: ChatMessage) -> tuple[str, str] | None:
    if not is_skill_context_note(message) or not isinstance(message.content, str):
        return None
    try:
        data = json.loads(message.content.removeprefix(SKILL_CONTEXT_NOTE_PREFIX))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name, content = data.get("name"), data.get("content")
    return (name, content) if isinstance(name, str) and name and isinstance(content, str) else None


def skill_context_note_name(message: ChatMessage) -> str | None:
    payload = skill_context_note_payload(message)
    return payload[0] if payload else None


def skill_tool_activation(message: ChatMessage) -> tuple[str, str] | None:
    if (
        message.role != "tool"
        or message.name != SKILL_TOOL_MESSAGE_NAME
        or not isinstance(message.content, str)
    ):
        return None
    try:
        envelope = json.loads(message.content)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        return None
    data = envelope.get("data")
    if not isinstance(data, dict) or data.get("status") != SKILL_TOOL_LOADED_STATUS:
        return None
    name, content = data.get("name"), data.get("content")
    if not isinstance(name, str) or not name or not isinstance(content, str) or not content:
        return None
    resources = data.get("resource_files")
    files: list[str] = []
    guidance = ""
    if resources is not None:
        if not isinstance(resources, dict):
            return None
        resource_guidance = resources.get("guidance")
        resource_files = resources.get("files")
        if (
            not isinstance(resource_guidance, str)
            or not isinstance(resource_files, list)
            or not all(isinstance(file, str) and file for file in resource_files)
        ):
            return None
        guidance = resource_guidance
        files = resource_files
    access = data.get("environment_access", "")
    if not isinstance(access, str):
        return None
    return name, format_skill_activation_context(
        name, content, resource_files=files, resource_guidance=guidance, environment_access=access
    )


def skill_tool_activation_name(message: ChatMessage) -> str | None:
    activation = skill_tool_activation(message)
    return activation[0] if activation else None


def project_tool_context_id(message: ChatMessage) -> str | None:
    if (
        message.role != "tool"
        or message.name != PROJECT_TOOL_MESSAGE_NAME
        or not isinstance(message.content, str)
    ):
        return None
    try:
        envelope = json.loads(message.content)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        return None
    data = envelope.get("data")
    value = (
        data.get("project_id")
        if isinstance(data, dict) and data.get("status") == PROJECT_TOOL_LOADED_STATUS
        else None
    )
    return value if isinstance(value, str) and value else None


def latest_project_tool_context_id(messages: list[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if project_id := project_tool_context_id(message):
            return project_id
    return None


def _skill_contexts(messages: list[ChatMessage]) -> dict[str, str]:
    result: dict[str, str] = {}
    for message in messages:
        activation = skill_context_note_payload(message) or skill_tool_activation(message)
        if activation:
            result[activation[0]] = activation[1]
    return result


def skill_activation_names(messages: list[ChatMessage]) -> frozenset[str]:
    return frozenset(_skill_contexts(messages))


def skill_activation_contents(messages: list[ChatMessage]) -> dict[str, str]:
    return _skill_contexts(messages)


def current_skill_activation_contents(messages: list[ChatMessage]) -> dict[str, str]:
    checkpoint = max(
        (
            index
            for index, message in enumerate(messages)
            if message.role == "compaction_checkpoint"
        ),
        default=-1,
    )
    return _skill_contexts(messages[checkpoint + 1 :])


def is_channel_message_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(CHANNEL_MESSAGE_NOTE_PREFIX)
    )


def is_skill_available_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(SKILL_AVAILABLE_NOTE_PREFIX)
    )
