"""Validated Agent creation, configuration updates and compensated Workspace relocation."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents import _delegation
from core.agents import _workspace as workspace_ops
from core.agents._config import (
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING_EFFORT,
    _apply_defaults,
    _normalize_agent_description,
    _normalize_agent_name,
    _validate_agent_id,
    _validate_allowed_items,
    _validate_bool_field,
    _validate_excluded_skills,
    _validate_fallback_models,
    _validate_memory_prompt_mode,
    _validate_permission_rules,
    _validate_root_project_id,
    _validate_string_field,
    _validate_temperature,
    _validate_thinking_effort,
    _validate_tool_access,
    _validate_tool_loading,
    _validate_top_p,
    _with_builtin_capabilities,
    normalize_agent_tools,
)
from core.agents._types import (
    DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED,
    DEFAULT_LIBRARIAN_ENABLED,
    LIBRARIAN_BUILTIN,
    LIVE_BACKEND_BUILTIN,
    LIVE_VOICE_BUILTIN,
    Agent,
    AgentAlreadyExistsError,
    AgentError,
    AgentUpdateResult,
    BuiltinAgent,
    BuiltinAgentError,
)
from core.agents._workspace import _resolve_workspace, _WorkspaceRelocation
from core.memory import DEFAULT_MEMORY_PROMPT_MODE, MemoryPromptMode
from core.permissions import PermissionRules
from core.settings.normalizers import normalize_compaction_policy
from core.tools.availability import ToolAccess
from core.utils.file_status import exists_strict
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.agents.agents import AgentStore


_LOGGER = get_logger("agents")


def _log_permission_rules(agent_id: str, rules: PermissionRules | None) -> None:
    """Log a change of an Agent's Permission Rules: which parts, never their contents."""
    if rules is None:
        _LOGGER.info("Agent permission rules removed (agent=%s)", agent_id)
        return
    parts = [part for part in ("read", "write", "commands", "web") if getattr(rules, part)]
    _LOGGER.info(
        "Agent permission rules set (agent=%s, bases=%s, allow=%d, deny=%d)",
        agent_id,
        ",".join(parts) or "-",
        len(rules.allow or ()),
        len(rules.deny or ()),
    )


class AgentMutations:
    def __init__(self, owner: AgentStore) -> None:
        self._owner = owner

    def _create(
        self,
        agent_id: str,
        name: str | None,
        *,
        builtin: BuiltinAgent | None,
        model: str = DEFAULT_MODEL,
        fallback_models: list[str] | None = None,
        workspace: str | Path | None = None,
        temperature: float | None = DEFAULT_TEMPERATURE,
        top_p: float | None = None,
        thinking_effort: str | None = DEFAULT_THINKING_EFFORT,
        memory_prompt_mode: MemoryPromptMode = DEFAULT_MEMORY_PROMPT_MODE,
        tool_access: ToolAccess | Mapping[str, Any] | None = None,
        allowed_skills: list[str] | None = None,
        excluded_skills: list[str] | None = None,
        tools: Mapping[str, Any] | None = None,
        custom_system_prompt_enabled: bool = DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED,
        compaction_policy: dict[str, Any] | None = None,
        tool_loading: Mapping[str, Any] | None = None,
        permission_rules: PermissionRules | Mapping[str, Any] | None = None,
        librarian_enabled: bool = DEFAULT_LIBRARIAN_ENABLED,
        description: str | None = None,
    ) -> Agent:
        """:meth:`create` past the reserved-id rules; ``builtin`` marks a built-in Agent."""
        with self._owner._snapshot_barrier.compound_mutation(), self._owner._change():
            _validate_agent_id(agent_id)
            agent_dir = self._owner._agent_dir(agent_id)
            if exists_strict(agent_dir):
                raise AgentAlreadyExistsError(f"Agent already exists: {agent_id}")
            if agent_id in self._owner._renames._pending_rename_ids():
                raise AgentError(
                    f"Agent id {agent_id} belongs to an unfinished rename; "
                    "restart vBot to finish it"
                )

            validated_name = _normalize_agent_name(agent_id, name)
            validated_description = _normalize_agent_description(description)
            validated_model = _validate_string_field("model", model, allow_empty=True)
            validated_fallback_models = _validate_fallback_models(
                "fallback_models", fallback_models or []
            )
            validated_temperature = _validate_temperature(temperature)
            validated_top_p = _validate_top_p(top_p)
            validated_thinking_effort = _validate_thinking_effort(thinking_effort)
            validated_memory_prompt_mode = _validate_memory_prompt_mode(memory_prompt_mode)
            validated_tool_access = _validate_tool_access(tool_access)
            validated_allowed_skills = _validate_allowed_items("allowed_skills", allowed_skills)
            validated_excluded_skills = _validate_excluded_skills(excluded_skills)
            validated_tools = normalize_agent_tools(tools)
            validated_custom_system_prompt_enabled = _validate_bool_field(
                "custom_system_prompt_enabled", custom_system_prompt_enabled
            )
            validated_librarian_enabled = _validate_bool_field(
                "librarian_enabled", librarian_enabled
            )
            validated_compaction_policy = (
                normalize_compaction_policy(compaction_policy)
                if compaction_policy is not None
                else None
            )
            validated_tool_loading = _validate_tool_loading(tool_loading)
            validated_permission_rules = _validate_permission_rules(permission_rules)
            now = _delegation.new_creation_timestamp(self._owner)
            workspace_value = workspace
            if workspace_value is None or (
                isinstance(workspace_value, str) and not workspace_value.strip()
            ):
                workspace_value = self._owner._default_workspace(agent_id)
            workspace_path = _resolve_workspace(workspace_value, data_dir=self._owner._data_dir)

            # A new Agent has no Session: its first message creates one.
            agent_dir.mkdir(parents=True)
            agent = Agent(
                id=agent_id,
                name=validated_name,
                description=validated_description,
                model=validated_model,
                fallback_models=validated_fallback_models,
                workspace=str(workspace_path.resolve()),
                root_project_id=None,
                temperature=validated_temperature,
                top_p=validated_top_p,
                thinking_effort=validated_thinking_effort,
                memory_prompt_mode=validated_memory_prompt_mode,
                tool_access=validated_tool_access,
                allowed_skills=validated_allowed_skills,
                excluded_skills=validated_excluded_skills,
                tools=validated_tools,
                custom_system_prompt_enabled=validated_custom_system_prompt_enabled,
                compaction_policy=validated_compaction_policy,
                tool_loading=validated_tool_loading,
                permission_rules=validated_permission_rules,
                librarian_enabled=validated_librarian_enabled,
                created_at=now,
                updated_at=now,
                builtin=builtin,
            )
            agent = _with_builtin_capabilities(agent)

            try:
                agent = _delegation.bind_changes(self._owner, agent, None)
                self._owner._seed_workspace(Path(agent.workspace))
                self._owner._write_agent(agent)
            except Exception:
                shutil.rmtree(agent_dir, ignore_errors=True)
                raise
            # ``list_with_order`` appends this newly valid Agent after every existing
            # roster entry and persists that projection. The config write remains the
            # creation commit point; an auxiliary order write failure is logged there
            # and never turns a successfully created identity into a false failure.
            if agent.permission_rules is not None:
                _log_permission_rules(agent.id, agent.permission_rules)
            self._owner.list_with_order()
            return _apply_defaults(agent, self._owner._agent_defaults())

    def update_with_metadata(
        self,
        agent_id: str,
        *,
        copy_workspace_identity_files: bool = False,
        **changes: Any,
    ) -> AgentUpdateResult:
        """Update an Agent and transactionally relocate its identity files.

        The copy directive is operation input, not persisted configuration. Only
        ``SOUL.md``, ``USER.md``, and ``MEMORY.md`` can move. Sources are preserved,
        replaced destination files are backed up under the Agent's data home, and
        a failure restores every destination touched before leaving the config on
        its original Workspace.
        """
        with self._owner._change():
            _validate_agent_id(agent_id)
            if "id" in changes and changes["id"] != agent_id:
                raise AgentError("Agent id is immutable")

            changes.pop("id", None)
            agent_path = self._owner._require_agent_path(agent_id)
            agent = self._owner._load_verified_agent(agent_path)
            if "subagent_allow" in changes:
                if "tools" in changes:
                    raise AgentError("subagent_allow cannot be combined with tools")
                # Patch only this setting, using the current Agent while the store's
                # mutation lock is held; a CLI read/replace would lose sibling changes.
                subagent = dict(agent.tools.get("subagent", {}))
                subagent["allowed_agents"] = changes.pop("subagent_allow")
                changes["tools"] = {**agent.tools, "subagent": subagent}
            if agent.builtin is not None:
                editable = _BUILTIN_EDITABLE_FIELDS[agent.builtin]
                fixed_fields = sorted(set(changes) - editable)
                if fixed_fields or copy_workspace_identity_files:
                    names = ", ".join(sorted(editable - {"current_session_id"}))
                    raise BuiltinAgentError(
                        f"The {agent.name} Agent is built into vBot: only its {names} can "
                        "change, not "
                        f"{', '.join(fixed_fields or ['copy_workspace_identity_files'])}"
                    )
            if not changes:
                if copy_workspace_identity_files:
                    raise AgentError("copy_workspace_identity_files requires a workspace change")
                return AgentUpdateResult(_apply_defaults(agent, self._owner._agent_defaults()))

            allowed_fields = set(Agent.__dataclass_fields__) - {
                "id",
                "created_at",
                "updated_at",
                "delegation_bindings",
            }
            unknown_fields = sorted(set(changes) - allowed_fields)
            if unknown_fields:
                raise AgentError(f"Unknown agent fields: {', '.join(unknown_fields)}")

            if "name" in changes:
                changes["name"] = _normalize_agent_name(agent_id, changes["name"])
            if "description" in changes:
                changes["description"] = _normalize_agent_description(changes["description"])
            # An empty current_session_id clears the pointer: the Agent opens a new Session.
            string_fields = {"model", "current_session_id"}
            for field_name in sorted(string_fields & set(changes)):
                changes[field_name] = _validate_string_field(
                    field_name, changes[field_name], allow_empty=True
                )
            if "fallback_models" in changes:
                changes["fallback_models"] = _validate_fallback_models(
                    "fallback_models", changes["fallback_models"]
                )
            if "workspace" in changes:
                workspace = changes["workspace"]
                if workspace is None or (isinstance(workspace, str) and not workspace.strip()):
                    workspace = self._owner._default_workspace(agent_id)
                changes["workspace"] = str(
                    _resolve_workspace(workspace, data_dir=self._owner._data_dir)
                )
                if changes["workspace"] == agent.workspace:
                    if copy_workspace_identity_files:
                        raise AgentError(
                            "copy_workspace_identity_files requires a changed workspace"
                        )
                    changes.pop("workspace")
            elif copy_workspace_identity_files:
                raise AgentError("copy_workspace_identity_files requires a workspace change")
            if "root_project_id" in changes:
                changes["root_project_id"] = _validate_root_project_id(changes["root_project_id"])
            if "temperature" in changes:
                changes["temperature"] = _validate_temperature(changes["temperature"])
            if "top_p" in changes:
                changes["top_p"] = _validate_top_p(changes["top_p"])
            if "thinking_effort" in changes:
                changes["thinking_effort"] = _validate_thinking_effort(changes["thinking_effort"])
            if "memory_prompt_mode" in changes:
                changes["memory_prompt_mode"] = _validate_memory_prompt_mode(
                    changes["memory_prompt_mode"]
                )
            if "tool_access" in changes:
                changes["tool_access"] = _validate_tool_access(changes["tool_access"])
            if "allowed_skills" in changes:
                changes["allowed_skills"] = _validate_allowed_items(
                    "allowed_skills", changes["allowed_skills"]
                )
            if "excluded_skills" in changes:
                changes["excluded_skills"] = _validate_excluded_skills(changes["excluded_skills"])
            if "tools" in changes:
                changes["tools"] = normalize_agent_tools(changes["tools"])
            for bool_field in ("custom_system_prompt_enabled", "librarian_enabled"):
                if bool_field in changes:
                    changes[bool_field] = _validate_bool_field(bool_field, changes[bool_field])
            if "compaction_policy" in changes:
                policy = changes["compaction_policy"]
                changes["compaction_policy"] = (
                    normalize_compaction_policy(policy) if policy is not None else None
                )
            if "tool_loading" in changes:
                changes["tool_loading"] = _validate_tool_loading(changes["tool_loading"])
            if "permission_rules" in changes:
                changes["permission_rules"] = _validate_permission_rules(
                    changes["permission_rules"]
                )
            if changes.get("current_session_id"):
                self._owner._validate_current_session(agent_id, changes["current_session_id"])

            if not changes:
                return AgentUpdateResult(_apply_defaults(agent, self._owner._agent_defaults()))

            updated_agent = _with_builtin_capabilities(
                replace(agent, **changes, updated_at=utc_now_timestamp())
            )
            updated_agent = _delegation.bind_changes(self._owner, updated_agent, agent)
            relocation = _WorkspaceRelocation()
            try:
                if "workspace" in changes:
                    relocation = workspace_ops.relocate_workspace(
                        self._owner._agent_dir(agent_id),
                        self._owner._template_dir,
                        agent,
                        Path(updated_agent.workspace),
                        copy_identity_files=copy_workspace_identity_files,
                    )
                self._owner._write_agent(updated_agent)
            except Exception:
                relocation.rollback()
                raise
            if updated_agent.permission_rules != agent.permission_rules:
                _log_permission_rules(agent_id, updated_agent.permission_rules)
            return AgentUpdateResult(
                agent=_apply_defaults(updated_agent, self._owner._agent_defaults()),
                copied_files=relocation.copied_files,
                backed_up_files=relocation.backed_up_files,
                backup_dir=str(relocation.backup_dir) if relocation.backup_dir else None,
                created_files=relocation.created_files,
                destination=str(relocation.destination) if relocation.destination else None,
            )

    def restore_update(self, previous_agent: Agent, result: AgentUpdateResult) -> None:
        """Compensate a completed update during a larger coordinated operation."""
        with self._owner._change():
            if result.destination is not None:
                relocation = _WorkspaceRelocation(
                    destination=Path(result.destination),
                    copied_files=result.copied_files,
                    backed_up_files=result.backed_up_files,
                    created_files=result.created_files,
                    backup_dir=Path(result.backup_dir) if result.backup_dir else None,
                )
                relocation.rollback()
            self._owner._write_agent(previous_agent)


# What the user may change on a built-in Agent: its Model settings, and on the
# Agents of a Live call also their Tools. The current Session follows the
# Session the user opens.
_BUILTIN_MODEL_FIELDS = frozenset(
    {
        "model",
        "fallback_models",
        "temperature",
        "top_p",
        "thinking_effort",
        "current_session_id",
    }
)


_BUILTIN_EDITABLE_FIELDS: dict[str, frozenset[str]] = {
    LIBRARIAN_BUILTIN: _BUILTIN_MODEL_FIELDS,
    LIVE_VOICE_BUILTIN: _BUILTIN_MODEL_FIELDS | {"tool_access"},
    LIVE_BACKEND_BUILTIN: _BUILTIN_MODEL_FIELDS | {"tool_access"},
}
