"""Prompt catalog formatting with stable ordering and framing."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Any

from core.prompts._types import (
    ChannelPromptMetadata,
    SkillPromptMetadata,
)
from core.skills.skills import (
    SKILL_ORIGIN_AGENT,
    SKILL_ORIGIN_BUNDLED,
    SKILL_ORIGIN_GLOBAL,
    SKILL_ORIGIN_PROJECT_PREFIX,
    skill_origin_sort_key,
)


def _format_tool_list(tool_definitions: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {definition['name']}: {definition['description']}" for definition in tool_definitions
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
    grouped: dict[str | None, list[SkillPromptMetadata]] = {}
    for skill in skills:
        grouped.setdefault(skill.origin, []).append(skill)

    lines = ["<available_skills>"]
    for origin in sorted(grouped, key=skill_origin_sort_key):
        label = escape(_skill_origin_label(origin), quote=True)
        lines.append(f'  <skill_group label="{label}">')
        for skill in grouped[origin]:
            lines.extend(
                [
                    "    <skill>",
                    f"      <name>{escape(skill.name)}</name>",
                    f"      <description>{escape(skill.description)}</description>",
                    "    </skill>",
                ]
            )
        lines.append("  </skill_group>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _skill_origin_label(origin: str | None) -> str:
    """Human header for a skill origin group (path-free, English — a prompt string)."""
    if origin == SKILL_ORIGIN_BUNDLED:
        return "Bundled skills"
    if origin == SKILL_ORIGIN_GLOBAL:
        return "Your global skills"
    if origin is not None and origin.startswith(SKILL_ORIGIN_PROJECT_PREFIX):
        return f"Skills from project '{origin[len(SKILL_ORIGIN_PROJECT_PREFIX) :]}'"
    if origin == SKILL_ORIGIN_AGENT:
        return "Your own skills"
    return "Skills"
