"""System Prompt assembly and editable prompt fragment rules."""

from __future__ import annotations

import platform
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, Protocol

from core.utils.errors import VBotError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]

EDITABLE_PROMPT_FRAGMENT_NAMES = (
    "system.md",
    "runtime.md",
    "tools.md",
    "channels.md",
    "skills.md",
)
PROMPT_FRAGMENT_VARIABLES: dict[str, list[dict[str, str]]] = {
    "system.md": [
        {"placeholder": "{app_version}", "description": "Application version string"},
        {"placeholder": "{runtime}", "description": "Rendered runtime fragment"},
        {"placeholder": "{tools}", "description": "Rendered tools fragment"},
        {"placeholder": "{channels}", "description": "Rendered channels fragment"},
        {"placeholder": "{skills}", "description": "Rendered skills fragment"},
        {
            "placeholder": "{include:filename}",
            "description": "Include another fragment by filename",
        },
    ],
    "runtime.md": [
        {"placeholder": "{host}", "description": "Host machine name"},
        {"placeholder": "{os}", "description": "Operating system name"},
        {"placeholder": "{model}", "description": "Active model identifier"},
        {"placeholder": "{agent_workspace}", "description": "Agent workspace directory path"},
        {"placeholder": "{app_dir}", "description": "Application source directory path"},
        {"placeholder": "{data_root}", "description": "Data root directory path"},
        {"placeholder": "{thinking_effort}", "description": "Agent thinking effort setting"},
        {"placeholder": "{current_date}", "description": "Current date in ISO 8601 format"},
    ],
    "tools.md": [
        {"placeholder": "{tool_list}", "description": "List of available tools"},
    ],
    "channels.md": [
        {"placeholder": "{channel_list}", "description": "List of active agent-bound channels"},
    ],
    "skills.md": [
        {"placeholder": "{skill_list}", "description": "List of available skills"},
    ],
}
INCLUDE_PATTERN = re.compile(r"\{include:([^{}]+)\}")
_LOGGER = get_logger("prompts")


class PromptError(VBotError, ValueError):
    """Raised when a public prompt-domain request is invalid."""


class PromptAgent(Protocol):
    """Agent fields needed for prompt assembly."""

    @property
    def id(self) -> str:
        """Stable Agent identifier."""
        ...

    @property
    def model(self) -> str:
        """Resolved Agent model identifier."""
        ...

    @property
    def workspace(self) -> str:
        """Agent workspace path."""
        ...

    @property
    def thinking_effort(self) -> str | None:
        """Agent thinking effort setting."""
        ...

    @property
    def allowed_tools(self) -> list[str]:
        """Tool allowlist for prompt and provider schemas."""
        ...

    @property
    def allowed_skills(self) -> list[str]:
        """Skill allowlist for prompt-visible skills."""
        ...


class PromptFragmentReader(Protocol):
    """Minimal prompt storage interface used by the system prompt manager."""

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Return a prompt fragment by resource name."""
        ...


class PromptFragmentStorage(PromptFragmentReader, Protocol):
    """Prompt fragment storage operations used by the editable prompt surface."""

    prompts_dir: Path

    def write_prompt_fragment(self, fragment_name: str, content: str) -> Path:
        """Write a user-copy prompt fragment."""
        ...

    def reset_prompt_fragment(self, fragment_name: str) -> Path:
        """Reset a user-copy prompt fragment to the bundled default."""
        ...


class ToolPromptRegistry(Protocol):
    """Tool registry methods needed for prompt and provider definitions."""

    def prompt_definitions(
        self, allowed_tools: Sequence[str] | None = None, *, include_internal: bool = False
    ) -> list[dict[str, Any]]:
        """Return prompt-ready tool name and description mappings."""
        ...

    def provider_definitions(
        self, allowed_tools: Sequence[str] | None = None, *, include_internal: bool = False
    ) -> list[dict[str, Any]]:
        """Return provider-ready tool schemas."""
        ...


class SkillPromptMetadata(Protocol):
    """Skill metadata fields needed for prompt assembly."""

    @property
    def name(self) -> str:
        """Stable skill identifier."""
        ...

    @property
    def description(self) -> str:
        """Prompt-visible skill description."""
        ...


class SkillPromptRegistry(Protocol):
    """Skill registry method needed for prompt-visible skill filtering."""

    def filter_allowed(self, allowed_skills: list[str]) -> list[SkillPromptMetadata]:
        """Return prompt-visible skills filtered by an agent allowlist."""
        ...


class ChannelPromptMetadata(Protocol):
    """Channel fields needed for prompt-visible channel rendering."""

    @property
    def id(self) -> str:
        """Stable channel identifier."""
        ...

    @property
    def platform(self) -> str:
        """Channel platform identifier."""
        ...

    @property
    def agent_id(self) -> str:
        """Agent that owns this channel."""
        ...

    @property
    def allowed_chat_ids(self) -> list[int]:
        """Allowed platform chat identifiers."""
        ...

    @property
    def enabled(self) -> bool:
        """Whether this channel is enabled."""
        ...


class ChannelPromptRegistry(Protocol):
    """Channel registry methods needed for prompt-visible channel filtering."""

    def has_active_channels(self) -> bool:
        """Return whether any channel adapter is currently running."""
        ...

    def list_channels(self) -> Sequence[ChannelPromptMetadata]:
        """Return all configured channels."""
        ...

    def _is_running(self, channel_id: str) -> bool:
        """Return whether one configured channel adapter is currently running."""
        ...


@dataclass(frozen=True)
class PromptFragment:
    """Editable prompt fragment response model."""

    name: str
    content: str
    is_modified: bool
    variables: list[dict[str, str]]

    def to_dict(self) -> JsonObject:
        return {
            "name": self.name,
            "content": self.content,
            "is_modified": self.is_modified,
            "variables": list(self.variables),
        }


class PromptFragmentManager:
    """Manage editable prompt fragments through a storage backend."""

    def __init__(self, storage: PromptFragmentStorage) -> None:
        self._storage = storage

    def list_fragments(self) -> list[JsonObject]:
        """Return editable prompt fragments in stable UI order."""
        fragments: list[JsonObject] = []
        for name in EDITABLE_PROMPT_FRAGMENT_NAMES:
            fragment = PromptFragment(
                name=name,
                content=self._storage.read_prompt_fragment(name),
                is_modified=(self._storage.prompts_dir / name).exists(),
                variables=PROMPT_FRAGMENT_VARIABLES.get(name, []),
            )
            fragments.append(fragment.to_dict())
        return fragments

    def update_fragment(self, name: str, content: str) -> JsonObject:
        """Write an editable prompt fragment and return its public state."""
        fragment_name = _validate_editable_prompt_fragment_name(name)
        self._storage.write_prompt_fragment(fragment_name, content)
        return {"name": fragment_name, "content": content, "is_modified": True}

    def reset_fragment(self, name: str) -> JsonObject:
        """Reset an editable prompt fragment to its bundled default."""
        fragment_name = _validate_editable_prompt_fragment_name(name)
        self._storage.reset_prompt_fragment(fragment_name)
        content = self._storage.read_prompt_fragment(fragment_name)
        return {"name": fragment_name, "content": content}


class SystemPromptManager:
    """Assemble system prompts from prompt fragments and workspace includes."""

    def __init__(
        self,
        storage: PromptFragmentReader,
        tool_registry: ToolPromptRegistry,
        skill_registry: SkillPromptRegistry,
        channel_registry: ChannelPromptRegistry | None = None,
        *,
        app_version: str,
        app_dir: str | Path,
        data_root: str | Path,
        host: str | None = None,
        os_name: str | None = None,
        current_date: Callable[[], str] | None = None,
    ) -> None:
        self._storage = storage
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry
        self._channel_registry = channel_registry
        self._app_version = app_version
        self._app_dir = Path(app_dir)
        self._data_root = Path(data_root)
        self._host = host
        self._os_name = os_name
        self._current_date = current_date or _current_utc_date

    def update_skill_registry(self, skill_registry: SkillPromptRegistry) -> None:
        """Replace the skill registry used for prompt and provider tool decisions."""
        self._skill_registry = skill_registry

    def build_system_prompt(self, agent: PromptAgent) -> str:
        """Build the complete system prompt for an agent."""
        prompt = self._storage.read_prompt_fragment("system.md")
        replacements = {
            "{app_version}": self._app_version,
            "{runtime}": self._build_runtime_block(agent),
            "{tools}": self._build_tools_block(agent),
            "{channels}": self._build_channels_block(agent),
            "{skills}": self._build_skills_block(agent),
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)

        return self._replace_workspace_includes(prompt, Path(agent.workspace))

    def provider_tool_definitions(self, agent: PromptAgent) -> list[dict[str, Any]]:
        """Return provider tool definitions filtered by the agent allowlist."""
        definitions = self._tool_registry.provider_definitions(agent.allowed_tools)
        if not self._agent_has_loadable_skills(agent):
            return definitions

        return [
            *definitions,
            *self._tool_registry.provider_definitions(["skill"], include_internal=True),
        ]

    def _build_runtime_block(self, agent: PromptAgent) -> str:
        runtime = self._storage.read_prompt_fragment("runtime.md")
        thinking_effort = "default" if agent.thinking_effort is None else agent.thinking_effort
        replacements = {
            "{host}": self._host or socket.gethostname(),
            "{os}": self._os_name or platform.platform(),
            "{model}": agent.model,
            "{agent_workspace}": agent.workspace,
            "{app_dir}": str(self._app_dir.resolve()),
            "{data_root}": str(self._data_root.resolve()),
            "{thinking_effort}": thinking_effort,
            "{current_date}": self._current_date(),
        }
        return _replace_placeholders(runtime, replacements)

    def _build_tools_block(self, agent: PromptAgent) -> str:
        tools = self._storage.read_prompt_fragment("tools.md")
        tool_list = _format_tool_list(
            self._tool_registry.prompt_definitions(agent.allowed_tools),
        )
        return tools.replace("{tool_list}", tool_list)

    def _build_channels_block(self, agent: PromptAgent) -> str:
        channels = self._storage.read_prompt_fragment("channels.md")
        channel_list = _format_channel_list(self._agent_active_channels(agent))
        return channels.replace("{channel_list}", channel_list)

    def _build_skills_block(self, agent: PromptAgent) -> str:
        skills = self._storage.read_prompt_fragment("skills.md")
        skill_list = _format_skill_list(self._skill_registry.filter_allowed(agent.allowed_skills))
        return skills.replace("{skill_list}", skill_list)

    def _agent_has_loadable_skills(self, agent: PromptAgent) -> bool:
        return bool(self._skill_registry.filter_allowed(agent.allowed_skills))

    def _agent_active_channels(self, agent: PromptAgent) -> list[ChannelPromptMetadata]:
        channel_registry = self._channel_registry
        if channel_registry is None or not channel_registry.has_active_channels():
            return []

        active_channels: list[ChannelPromptMetadata] = []
        for channel in channel_registry.list_channels():
            if channel.agent_id != agent.id:
                continue
            if not channel.enabled:
                continue
            if not channel_registry._is_running(channel.id):
                continue
            active_channels.append(channel)
        return active_channels

    def _replace_workspace_includes(self, prompt: str, workspace_path: Path) -> str:
        def replace_include(match: re.Match[str]) -> str:
            filename = match.group(1).strip()
            _validate_workspace_include(filename)
            include_path = workspace_path / filename
            try:
                content = include_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                _LOGGER.warning("Skipping missing workspace include: %s", include_path)
                return ""
            except OSError as exc:
                raise PromptError(f"Cannot read workspace include {filename}: {exc}") from exc
            return f'<file name="{filename}">\n{content}\n</file>'

        return INCLUDE_PATTERN.sub(replace_include, prompt)


def _validate_workspace_include(filename: str) -> None:
    path = Path(filename)
    if path.name != filename or path.is_absolute():
        raise PromptError(f"Unsafe workspace include: {filename}")


def _validate_editable_prompt_fragment_name(name: str) -> str:
    if name not in EDITABLE_PROMPT_FRAGMENT_NAMES:
        raise PromptError(f"unknown prompt fragment: {name}")
    return name


def _current_utc_date() -> str:
    return datetime.now(UTC).date().isoformat()


def _replace_placeholders(template: str, replacements: dict[str, str]) -> str:
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


def _format_tool_list(tool_definitions: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {definition['name']}: {definition['description']}" for definition in tool_definitions
    )


def _format_channel_list(channels: list[ChannelPromptMetadata]) -> str:
    if not channels:
        return "- None"

    lines: list[str] = []
    for channel in channels:
        target_hint = (
            "default target available"
            if len(channel.allowed_chat_ids) == 1
            else "explicit target required"
        )
        lines.append(f"- {channel.id}: {channel.platform} ({target_hint})")
    return "\n".join(lines)


def _format_skill_list(skills: list[SkillPromptMetadata]) -> str:
    lines = ["<available_skills>"]
    for skill in skills:
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(skill.description)}</description>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)
