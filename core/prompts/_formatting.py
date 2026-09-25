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


def _format_channel_list(channels: list[ChannelPromptMetadata]) -> str:
    # No ``- None`` fallback anymore: the ``core:channels`` block is owner
    # ``channel``, so with no enabled channels the whole block gates out (D5). This
    # producer is only invoked when at least one channel is enabled.
    lines: list[str] = []
    for channel in channels:
        target_hint = (
            "default target available"
            if len(channel.allowed_chat_ids) == 1
            else "explicit target required"
        )
        lines.append(f"- {channel.id}: {channel.platform} ({target_hint})")
    return "\n".join(lines)


def _format_skill_catalog(skills: Sequence[SkillPromptMetadata]) -> str:
    # The same origin headings and "- name: description" lines as the skill Tool's list.
    entries = format_skill_catalog_entries(skills)
    body = f"{entries}\n" if entries else ""
    return f"<available_skills>\n{body}</available_skills>"
