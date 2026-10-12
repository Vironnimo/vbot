"""Prompt catalog formatting with stable ordering and framing."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.prompts._types import (
    ChannelPromptMetadata,
    SkillPromptMetadata,
)
from core.skills.skills import format_skill_catalog_entries
from core.tools.model_names import model_tool_name


def _format_tool_list(tool_definitions: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {model_tool_name(definition['name'])}: {definition['description']}"
        for definition in tool_definitions
    )


def _format_on_demand_tool_list(entries: Sequence[tuple[str, str]]) -> str:
    """List On-demand Tools as ``- name: summary``, in the given (sorted) order."""
    return "\n".join(
        f"- {model_tool_name(name)}: {summary}" if summary else f"- {model_tool_name(name)}"
        for name, summary in entries
    )


def _format_channel_list(channels: list[ChannelPromptMetadata]) -> str:
    # The ``core:channels`` block requires this producer, so with no enabled
    # channels the whole block renders nothing.
    return "\n".join(_channel_line(channel) for channel in channels)


def _channel_line(channel: ChannelPromptMetadata) -> str:
    """The line the ``core:channels`` block shows for one enabled channel."""
    target_hint = (
        "default target available"
        if len(channel.allowed_chat_ids) == 1
        else "explicit target required"
    )
    return f"- {channel.id}: {channel.platform} ({target_hint})"


def _format_skill_catalog(skills: Sequence[SkillPromptMetadata]) -> str:
    # The same origin headings and "- name: description" lines as the skill Tool's list.
    entries = format_skill_catalog_entries(skills)
    body = f"{entries}\n" if entries else ""
    return f"<available_skills>\n{body}</available_skills>"
