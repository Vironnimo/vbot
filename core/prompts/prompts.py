"""System Prompt assembly and editable prompt fragment rules."""

from __future__ import annotations

import platform
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol

from core.memory import DEFAULT_MEMORY_PROMPT_MODE, MemoryPromptMode, MemoryService
from core.tools.availability import MEMORY_TOOL_NAME, memory_tool_enabled
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
        {"placeholder": "{memory}", "description": "Rendered memory fragment"},
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
        {"placeholder": "{app_version}", "description": "Application version string"},
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


@dataclass(frozen=True)
class PromptScope:
    """Resolved prompt-fragment scope."""

    type: str
    agent_id: str | None = None


class PromptAgent(Protocol):
    """Agent fields needed for prompt assembly."""

    @property
    def id(self) -> str:
        """Stable Agent identifier."""
        ...

    @property
    def name(self) -> str:
        """Display name for the Agent."""
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
    def memory_prompt_mode(self) -> MemoryPromptMode:
        """Which pinned memory files are included in the system prompt."""
        ...

    @property
    def allowed_tools(self) -> list[str]:
        """Tool allowlist for prompt and provider schemas."""
        ...

    @property
    def allowed_skills(self) -> list[str]:
        """Skill allowlist for prompt-visible skills."""
        ...

    @property
    def custom_system_prompt_enabled(self) -> bool:
        """Whether this Agent uses its private system prompt fragment scope."""
        ...


class PromptFragmentReader(Protocol):
    """Minimal prompt storage interface used by the system prompt manager."""

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Return a prompt fragment by resource name."""
        ...

    def read_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> str:
        """Return an Agent prompt fragment or an empty string when absent."""
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

    def agent_prompts_dir(self, agent_id: str) -> Path:
        """Return the prompt-fragment directory for one Agent."""
        ...

    def agent_prompt_fragment_exists(self, agent_id: str, fragment_name: str) -> bool:
        """Return whether one Agent prompt fragment exists."""
        ...

    def write_agent_prompt_fragment(self, agent_id: str, fragment_name: str, content: str) -> Path:
        """Write one Agent prompt fragment."""
        ...

    def reset_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> Path:
        """Reset one Agent prompt fragment to current default-scope content."""
        ...


class PromptAgentStore(Protocol):
    """Agent catalog methods needed for prompt scope validation."""

    def get(self, agent_id: str) -> PromptAgent:
        """Return one Agent by id."""
        ...

    def list(self) -> list[PromptAgent]:
        """Return all Agents."""
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
    def allowed_chat_ids(self) -> list[str]:
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


class MemoryPromptProvider(Protocol):
    """Pinned memory renderer used by prompt assembly."""

    def build_prompt_block(self, workspace: Path, mode: MemoryPromptMode) -> str:
        """Return the prompt-visible memory block for one workspace."""
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

    def __init__(
        self,
        storage: PromptFragmentStorage,
        agent_store: PromptAgentStore | None = None,
    ) -> None:
        self._storage = storage
        self._agent_store = agent_store

    def list_scopes(self) -> list[JsonObject]:
        """Return prompt scopes available to the System Prompt editor."""
        scopes: list[JsonObject] = [{"type": "default", "label": "Default"}]
        if self._agent_store is None:
            return scopes

        for agent in sorted(self._agent_store.list(), key=lambda item: item.id):
            if not agent.custom_system_prompt_enabled:
                continue
            scopes.append(
                {
                    "type": "agent",
                    "agent_id": agent.id,
                    "label": agent.name or agent.id,
                }
            )
        return scopes

    def validate_scope(self, scope: Any = None) -> PromptScope:
        """Resolve and validate a public prompt scope payload."""
        return self._resolve_scope(scope)

    def list_fragments(self, scope: Any = None) -> list[JsonObject]:
        """Return editable prompt fragments in stable UI order."""
        prompt_scope = self._resolve_scope(scope)
        fragments: list[JsonObject] = []
        for name in EDITABLE_PROMPT_FRAGMENT_NAMES:
            if prompt_scope.type == "agent":
                agent_id = _require_scope_agent_id(prompt_scope)
                fragment = PromptFragment(
                    name=name,
                    content=self._storage.read_agent_prompt_fragment(agent_id, name),
                    is_modified=self._storage.agent_prompt_fragment_exists(agent_id, name),
                    variables=PROMPT_FRAGMENT_VARIABLES.get(name, []),
                )
            else:
                fragment = PromptFragment(
                    name=name,
                    content=self._storage.read_prompt_fragment(name),
                    is_modified=(self._storage.prompts_dir / name).exists(),
                    variables=PROMPT_FRAGMENT_VARIABLES.get(name, []),
                )
            fragments.append(fragment.to_dict())
        return fragments

    def update_fragment(self, name: str, content: str, scope: Any = None) -> JsonObject:
        """Write an editable prompt fragment and return its public state."""
        fragment_name = _validate_editable_prompt_fragment_name(name)
        prompt_scope = self._resolve_scope(scope)
        if prompt_scope.type == "agent":
            agent_id = _require_scope_agent_id(prompt_scope)
            self._storage.write_agent_prompt_fragment(agent_id, fragment_name, content)
            return {"name": fragment_name, "content": content, "is_modified": True}

        self._storage.write_prompt_fragment(fragment_name, content)
        return {"name": fragment_name, "content": content, "is_modified": True}

    def reset_fragment(self, name: str, scope: Any = None) -> JsonObject:
        """Reset an editable prompt fragment to its default content."""
        fragment_name = _validate_editable_prompt_fragment_name(name)
        prompt_scope = self._resolve_scope(scope)
        if prompt_scope.type == "agent":
            agent_id = _require_scope_agent_id(prompt_scope)
            self._storage.reset_agent_prompt_fragment(agent_id, fragment_name)
            content = self._storage.read_agent_prompt_fragment(agent_id, fragment_name)
            return {"name": fragment_name, "content": content, "is_modified": True}

        self._storage.reset_prompt_fragment(fragment_name)
        content = self._storage.read_prompt_fragment(fragment_name)
        return {"name": fragment_name, "content": content, "is_modified": False}

    def _resolve_scope(self, scope: Any = None) -> PromptScope:
        prompt_scope = _normalize_prompt_scope(scope)
        if prompt_scope.type == "default":
            return prompt_scope
        if self._agent_store is None:
            raise PromptError("Agent prompt scopes are not available")

        agent_id = _require_scope_agent_id(prompt_scope)
        try:
            agent = self._agent_store.get(agent_id)
        except Exception as exc:
            raise PromptError(f"unknown prompt scope agent: {agent_id}") from exc
        if not agent.custom_system_prompt_enabled:
            raise PromptError(f"Agent prompt scope is not enabled: {agent_id}")
        return prompt_scope


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
        memory_provider: MemoryPromptProvider | None = None,
        host: str | None = None,
        os_name: str | None = None,
        current_date: Callable[[], str] | None = None,
    ) -> None:
        self._storage = storage
        self._tool_registry = tool_registry
        self._skill_registry = skill_registry
        self._channel_registry = channel_registry
        self._memory_provider = memory_provider or MemoryService()
        self._app_version = app_version
        self._app_dir = Path(app_dir)
        self._data_root = Path(data_root)
        self._host = host
        self._os_name = os_name
        self._current_date = current_date or _current_utc_date

    @property
    def app_dir(self) -> Path:
        """Application source directory this prompt manager was built with."""
        return self._app_dir

    def update_skill_registry(self, skill_registry: SkillPromptRegistry) -> None:
        """Replace the skill registry used for prompt and provider tool decisions."""
        self._skill_registry = skill_registry

    def build_system_prompt(self, agent: PromptAgent, scope: Any = None) -> str:
        """Build the complete system prompt for an agent."""
        prompt_scope = self._resolve_build_scope(agent, scope)
        prompt = self._read_prompt_fragment(prompt_scope, agent.id, "system.md")

        if "{app_version}" in prompt:
            prompt = prompt.replace("{app_version}", self._app_version)
        if "{memory}" in prompt:
            prompt = prompt.replace("{memory}", self._build_memory_block(agent))
        if "{runtime}" in prompt:
            prompt = prompt.replace("{runtime}", self._build_runtime_block(agent, prompt_scope))
        if "{tools}" in prompt:
            prompt = prompt.replace("{tools}", self._build_tools_block(agent, prompt_scope))
        if "{channels}" in prompt:
            prompt = prompt.replace("{channels}", self._build_channels_block(agent, prompt_scope))
        if "{skills}" in prompt:
            prompt = prompt.replace("{skills}", self._build_skills_block(agent, prompt_scope))

        return self._replace_workspace_includes(prompt, Path(agent.workspace))

    def provider_tool_definitions(self, agent: PromptAgent) -> list[dict[str, Any]]:
        """Return provider tool definitions filtered by the agent allowlist."""
        definitions = self._provider_definitions_for_agent(agent)
        if not self._agent_has_loadable_skills(agent):
            return definitions

        return [
            *definitions,
            *self._tool_registry.provider_definitions(["skill"], include_internal=True),
        ]

    def _build_memory_block(self, agent: PromptAgent) -> str:
        mode = getattr(agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
        return self._memory_provider.build_prompt_block(Path(agent.workspace), mode)

    def _build_runtime_block(self, agent: PromptAgent, scope: PromptScope) -> str:
        runtime = self._read_prompt_fragment(scope, agent.id, "runtime.md")
        thinking_effort = "default" if agent.thinking_effort is None else agent.thinking_effort
        replacements = {
            "{host}": self._host or socket.gethostname(),
            "{app_version}": self._app_version,
            "{os}": self._os_name or platform.platform(),
            "{model}": agent.model,
            "{agent_workspace}": agent.workspace,
            "{app_dir}": str(self._app_dir.resolve()),
            "{data_root}": str(self._data_root.resolve()),
            "{thinking_effort}": thinking_effort,
            "{current_date}": self._current_date(),
        }
        return _replace_placeholders(runtime, replacements)

    def _build_tools_block(self, agent: PromptAgent, scope: PromptScope) -> str:
        tools = self._read_prompt_fragment(scope, agent.id, "tools.md")
        tool_list = _format_tool_list(self._prompt_definitions_for_agent(agent))
        return tools.replace("{tool_list}", tool_list)

    def _provider_definitions_for_agent(self, agent: PromptAgent) -> list[JsonObject]:
        definitions = self._tool_registry.provider_definitions(agent.allowed_tools)
        return self._apply_memory_tool_visibility(
            definitions,
            agent,
            self._tool_registry.provider_definitions,
        )

    def _prompt_definitions_for_agent(self, agent: PromptAgent) -> list[JsonObject]:
        definitions = self._tool_registry.prompt_definitions(agent.allowed_tools)
        return self._apply_memory_tool_visibility(
            definitions,
            agent,
            self._tool_registry.prompt_definitions,
        )

    def _apply_memory_tool_visibility(
        self,
        definitions: list[JsonObject],
        agent: PromptAgent,
        definition_loader: Callable[[Sequence[str]], list[JsonObject]],
    ) -> list[JsonObject]:
        mode = getattr(agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
        if not memory_tool_enabled(mode):
            return [
                definition
                for definition in definitions
                if definition.get("name") != MEMORY_TOOL_NAME
            ]

        if any(definition.get("name") == MEMORY_TOOL_NAME for definition in definitions):
            return definitions

        return [*definitions, *definition_loader([MEMORY_TOOL_NAME])]

    def _build_channels_block(self, agent: PromptAgent, scope: PromptScope) -> str:
        channels = self._read_prompt_fragment(scope, agent.id, "channels.md")
        channel_list = _format_channel_list(self._agent_active_channels(agent))
        return channels.replace("{channel_list}", channel_list)

    def _build_skills_block(self, agent: PromptAgent, scope: PromptScope) -> str:
        skills = self._read_prompt_fragment(scope, agent.id, "skills.md")
        skill_list = _format_skill_list(self._skill_registry.filter_allowed(agent.allowed_skills))
        return skills.replace("{skill_list}", skill_list)

    def _resolve_build_scope(self, agent: PromptAgent, scope: Any = None) -> PromptScope:
        if scope is None:
            if agent.custom_system_prompt_enabled:
                return PromptScope("agent", agent.id)
            return PromptScope("default")

        prompt_scope = _normalize_prompt_scope(scope)
        if prompt_scope.type == "default":
            return prompt_scope

        agent_id = _require_scope_agent_id(prompt_scope)
        if agent_id != agent.id:
            raise PromptError("Agent prompt scope must match the preview Agent")
        if not agent.custom_system_prompt_enabled:
            raise PromptError(f"Agent prompt scope is not enabled: {agent_id}")
        return prompt_scope

    def _read_prompt_fragment(
        self,
        scope: PromptScope,
        agent_id: str,
        fragment_name: str,
    ) -> str:
        if scope.type == "agent":
            return self._storage.read_agent_prompt_fragment(agent_id, fragment_name)
        return self._storage.read_prompt_fragment(fragment_name)

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
    # Check both POSIX and Windows path semantics so separators and drive
    # prefixes of either platform are rejected on any host.
    posix_path = PurePosixPath(filename)
    windows_path = PureWindowsPath(filename)
    if (
        posix_path.name != filename
        or posix_path.is_absolute()
        or windows_path.name != filename
        or windows_path.is_absolute()
    ):
        raise PromptError(f"Unsafe workspace include: {filename}")


def _normalize_prompt_scope(scope: Any = None) -> PromptScope:
    if scope is None:
        return PromptScope("default")
    if isinstance(scope, PromptScope):
        return scope
    if not isinstance(scope, Mapping):
        raise PromptError("prompt scope must be an object")

    unsupported_fields = sorted(set(scope) - {"type", "agent_id"})
    if unsupported_fields:
        raise PromptError(f"unsupported prompt scope fields: {', '.join(unsupported_fields)}")

    scope_type = scope.get("type", "default")
    if scope_type == "default":
        if scope.get("agent_id") not in (None, ""):
            raise PromptError("default prompt scope must not include agent_id")
        return PromptScope("default")
    if scope_type == "agent":
        agent_id = scope.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise PromptError("agent prompt scope requires agent_id")
        return PromptScope("agent", agent_id)

    raise PromptError(f"unknown prompt scope type: {scope_type}")


def _require_scope_agent_id(scope: PromptScope) -> str:
    if scope.agent_id is None:
        raise PromptError("agent prompt scope requires agent_id")
    return scope.agent_id


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
