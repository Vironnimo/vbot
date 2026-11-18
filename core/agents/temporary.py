"""Owner-bound temporary runtime Agents backed by canonical Session bindings."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from core.agents._types import Agent
from core.chat.errors import ChatSessionError
from core.memory import MEMORY_PROMPT_MODE_OFF, MemoryPromptMode
from core.permissions import (
    PermissionCeiling,
    PermissionRules,
    normalize_permission_rules,
    permission_rules_to_dict,
)
from core.sessions import (
    ChatSessionManager,
    SessionAddress,
    TemporarySessionBinding,
)
from core.settings.normalizers import normalize_compaction_policy
from core.tools.availability import ToolAccess, normalize_tool_access
from core.utils.errors import StorageError
from core.utils.ids import new_id

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class TemporaryAgentConfig:
    """The protected runtime configuration for one owner-managed participant."""

    model: str
    cwd: Path
    tool_access: ToolAccess
    allowed_skills: list[str]
    tools: dict[str, Any]
    name: str
    temperature: float | None = None
    top_p: float | None = None
    thinking_effort: str | None = None
    fallback_models: list[str] | None = None
    instructions: str = ""
    prompt_blocks: list[str] | None = None
    project_memory_enabled: bool = True
    # ``None`` inherits the global Compaction Policy; a dict is the participant's
    # complete Agent-level Policy for automatic and manual Compaction.
    compaction_policy: dict[str, Any] | None = None
    repository_profile: str | None = None
    # An Identity Agent id: the participant takes that Agent's Model, sampling,
    # Tool access, Skills, Permission Rules and SOUL.md, bounded by the owner's
    # selection. Exclusive with ``repository_profile``.
    identity_profile: str | None = None
    # Run the actual stored identity in a new owner-managed Session.
    identity_agent: str | None = None
    identity_incarnation: str | None = None
    # The owner's Permission Rules for this participant (``None``: full access);
    # with a ``repository_profile`` each part it leaves unset resolves like that
    # Team member's. A mapping is normalized to :class:`PermissionRules`.
    permission_rules: PermissionRules | Mapping[str, Any] | None = None
    # A boundary the owner imposes on top, in its stored ceiling form
    # (``PermissionBoundary.to_ceiling_dict(source="extension")``).
    permission_ceiling: Mapping[str, Any] | None = None
    # Explicit owner choices win after Profile resolution (repository or identity). The ordinary
    # model/effort fields retain their existing fallback semantics.
    model_override: str | None = None
    thinking_effort_override: str | None = None

    def __post_init__(self) -> None:
        """Validate and snapshot caller-owned mutable configuration at the boundary."""

        if not isinstance(self.model, str) or (not self.model and not self.has_profile):
            raise ValueError("temporary model must be a non-empty string")
        cwd = Path(self.cwd)
        if not cwd.is_absolute():
            raise ValueError("temporary cwd must be absolute")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("temporary name must be a non-empty string")
        if not isinstance(self.instructions, str):
            raise ValueError("temporary instructions must be a string")
        if not isinstance(self.project_memory_enabled, bool):
            raise ValueError("temporary project_memory_enabled must be a boolean")
        if self.prompt_blocks is not None:
            if (
                not isinstance(self.prompt_blocks, list)
                or any(not isinstance(item, str) or ":" not in item for item in self.prompt_blocks)
                or len(set(self.prompt_blocks)) != len(self.prompt_blocks)
            ):
                raise ValueError("temporary prompt_blocks must contain unique block ids")
            object.__setattr__(self, "prompt_blocks", list(self.prompt_blocks))
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("temporary temperature must be between 0 and 2")
        if self.top_p is not None and (
            isinstance(self.top_p, bool)
            or not isinstance(self.top_p, (int, float))
            or not 0 <= self.top_p <= 1
        ):
            raise ValueError("temporary top_p must be between 0 and 1")
        if self.thinking_effort is not None and not isinstance(self.thinking_effort, str):
            raise ValueError("temporary thinking_effort must be a string or null")
        if not all(isinstance(item, str) and item for item in self.allowed_skills):
            raise ValueError("temporary allowed_skills must contain non-empty strings")
        if self.fallback_models is not None and not all(
            isinstance(item, str) and item for item in self.fallback_models
        ):
            raise ValueError("temporary fallback_models must contain non-empty strings")
        if self.repository_profile is not None and (
            not isinstance(self.repository_profile, str) or not self.repository_profile
        ):
            raise ValueError("repository_profile must name a Team Agent")
        if self.identity_profile is not None and (
            not isinstance(self.identity_profile, str) or not self.identity_profile
        ):
            raise ValueError("identity_profile must name an Identity Agent")
        if self.identity_agent is not None:
            if not isinstance(self.identity_agent, str) or not self.identity_agent:
                raise ValueError("identity_agent must name an Identity Agent")
            if not isinstance(self.identity_incarnation, str) or not self.identity_incarnation:
                raise ValueError("identity_agent requires identity_incarnation")
            if self.repository_profile is not None or self.identity_profile is not None:
                raise ValueError("identity_agent is exclusive with Profiles")
            if self.permission_rules is not None:
                raise ValueError(
                    "identity_agent uses its own Permission Rules; use permission_ceiling"
                )
        elif self.identity_incarnation is not None:
            raise ValueError("identity_incarnation requires identity_agent")
        if self.repository_profile is not None and self.identity_profile is not None:
            raise ValueError("choose either repository_profile or identity_profile")
        if self.model_override is not None and (
            not isinstance(self.model_override, str) or not self.model_override
        ):
            raise ValueError("temporary model_override must be a non-empty string")
        if not self.has_profile and (
            self.model_override is not None or self.thinking_effort_override is not None
        ):
            raise ValueError("temporary Profile overrides require a Profile")
        if self.thinking_effort_override is not None:
            from core.settings import validate_thinking_effort

            validate_thinking_effort(
                self.thinking_effort_override, label="thinking_effort_override"
            )
        normalized_access = replace(
            normalize_tool_access(self.tool_access),
            fixed=getattr(self.tool_access, "fixed", False),
            exact=getattr(self.tool_access, "exact", False),
        )
        if not isinstance(self.tools, dict):
            raise ValueError("temporary tools must be an object")
        if self.compaction_policy is not None:
            try:
                policy = normalize_compaction_policy(self.compaction_policy)
            except StorageError as error:
                raise ValueError(f"temporary compaction_policy is invalid: {error}") from error
            object.__setattr__(self, "compaction_policy", policy)
        if self.permission_rules is not None:
            object.__setattr__(
                self, "permission_rules", normalize_permission_rules(self.permission_rules)
            )
        if self.permission_ceiling is not None:
            PermissionCeiling.from_ceiling_dict(self.permission_ceiling)
            object.__setattr__(self, "permission_ceiling", deepcopy(dict(self.permission_ceiling)))
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "tool_access", normalized_access)
        object.__setattr__(self, "allowed_skills", list(self.allowed_skills))
        object.__setattr__(self, "tools", deepcopy(self.tools))
        object.__setattr__(self, "fallback_models", list(self.fallback_models or []))

    @property
    def has_profile(self) -> bool:
        """Whether a repository or identity Profile resolves this configuration."""
        return (
            self.repository_profile is not None
            or self.identity_profile is not None
            or self.identity_agent is not None
        )


@dataclass(frozen=True, kw_only=True)
class AppChatAgentConfig(TemporaryAgentConfig):
    """A declared operator chat profile, captured at Session creation."""

    profile_id: str
    profile_revision: str


@dataclass(frozen=True)
class TemporaryAgent:
    """A workspace-less RuntimeAgent resolved only from its Session binding."""

    id: str
    name: str
    model: str
    cwd: Path
    tool_access: ToolAccess
    allowed_skills: list[str]
    tools: dict[str, Any]
    fallback_models: list[str]
    delegation_bindings: dict[str, str | int] = field(default_factory=dict)
    instructions: str = ""
    prompt_blocks: list[str] | None = None
    workspace: str = ""
    root_project_id: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    thinking_effort: str | None = None
    memory_prompt_mode: MemoryPromptMode = MEMORY_PROMPT_MODE_OFF
    project_memory_enabled: bool = True
    custom_system_prompt_enabled: bool = False
    current_session_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    compaction_policy: dict[str, Any] | None = None
    # App chat profiles use packaged prompt blocks independent of user overrides.
    bundled_prompt: bool = False
    # A temporary Agent has no On-demand Tools.
    tool_loading: dict[str, Any] | None = None
    # The owner's rules (``TemporaryAgentConfig.permission_rules``) and ceiling.
    permission_rules: PermissionRules | None = None
    permission_ceiling: PermissionCeiling | None = None
    # A temporary Agent's rules come from its owner, not from files.
    rule_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class OwnedIdentityAgent(Agent):
    """A real Identity Agent with configuration scoped to one owned Session."""

    cwd: Path = field(default_factory=Path)
    instructions: str = ""
    prompt_blocks: list[str] | None = None
    owned_ceiling: PermissionCeiling | None = None

    @property
    @override
    def permission_ceiling(self) -> PermissionCeiling | None:
        return self.owned_ceiling


class TemporaryAgentRegistry:
    """Creates and resolves participants without touching Identity Agent storage."""

    def __init__(
        self,
        sessions: ChatSessionManager,
        *,
        prepare_config: Callable[[TemporaryAgentConfig, str | None], TemporaryAgentConfig]
        | None = None,
        capture_delegation_bindings: Callable[[dict[str, Any]], dict[str, str | int]] | None = None,
        resolve_identity: Callable[[str], Agent] | None = None,
    ) -> None:
        self._sessions = sessions
        self._prepare_config = prepare_config
        self._capture_delegation_bindings = capture_delegation_bindings
        self._resolve_identity = resolve_identity

    def create(
        self,
        *,
        owner_name: str,
        group_id: str,
        participant_id: str,
        config: TemporaryAgentConfig,
        project_id: str | None = None,
    ) -> TemporarySessionBinding:
        # The store reconciles this key inside its serialized write transaction.
        # A concurrent creator can therefore win without making the same participant
        # fail merely because it generated a different unused address.
        address = SessionAddress(
            None if config.identity_agent else project_id,
            config.identity_agent or new_id("tmp"),
            new_id("ses"),
        )
        binding_config = self._config_values(config)
        request = deepcopy(binding_config)
        existing = self._sessions.temporary_binding_by_participant(
            owner_name=owner_name, group_id=group_id, participant_id=participant_id
        )
        if existing is not None:
            if self._same_request(existing, project_id, request):
                return existing
            error_type = ValueError if config.has_profile else ChatSessionError
            raise error_type("temporary participant configuration differs from its snapshot")
        if config.has_profile:
            if self._prepare_config is None:
                raise ValueError("profile resolution is unavailable")
            config = self._prepare_config(config, project_id)
            binding_config = self._config_values(config)
            # The stored key predates identity Profiles; it holds either kind's request.
            binding_config["repository_profile_request"] = request
        if config.identity_agent is not None:
            self._identity(config.identity_agent, config.identity_incarnation)
            binding_config["working_project_id"] = project_id
        if project_id is None and self._capture_delegation_bindings is not None:
            bindings = self._capture_delegation_bindings(config.tools)
            if bindings:
                binding_config["delegation_bindings"] = bindings
        try:
            return self._sessions.create_bound_temporary_session(
                address,
                owner_name=owner_name,
                group_id=group_id,
                participant_id=participant_id,
                config=binding_config,
            )
        except ChatSessionError:
            # The first committed snapshot wins even if targets or the repo changed.
            existing = self._sessions.temporary_binding_by_participant(
                owner_name=owner_name, group_id=group_id, participant_id=participant_id
            )
            if existing is not None and self._same_request(existing, project_id, request):
                return existing
            raise

    @staticmethod
    def _same_request(
        binding: TemporarySessionBinding, project_id: str | None, request: dict[str, Any]
    ) -> bool:
        stored = binding.config.get("repository_profile_request")
        if stored is None:
            stored = {
                key: value for key, value in binding.config.items() if key != "delegation_bindings"
            }
        selected_project = (
            binding.config.get("working_project_id")
            if binding.config.get("identity_agent")
            else binding.address.project_id
        )
        return selected_project == project_id and stored == request

    @staticmethod
    def _config_values(config: TemporaryAgentConfig) -> dict[str, Any]:
        result: dict[str, Any] = {
            "model": config.model,
            "cwd": str(config.cwd),
            "tool_access": config.tool_access.to_dict(),
            "allowed_skills": list(config.allowed_skills),
            "tools": deepcopy(config.tools),
            "name": config.name,
            "temperature": config.temperature,
            "thinking_effort": config.thinking_effort,
            "fallback_models": list(config.fallback_models or []),
            "instructions": config.instructions,
            "prompt_blocks": deepcopy(config.prompt_blocks),
        }
        # Written only when set: an inheriting participant keeps the exact
        # configuration of bindings that predate the optional key, so their
        # idempotent re-creation still reconciles.
        if isinstance(config, AppChatAgentConfig):
            result["app_chat_profile"] = config.profile_id
            result["app_chat_revision"] = config.profile_revision
        if config.compaction_policy is not None:
            result["compaction_policy"] = deepcopy(config.compaction_policy)
        if not config.project_memory_enabled:
            result["project_memory_enabled"] = False
        if config.top_p is not None:
            result["top_p"] = config.top_p
        if config.tool_access.fixed:
            result["fixed_tool_access"] = True
        if config.tool_access.exact:
            result["exact_tool_access"] = True
        if config.repository_profile is not None:
            result["repository_profile"] = config.repository_profile
        if config.identity_profile is not None:
            result["identity_profile"] = config.identity_profile
        if config.identity_agent is not None:
            result["identity_agent"] = config.identity_agent
            result["identity_incarnation"] = config.identity_incarnation
        if config.model_override is not None:
            result["model_override"] = config.model_override
        if config.thinking_effort_override is not None:
            result["thinking_effort_override"] = config.thinking_effort_override
        if isinstance(config.permission_rules, PermissionRules):
            result["permission_rules"] = permission_rules_to_dict(config.permission_rules)
        if config.permission_ceiling is not None:
            result["permission_ceiling"] = deepcopy(dict(config.permission_ceiling))
        return result

    def session_agent(
        self, address: SessionAddress, *, include_temporary: bool = False
    ) -> TemporaryAgent | OwnedIdentityAgent | None:
        """Read the current binding of an addressed Session and resolve its Agent.

        Ordinary resolution applies owned Identity profiles. Inspection can also
        resolve a temporary participant and its same-Agent delegated Sessions.
        An address without a live Session never inherits another binding.
        """
        from core.sessions.errors import SessionNotFoundError

        try:
            binding = self._sessions.temporary_binding(address)
        except SessionNotFoundError:
            return None
        if binding is None and include_temporary:
            binding = self._sessions.temporary_agent_binding(address.project_id, address.agent_id)
        if binding is None or not (include_temporary or binding.config.get("identity_agent")):
            return None
        return self._from_binding(binding)

    async def session_agent_async(
        self, address: SessionAddress, *, include_temporary: bool = False
    ) -> TemporaryAgent | OwnedIdentityAgent | None:
        return await self._sessions.run_async(
            self.session_agent, address, include_temporary=include_temporary
        )

    def _identity(self, agent_id: str, incarnation: str | None) -> Agent:
        if self._resolve_identity is None:
            raise ValueError("Identity execution is unavailable")
        agent = self._resolve_identity(agent_id)
        if agent.builtin is not None or not incarnation or agent.incarnation != incarnation:
            raise ValueError("Identity Agent incarnation no longer matches")
        return agent

    def preview_identity(self, config: TemporaryAgentConfig) -> OwnedIdentityAgent:
        """Resolve the same scoped actual identity without creating a Session."""
        if config.identity_agent is None:
            raise ValueError("identity_agent is required")
        return self._owned_identity(
            self._config_values(config), SessionAddress(None, config.identity_agent, "preview")
        )

    def _owned_identity(
        self, config: dict[str, Any], address: SessionAddress
    ) -> OwnedIdentityAgent:
        from core.tools.availability import (
            bash_allowed_env_keys,
            intersect_tool_access,
            subagent_allowed_agents,
        )

        agent = self._identity(config["identity_agent"], config.get("identity_incarnation"))
        if address.project_id is not None or address.agent_id != agent.id:
            raise ValueError("Identity Session binding is invalid")
        owner_access = replace(
            normalize_tool_access(config["tool_access"]),
            fixed=config.get("fixed_tool_access", False),
            exact=config.get("exact_tool_access", False),
        )
        owner_skills = config["allowed_skills"]
        skills = (
            list(agent.allowed_skills)
            if "*" in owner_skills
            else list(owner_skills)
            if "*" in agent.allowed_skills
            else [name for name in owner_skills if name in agent.allowed_skills]
        )
        tool_settings = {**deepcopy(agent.tools), **deepcopy(config["tools"])}
        if "subagent" in config["tools"]:
            own_targets = subagent_allowed_agents(agent.tools)
            owner_targets = subagent_allowed_agents(config["tools"])
            targets = (
                own_targets
                if "*" in owner_targets
                else owner_targets
                if "*" in own_targets
                else [name for name in owner_targets if name in own_targets]
            )
            tool_settings["subagent"] = {"allowed_agents": targets}
        if "bash" in config["tools"]:
            own_keys = bash_allowed_env_keys(agent.tools)
            tool_settings["bash"] = {
                **tool_settings["bash"],
                "allowed_env": [
                    key for key in bash_allowed_env_keys(config["tools"]) if key in own_keys
                ],
            }
        return OwnedIdentityAgent(
            **{
                item.name: getattr(agent, item.name)
                for item in fields(Agent)
                if item.name
                not in {
                    "model",
                    "thinking_effort",
                    "tool_access",
                    "allowed_skills",
                    "tools",
                    "project_memory_enabled",
                }
            },
            model=config.get("model_override") or agent.model,
            thinking_effort=(
                config.get("thinking_effort_override")
                if config.get("thinking_effort_override") is not None
                else agent.thinking_effort
            ),
            tool_access=intersect_tool_access(owner_access, agent.tool_access),
            allowed_skills=skills,
            tools=tool_settings,
            cwd=Path(config["cwd"]),
            instructions=config.get("instructions", ""),
            prompt_blocks=deepcopy(config.get("prompt_blocks")),
            project_memory_enabled=(
                agent.project_memory_enabled and config.get("project_memory_enabled", True)
            ),
            owned_ceiling=(
                PermissionCeiling.from_ceiling_dict(config["permission_ceiling"])
                if config.get("permission_ceiling")
                else None
            ),
        )

    def resolve(
        self, address: SessionAddress, *, generation_id: str
    ) -> TemporaryAgent | OwnedIdentityAgent | None:
        binding = self._sessions.temporary_binding(address)
        if binding is None or binding.generation_id != generation_id:
            return None
        return self._from_binding(binding)

    def _from_binding(
        self, binding: TemporarySessionBinding
    ) -> TemporaryAgent | OwnedIdentityAgent:
        address = binding.address
        config = binding.config
        if config.get("identity_agent"):
            return self._owned_identity(config, address)
        from core.agents._delegation import validate_bindings

        bindings = config.get("delegation_bindings", {})
        if validate_bindings(bindings):
            raise ValueError("temporary Session binding configuration is invalid")
        compaction_policy = config.get("compaction_policy")
        project_memory_enabled = config.get("project_memory_enabled", True)
        if not isinstance(project_memory_enabled, bool):
            raise ValueError("temporary Session binding configuration is invalid")
        if compaction_policy is not None and not isinstance(compaction_policy, dict):
            raise ValueError("temporary Session binding configuration is invalid")
        try:
            return TemporaryAgent(
                id=address.agent_id,
                name=str(config["name"]),
                model=str(config["model"]),
                cwd=Path(str(config["cwd"])),
                tool_access=replace(
                    normalize_tool_access(config["tool_access"]),
                    fixed=config.get("fixed_tool_access", False),
                    exact=config.get("exact_tool_access", bool(config.get("app_chat_profile"))),
                ),
                allowed_skills=list(config["allowed_skills"]),
                tools=dict(config["tools"]),
                fallback_models=list(config.get("fallback_models", [])),
                delegation_bindings=dict(bindings),
                instructions=str(config.get("instructions", "")),
                bundled_prompt=config.get("app_chat_profile") is not None,
                prompt_blocks=deepcopy(config.get("prompt_blocks")),
                temperature=config.get("temperature"),
                top_p=config.get("top_p"),
                thinking_effort=config.get("thinking_effort"),
                project_memory_enabled=project_memory_enabled and address.project_id is not None,
                current_session_id=address.session_id,
                compaction_policy=deepcopy(compaction_policy),
                # Unreadable stored rules refuse the Session instead of dropping them.
                permission_rules=(
                    normalize_permission_rules(config["permission_rules"])
                    if config.get("permission_rules") is not None
                    else None
                ),
                permission_ceiling=(
                    PermissionCeiling.from_ceiling_dict(config["permission_ceiling"])
                    if config.get("permission_ceiling") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("temporary Session binding configuration is invalid") from error

    async def resolve_async(
        self, address: SessionAddress, *, generation_id: str
    ) -> TemporaryAgent | OwnedIdentityAgent | None:
        """Event-Loop-safe :meth:`resolve`; the binding read runs on the Session pool."""
        return await self._sessions.run_async(self.resolve, address, generation_id=generation_id)
