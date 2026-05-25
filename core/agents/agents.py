"""Agent persistence and workspace lifecycle management."""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import socket
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, Protocol

from core.channels.channels import ChannelConfig
from core.sessions import ChatSessionManager

DEFAULT_FALLBACK_MODEL = ""
DEFAULT_MODEL = ""
DEFAULT_TEMPERATURE: float | None = None
DEFAULT_THINKING_EFFORT: str | None = None
DEFAULT_ALLOWED_ITEMS = ("*",)
WORKSPACE_TEMPLATE_FILES = ("SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md")
ALLOWED_THINKING_EFFORTS = {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
INCLUDE_PATTERN = re.compile(r"\{include:([^{}]+)\}")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_TEMPLATE_DIR = _PROJECT_ROOT / "resources" / "workspace-templates"


class AgentError(ValueError):
    """Base error for expected agent lifecycle failures."""


class AgentAlreadyExistsError(AgentError):
    """Raised when creating an agent whose ID already exists."""


class AgentNotFoundError(AgentError):
    """Raised when an agent cannot be found."""


class InvalidAgentIdError(AgentError):
    """Raised when an agent ID is unsafe for filesystem use."""


class PromptFragmentReader(Protocol):
    """Minimal prompt storage interface used by the system prompt manager."""

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Return a prompt fragment by resource name."""


class ToolPromptRegistry(Protocol):
    """Tool registry methods needed for prompt and provider definitions."""

    def prompt_definitions(
        self, allowed_tools: Sequence[str] | None = None, *, include_internal: bool = False
    ) -> list[dict[str, Any]]:
        """Return prompt-ready tool name and description mappings."""

    def provider_definitions(
        self, allowed_tools: Sequence[str] | None = None, *, include_internal: bool = False
    ) -> list[dict[str, Any]]:
        """Return provider-ready tool schemas."""


class SkillPromptMetadata(Protocol):
    """Skill metadata fields needed for prompt assembly."""

    @property
    def name(self) -> str:
        """Stable skill identifier."""

    @property
    def description(self) -> str:
        """Prompt-visible skill description."""


class SkillPromptRegistry(Protocol):
    """Skill registry method needed for prompt-visible skill filtering."""

    def filter_allowed(self, allowed_skills: list[str]) -> list[SkillPromptMetadata]:
        """Return prompt-visible skills filtered by an agent allowlist."""


class ChannelPromptRegistry(Protocol):
    """Channel registry methods needed for prompt-visible channel filtering."""

    def has_active_channels(self) -> bool:
        """Return whether any channel adapter is currently running."""

    def list_channels(self) -> list[ChannelConfig]:
        """Return all configured channels."""

    def _is_running(self, channel_id: str) -> bool:
        """Return whether one configured channel adapter is currently running."""


@dataclass(frozen=True)
class Agent:
    """Persisted agent configuration stored in ``agent.json``."""

    id: str
    name: str
    model: str
    fallback_model: str
    workspace: str
    temperature: float | None
    thinking_effort: str | None
    allowed_tools: list[str]
    allowed_skills: list[str]
    created_at: str
    updated_at: str
    current_session_id: str = ""


class AgentStore:
    """CRUD store for persisted agent configs and workspaces."""

    def __init__(
        self,
        data_dir: str | Path,
        template_dir: str | Path | None = None,
        defaults_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._template_dir = (
            Path(template_dir) if template_dir is not None else _DEFAULT_TEMPLATE_DIR
        )
        self._defaults_provider = defaults_provider

    @property
    def data_dir(self) -> Path:
        """Root directory containing agents, workspaces, and archives."""
        return self._data_dir

    def create(
        self,
        agent_id: str,
        name: str,
        *,
        model: str = DEFAULT_MODEL,
        fallback_model: str = DEFAULT_FALLBACK_MODEL,
        workspace: str | Path | None = None,
        temperature: float | None = DEFAULT_TEMPERATURE,
        thinking_effort: str | None = DEFAULT_THINKING_EFFORT,
        allowed_tools: list[str] | None = None,
        allowed_skills: list[str] | None = None,
    ) -> Agent:
        """Create and persist a new agent, sessions directory, and workspace."""
        self._validate_agent_id(agent_id)
        agent_dir = self._agent_dir(agent_id)
        if agent_dir.exists():
            raise AgentAlreadyExistsError(f"Agent already exists: {agent_id}")

        validated_name = _validate_string_field("name", name, allow_empty=False)
        validated_model = _validate_string_field("model", model, allow_empty=True)
        validated_fallback_model = _validate_string_field(
            "fallback_model", fallback_model, allow_empty=True
        )
        validated_temperature = _validate_temperature(temperature)
        validated_thinking_effort = _validate_thinking_effort(thinking_effort)
        validated_allowed_tools = _validate_allowed_items("allowed_tools", allowed_tools)
        validated_allowed_skills = _validate_allowed_items("allowed_skills", allowed_skills)
        now = _utc_now()
        workspace_path = (
            _validate_workspace(workspace)
            if workspace is not None
            else self._default_workspace(agent_id)
        )

        agent_dir.mkdir(parents=True)
        session = self._session_manager().create(agent_id)
        agent = Agent(
            id=agent_id,
            name=validated_name,
            model=validated_model,
            fallback_model=validated_fallback_model,
            workspace=str(workspace_path.resolve()),
            temperature=validated_temperature,
            thinking_effort=validated_thinking_effort,
            allowed_tools=validated_allowed_tools,
            allowed_skills=validated_allowed_skills,
            current_session_id=session.id,
            created_at=now,
            updated_at=now,
        )

        self._seed_workspace(Path(agent.workspace))
        self._write_agent(agent)
        return self._apply_defaults(agent, self._agent_defaults())

    def get(self, agent_id: str) -> Agent:
        """Load an agent from disk."""
        self._validate_agent_id(agent_id)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        raw_agent = self._load_raw_agent(agent_path)
        return self._apply_defaults(raw_agent, self._agent_defaults())

    def list(self) -> list[Agent]:
        """Return all persisted agents sorted by ID."""
        agents_dir = self._data_dir / "agents"
        if not agents_dir.exists():
            return []

        defaults = self._agent_defaults()
        agents: list[Agent] = []
        for agent_path in sorted(agents_dir.glob("*/agent.json")):
            raw_agent = self._load_raw_agent(agent_path)
            agents.append(self._apply_defaults(raw_agent, defaults))
        return agents

    def update(self, agent_id: str, **changes: Any) -> Agent:
        """Update mutable fields for an existing agent."""
        self._validate_agent_id(agent_id)
        if "id" in changes and changes["id"] != agent_id:
            raise AgentError("Agent id is immutable")

        changes.pop("id", None)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        agent = self._load_raw_agent(agent_path)
        if not changes:
            return self._apply_defaults(agent, self._agent_defaults())

        allowed_fields = set(Agent.__dataclass_fields__) - {
            "id",
            "created_at",
            "updated_at",
            "workspace",
        }
        unknown_fields = sorted(set(changes) - allowed_fields)
        if unknown_fields:
            raise AgentError(f"Unknown agent fields: {', '.join(unknown_fields)}")

        string_fields = {
            "name",
            "model",
            "fallback_model",
            "current_session_id",
        }
        for field in sorted(string_fields & set(changes)):
            changes[field] = _validate_string_field(
                field,
                changes[field],
                allow_empty=field in {"model", "fallback_model"},
            )
        if "temperature" in changes:
            changes["temperature"] = _validate_temperature(changes["temperature"])
        if "thinking_effort" in changes:
            changes["thinking_effort"] = _validate_thinking_effort(changes["thinking_effort"])
        if "allowed_tools" in changes:
            changes["allowed_tools"] = _validate_allowed_items(
                "allowed_tools", changes["allowed_tools"]
            )
        if "allowed_skills" in changes:
            changes["allowed_skills"] = _validate_allowed_items(
                "allowed_skills", changes["allowed_skills"]
            )
        if "current_session_id" in changes:
            self._validate_current_session(agent_id, changes["current_session_id"])

        updated_agent = replace(agent, **changes, updated_at=_utc_now())
        self._write_agent(updated_agent)
        return self._apply_defaults(updated_agent, self._agent_defaults())

    def delete(self, agent_id: str) -> Path:
        """Archive an agent directory and workspace, then remove active copies."""
        agent = self.get(agent_id)
        archive_dir = self._archive_dir(agent_id)
        if archive_dir.exists():
            shutil.rmtree(archive_dir)

        archive_dir.mkdir(parents=True)
        shutil.move(str(self._agent_dir(agent_id)), str(archive_dir / "agent"))

        workspace_path = Path(agent.workspace)
        if workspace_path.exists():
            shutil.move(str(workspace_path), str(archive_dir / "workspace"))

        return archive_dir

    def _agent_dir(self, agent_id: str) -> Path:
        return self._data_dir / "agents" / agent_id

    def _agent_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "agent.json"

    def _default_workspace(self, agent_id: str) -> Path:
        return self._data_dir / f"workspace-{agent_id}"

    def _archive_dir(self, agent_id: str) -> Path:
        return self._data_dir / "archive" / agent_id

    def _write_agent(self, agent: Agent) -> None:
        agent_path = self._agent_path(agent.id)
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = agent_path.with_name(f".{agent_path.name}.tmp")
        temp_path.write_text(
            json.dumps(asdict(agent), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, agent_path)

    def _agent_defaults(self) -> dict[str, Any]:
        if self._defaults_provider is None:
            return {}

        defaults = self._defaults_provider()
        if not isinstance(defaults, dict):
            raise AgentError("defaults provider must return a dictionary")
        return defaults

    def _load_raw_agent(self, agent_path: Path) -> Agent:
        data = json.loads(agent_path.read_text(encoding="utf-8"))
        agent = _agent_from_dict(data)
        return self._ensure_current_session(agent)

    def _apply_defaults(self, agent: Agent, defaults: dict[str, Any]) -> Agent:
        changes: dict[str, Any] = {}

        if agent.model == "" and "model" in defaults:
            changes["model"] = _validate_string_field("model", defaults["model"], allow_empty=True)
        if agent.fallback_model == "" and "fallback_model" in defaults:
            changes["fallback_model"] = _validate_string_field(
                "fallback_model",
                defaults["fallback_model"],
                allow_empty=True,
            )
        if agent.temperature is None and "temperature" in defaults:
            changes["temperature"] = _validate_temperature(defaults["temperature"])
        if agent.thinking_effort is None and "thinking_effort" in defaults:
            changes["thinking_effort"] = _validate_thinking_effort(defaults["thinking_effort"])

        if not changes:
            return agent
        return replace(agent, **changes)

    def _ensure_current_session(self, agent: Agent) -> Agent:
        if agent.current_session_id and self._session_exists(agent.id, agent.current_session_id):
            return agent

        session = self._session_manager().create(agent.id)
        updated_agent = replace(agent, current_session_id=session.id, updated_at=_utc_now())
        self._write_agent(updated_agent)
        return updated_agent

    def _validate_current_session(self, agent_id: str, session_id: Any) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise AgentError("current_session_id must be a non-empty string")
        if not self._session_exists(agent_id, session_id):
            raise AgentError(f"current session does not exist: {session_id}")

    def _session_exists(self, agent_id: str, session_id: str) -> bool:
        return self._session_manager().exists(agent_id, session_id)

    def _session_manager(self) -> ChatSessionManager:
        return ChatSessionManager(self._data_dir)

    def _seed_workspace(self, workspace_path: Path) -> None:
        workspace_path.mkdir(parents=True, exist_ok=True)
        for filename in WORKSPACE_TEMPLATE_FILES:
            target = workspace_path / filename
            if target.exists():
                continue
            template = self._template_dir / filename
            target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not AGENT_ID_PATTERN.fullmatch(agent_id):
            raise InvalidAgentIdError(
                "Agent id must be 1-64 characters using only letters, numbers, "
                "hyphen, or underscore"
            )


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

    def build_system_prompt(self, agent: Agent) -> str:
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

    def provider_tool_definitions(self, agent: Agent) -> list[dict[str, Any]]:
        """Return provider tool definitions filtered by the agent allowlist."""
        definitions = self._tool_registry.provider_definitions(agent.allowed_tools)
        if not self._agent_has_loadable_skills(agent):
            return definitions

        return [
            *definitions,
            *self._tool_registry.provider_definitions(["skill"], include_internal=True),
        ]

    def _build_runtime_block(self, agent: Agent) -> str:
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

    def _build_tools_block(self, agent: Agent) -> str:
        tools = self._storage.read_prompt_fragment("tools.md")
        tool_list = _format_tool_list(
            self._tool_registry.prompt_definitions(agent.allowed_tools),
        )
        return tools.replace("{tool_list}", tool_list)

    def _build_channels_block(self, agent: Agent) -> str:
        channels = self._storage.read_prompt_fragment("channels.md")
        channel_list = _format_channel_list(self._agent_active_channels(agent))
        return channels.replace("{channel_list}", channel_list)

    def _build_skills_block(self, agent: Agent) -> str:
        skills = self._storage.read_prompt_fragment("skills.md")
        skill_list = _format_skill_list(self._skill_registry.filter_allowed(agent.allowed_skills))
        return skills.replace("{skill_list}", skill_list)

    def _agent_has_loadable_skills(self, agent: Agent) -> bool:
        return bool(self._skill_registry.filter_allowed(agent.allowed_skills))

    def _agent_active_channels(self, agent: Agent) -> list[ChannelConfig]:
        channel_registry = self._channel_registry
        if channel_registry is None or not channel_registry.has_active_channels():
            return []

        active_channels: list[ChannelConfig] = []
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
            except OSError as exc:
                raise AgentError(f"Cannot read workspace include {filename}: {exc}") from exc
            return f'<file name="{filename}">\n{content}\n</file>'

        return INCLUDE_PATTERN.sub(replace_include, prompt)


def _validate_string_field(field: str, value: Any, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise AgentError(f"{field} must be a string")
    if not allow_empty and not value:
        raise AgentError(f"{field} must be a non-empty string")
    return value


def _validate_temperature(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AgentError("temperature must be a number")
    temperature = float(value)
    if not math.isfinite(temperature):
        raise AgentError("temperature must be finite")
    if temperature < MIN_TEMPERATURE or temperature > MAX_TEMPERATURE:
        raise AgentError(f"temperature must be between {MIN_TEMPERATURE:g} and {MAX_TEMPERATURE:g}")
    return temperature


def _validate_thinking_effort(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentError("thinking_effort must be a string")
    if value not in ALLOWED_THINKING_EFFORTS:
        allowed = ", ".join(repr(item) for item in sorted(ALLOWED_THINKING_EFFORTS))
        raise AgentError(f"thinking_effort must be one of: {allowed}")
    return value


def _validate_allowed_items(field: str, items: list[str] | None) -> list[str]:
    if items is None:
        return list(DEFAULT_ALLOWED_ITEMS)
    if not isinstance(items, list):
        raise AgentError(f"{field} must be a list of strings")
    if not all(isinstance(item, str) for item in items):
        raise AgentError(f"{field} must be a list of strings")
    return list(items)


def _validate_workspace(workspace: str | Path) -> Path:
    if not isinstance(workspace, str | os.PathLike):
        raise AgentError("workspace must be a path string")
    return Path(workspace)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def _format_channel_list(channels: list[ChannelConfig]) -> str:
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


def _validate_workspace_include(filename: str) -> None:
    path = Path(filename)
    if path.name != filename or path.is_absolute():
        raise AgentError(f"Unsafe workspace include: {filename}")


def _agent_from_dict(data: dict[str, Any]) -> Agent:
    return Agent(
        id=_validate_string_field("id", data["id"], allow_empty=False),
        name=_validate_string_field("name", data["name"], allow_empty=False),
        model=_validate_string_field("model", data["model"], allow_empty=True),
        fallback_model=_validate_string_field(
            "fallback_model", data["fallback_model"], allow_empty=True
        ),
        workspace=str(_validate_workspace(data["workspace"])),
        temperature=_validate_temperature(data.get("temperature")),
        thinking_effort=_validate_thinking_effort(data.get("thinking_effort")),
        allowed_tools=_validate_allowed_items("allowed_tools", data["allowed_tools"]),
        allowed_skills=_validate_allowed_items("allowed_skills", data["allowed_skills"]),
        current_session_id=_validate_string_field(
            "current_session_id", data.get("current_session_id", ""), allow_empty=True
        ),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )
