"""Identity Agent JSON validation and field normalization."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from core.agents._types import (
    DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED,
    Agent,
    AgentError,
    InvalidAgentIdError,
    _AgentOrderDocument,
)
from core.agents._workspace import (
    _workspace_from_data,
)
from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonObject,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_allowed_string,
    validate_json_file,
    validate_non_empty_string,
    validate_optional_path_string,
    validate_positive_integer,
    validate_required_fields,
    validate_string,
    validate_string_list,
    warn_unknown_keys,
)
from core.memory import (
    DEFAULT_MEMORY_PROMPT_MODE,
    MEMORY_PROMPT_MODES,
    MemoryPromptMode,
    validate_memory_prompt_mode,
)
from core.settings import (
    AgentDefaults,
    SettingsValidationError,
    bake_agent_defaults,
    is_valid_agent_id,
    validate_temperature,
    validate_thinking_effort,
)
from core.settings.agent_defaults import validate_fallback_chain
from core.settings.validation import (
    validate_optional_compaction_policy,
    validate_temperature_diagnostic,
    validate_thinking_effort_diagnostic,
)
from core.tools.availability import (
    BASH_ALLOWED_ENV_KEY,
    BASH_TOOL_SETTINGS_KEY,
    ToolAccess,
    normalize_env_keys,
    normalize_tool_access,
)

DEFAULT_FALLBACK_MODELS: list[str] = []

DEFAULT_MODEL = ""

DEFAULT_TEMPERATURE: float | None = None

DEFAULT_THINKING_EFFORT: str | None = None

DEFAULT_ALLOWED_ITEMS = ("*",)

_AGENT_CONFIG_FIELDS = frozenset(
    {
        "allowed_skills",
        "compaction_policy",
        "created_at",
        "current_session_id",
        "custom_system_prompt_enabled",
        "fallback_models",
        "id",
        "memory_prompt_mode",
        "model",
        "name",
        "root_project_id",
        "tools",
        "temperature",
        "thinking_effort",
        "tool_access",
        "updated_at",
        "workspace",
    }
)

_SUBAGENT_TOOL_SETTING_FIELDS = frozenset({"allowed_agents"})

_BASH_TOOL_SETTING_FIELDS = frozenset({BASH_ALLOWED_ENV_KEY})

_AGENT_ORDER_FIELDS = frozenset({"agent_ids", "revision"})


def validate_agent_order_file(order_path: str | Path) -> JsonValidationReport:
    """Validate the optional persisted Identity Agent order document."""
    return validate_json_file(order_path, validate_agent_order_data, missing_ok=True)


def validate_agent_order_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``agents/order.json`` mapping."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

    warn_unknown_keys(diagnostics, "$", data, _AGENT_ORDER_FIELDS, "agent order field")
    validate_required_fields(diagnostics, "$", data, _AGENT_ORDER_FIELDS)
    if "revision" in data:
        validate_positive_integer(diagnostics, "$.revision", data["revision"], required=True)
    agent_ids = data.get("agent_ids")
    if "agent_ids" in data:
        validate_string_list(diagnostics, "$.agent_ids", agent_ids)
    if isinstance(agent_ids, list):
        seen: set[str] = set()
        for index, agent_id in enumerate(agent_ids):
            if not isinstance(agent_id, str):
                continue
            if not is_valid_agent_id(agent_id):
                add_error(
                    diagnostics,
                    f"$.agent_ids[{index}]",
                    "must be a valid Agent id",
                )
            if agent_id in seen:
                add_error(diagnostics, f"$.agent_ids[{index}]", "must be unique")
            seen.add(agent_id)
    return diagnostics


def validate_agent_file(agent_path: str | Path) -> JsonValidationReport:
    """Validate one persisted ``agent.json`` without consuming it."""
    return validate_json_file(agent_path, validate_agent_data, missing_ok=False)


def load_validated_agent_json(agent_path: str | Path) -> JsonObject:
    """Load one schema-valid ``agent.json`` mapping."""
    try:
        return cast(
            "JsonObject",
            load_validated_json_file(agent_path, validate_agent_data, missing_ok=False),
        )
    except JsonConfigValidationError as error:
        raise AgentError(str(error)) from error


def validate_agent_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``agent.json`` mapping."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

    warn_unknown_keys(diagnostics, "$", data, _AGENT_CONFIG_FIELDS, "agent field")
    if "allowed_tools" in data:
        add_error(
            diagnostics,
            "$.allowed_tools",
            "retired Identity Agent field; run the agent Tool-access converter",
        )
    _validate_agent_config_id(diagnostics, "$.id", data.get("id"))
    validate_non_empty_string(diagnostics, "$.name", data.get("name"), required=False)
    validate_string(diagnostics, "$.model", data.get("model"), required=False)
    _validate_fallback_models_diagnostics(
        diagnostics, "$.fallback_models", data.get("fallback_models")
    )
    validate_optional_path_string(diagnostics, "$.workspace", data.get("workspace"))
    validate_non_empty_string(
        diagnostics,
        "$.root_project_id",
        data.get("root_project_id"),
        required=False,
    )
    validate_temperature_diagnostic(
        diagnostics, "$.temperature", data.get("temperature"), allow_none=True
    )
    validate_thinking_effort_diagnostic(
        diagnostics,
        "$.thinking_effort",
        data.get("thinking_effort"),
        allow_none=True,
    )
    if data.get("memory_prompt_mode") is not None:
        validate_allowed_string(
            diagnostics,
            "$.memory_prompt_mode",
            data["memory_prompt_mode"],
            frozenset(MEMORY_PROMPT_MODES),
        )
    if data.get("tool_access") is not None:
        try:
            normalize_tool_access(data["tool_access"])
        except ValueError as error:
            add_error(diagnostics, "$.tool_access", str(error))
    if data.get("allowed_skills") is not None:
        validate_string_list(diagnostics, "$.allowed_skills", data["allowed_skills"])
    if data.get("tools") is not None:
        _validate_agent_tools_diagnostics(diagnostics, data["tools"])
    if data.get("custom_system_prompt_enabled") is not None and not isinstance(
        data["custom_system_prompt_enabled"], bool
    ):
        add_error(diagnostics, "$.custom_system_prompt_enabled", "must be a boolean")
    validate_optional_compaction_policy(
        diagnostics, data.get("compaction_policy"), "$.compaction_policy"
    )
    validate_string(diagnostics, "$.created_at", data.get("created_at"), required=False)
    validate_string(diagnostics, "$.updated_at", data.get("updated_at"), required=False)
    if data.get("current_session_id") is not None:
        validate_string(
            diagnostics, "$.current_session_id", data.get("current_session_id"), required=False
        )
    return diagnostics


def _validate_agent_tools_diagnostics(diagnostics: list[JsonDiagnostic], tools: Any) -> None:
    if not isinstance(tools, dict):
        add_error(diagnostics, "$.tools", "must be an object")
        return
    for tool_name, tool_settings in tools.items():
        path = f"$.tools.{tool_name}"
        if tool_settings is None:
            continue
        if not isinstance(tool_settings, dict):
            add_error(diagnostics, path, "must be an object")
    bash = tools.get(BASH_TOOL_SETTINGS_KEY)
    if isinstance(bash, dict):
        bash_path = f"$.tools.{BASH_TOOL_SETTINGS_KEY}"
        warn_unknown_keys(
            diagnostics,
            bash_path,
            bash,
            _BASH_TOOL_SETTING_FIELDS,
            "bash setting",
        )
        allowed_env = bash.get(BASH_ALLOWED_ENV_KEY)
        if allowed_env is not None:
            try:
                normalize_env_keys(
                    allowed_env,
                    field_name=f"tools.{BASH_TOOL_SETTINGS_KEY}.{BASH_ALLOWED_ENV_KEY}",
                )
            except ValueError as error:
                add_error(
                    diagnostics,
                    f"{bash_path}.{BASH_ALLOWED_ENV_KEY}",
                    str(error),
                )
    subagent = tools.get("subagent")
    if not isinstance(subagent, dict):
        return
    warn_unknown_keys(
        diagnostics,
        "$.tools.subagent",
        subagent,
        _SUBAGENT_TOOL_SETTING_FIELDS,
        "subagent setting",
    )
    if subagent.get("allowed_agents") is not None:
        validate_string_list(
            diagnostics,
            "$.tools.subagent.allowed_agents",
            subagent["allowed_agents"],
        )


def _validate_agent_config_id(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        add_error(diagnostics, path, "must be a non-empty string")
    elif not is_valid_agent_id(value):
        add_error(
            diagnostics,
            path,
            "must be 1-64 characters using only letters, numbers, hyphen, or underscore",
        )


def _validate_string_field(field: str, value: Any, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise AgentError(f"{field} must be a string")
    if not allow_empty and not value:
        raise AgentError(f"{field} must be a non-empty string")
    return value


def _apply_agent_order(
    agents: list[Agent],
    order: _AgentOrderDocument | None,
) -> list[Agent]:
    """Project valid Agents through stored order, appending new ids by id."""
    if order is None:
        return agents

    agents_by_id = {agent.id: agent for agent in agents}
    ordered = [agents_by_id[agent_id] for agent_id in order.agent_ids if agent_id in agents_by_id]
    ordered_ids = {agent.id for agent in ordered}
    ordered.extend(agent for agent in agents if agent.id not in ordered_ids)
    return ordered


def _normalize_agent_name(agent_id: str, value: Any) -> str:
    """Use the immutable id as the display name when no name is configured."""
    if value is None:
        return agent_id
    if not isinstance(value, str):
        raise AgentError("name must be a string or null")
    return value if value.strip() else agent_id


def _validate_temperature(value: Any) -> float | None:
    try:
        return validate_temperature(value, label="temperature", allow_none=True)
    except SettingsValidationError as exc:
        raise AgentError(str(exc)) from exc


def _validate_thinking_effort(value: Any) -> str | None:
    try:
        return validate_thinking_effort(value, label="thinking_effort", allow_none=True)
    except SettingsValidationError as exc:
        raise AgentError(str(exc)) from exc


def _validate_memory_prompt_mode(value: Any) -> MemoryPromptMode:
    if not isinstance(value, str):
        raise AgentError("memory_prompt_mode must be a string")
    try:
        return validate_memory_prompt_mode(value)
    except ValueError as exc:
        allowed = ", ".join(repr(item) for item in MEMORY_PROMPT_MODES)
        raise AgentError(f"memory_prompt_mode must be one of: {allowed}") from exc


def _validate_allowed_items(field: str, items: list[str] | None) -> list[str]:
    if items is None:
        return list(DEFAULT_ALLOWED_ITEMS)
    if not isinstance(items, list):
        raise AgentError(f"{field} must be a list of strings")
    if not all(isinstance(item, str) for item in items):
        raise AgentError(f"{field} must be a list of strings")
    return list(items)


def _validate_fallback_models(field: str, items: Any) -> list[str]:
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        raise AgentError(f"{field} must be a list of strings")
    try:
        return validate_fallback_chain(items)
    except ValueError as error:
        raise AgentError(f"{field} {error}") from error


def _validate_fallback_models_diagnostics(
    diagnostics: list[JsonDiagnostic], path: str, items: Any
) -> None:
    if items is None:
        return
    try:
        _validate_fallback_models(path, items)
    except AgentError as error:
        add_error(diagnostics, path, str(error).removeprefix(f"{path} "))


def _validate_tool_access(value: ToolAccess | Mapping[str, Any] | None) -> ToolAccess:
    try:
        return normalize_tool_access(value)
    except ValueError as error:
        raise AgentError(str(error)) from error


def _normalize_agent_tools(tools: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and copy the optional Tool-settings blocks from agent.json."""
    if tools is None:
        return {}
    if not isinstance(tools, Mapping):
        raise AgentError("tools must be an object")
    normalized_tools: dict[str, Any] = {}
    for tool_name, tool_settings in tools.items():
        if not isinstance(tool_name, str) or not tool_name:
            raise AgentError("tools keys must be non-empty strings")
        if tool_settings is None:
            continue
        if not isinstance(tool_settings, Mapping):
            raise AgentError(f"tools.{tool_name} must be an object")
        normalized_tools[tool_name] = deepcopy(dict(tool_settings))
    bash = normalized_tools.get(BASH_TOOL_SETTINGS_KEY)
    if isinstance(bash, dict):
        unsupported_bash = sorted(set(bash) - _BASH_TOOL_SETTING_FIELDS)
        if unsupported_bash:
            raise AgentError(
                f"Unsupported tools.{BASH_TOOL_SETTINGS_KEY} fields: " + ", ".join(unsupported_bash)
            )
        if BASH_ALLOWED_ENV_KEY in bash:
            try:
                bash[BASH_ALLOWED_ENV_KEY] = normalize_env_keys(
                    bash[BASH_ALLOWED_ENV_KEY],
                    field_name=f"tools.{BASH_TOOL_SETTINGS_KEY}.{BASH_ALLOWED_ENV_KEY}",
                )
            except ValueError as error:
                raise AgentError(str(error)) from error
    subagent = normalized_tools.get("subagent")
    if isinstance(subagent, dict):
        unsupported_subagent = sorted(set(subagent) - _SUBAGENT_TOOL_SETTING_FIELDS)
        if unsupported_subagent:
            raise AgentError(
                "Unsupported tools.subagent fields: " + ", ".join(unsupported_subagent)
            )
        if "allowed_agents" in subagent:
            subagent["allowed_agents"] = _validate_allowed_items(
                "tools.subagent.allowed_agents",
                subagent["allowed_agents"],
            )
    return normalized_tools


def _validate_bool_field(field: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise AgentError(f"{field} must be a boolean")
    return value


def _replace_list_item_once(items: list[str], old: str, new: str) -> list[str]:
    """Replace an exact list item and preserve order without creating duplicates."""
    replaced: list[str] = []
    for item in items:
        candidate = new if item == old else item
        if candidate not in replaced:
            replaced.append(candidate)
    return replaced


def _validate_root_project_id(project_id: Any) -> str | None:
    if project_id is None:
        return None
    if not isinstance(project_id, str) or not project_id.strip():
        raise AgentError("root_project_id must be null or a non-empty string")
    return project_id


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _agent_from_dict(
    data: dict[str, Any],
    *,
    data_dir: str | Path,
    default_workspace: str | Path | None = None,
) -> Agent:
    """Build an Agent from a mapping already validated by ``load_validated_agent_json``.

    Field rules are enforced once by this domain's ``validate_agent_data`` at load
    time; this constructor only normalizes shapes (workspace fallback, tool
    sanitization, optional-field defaults) without re-validating.
    """
    agent_id = cast(str, data["id"])
    timestamp_default = _utc_now()
    temperature = data.get("temperature")
    memory_prompt_mode = data.get("memory_prompt_mode")
    return Agent(
        id=agent_id,
        name=data.get("name") or agent_id,
        model=data.get("model") or "",
        fallback_models=_validate_fallback_models(
            "fallback_models", data.get("fallback_models") or []
        ),
        workspace=str(
            _workspace_from_data(
                data.get("workspace"),
                data_dir=data_dir,
                default_workspace=default_workspace,
            )
        ),
        root_project_id=data.get("root_project_id"),
        temperature=None if temperature is None else float(temperature),
        thinking_effort=data.get("thinking_effort"),
        memory_prompt_mode=cast(MemoryPromptMode, memory_prompt_mode or DEFAULT_MEMORY_PROMPT_MODE),
        tool_access=_validate_tool_access(data.get("tool_access")),
        allowed_skills=_validate_allowed_items("allowed_skills", data.get("allowed_skills")),
        tools=_normalize_agent_tools(data.get("tools")),
        custom_system_prompt_enabled=bool(
            data.get("custom_system_prompt_enabled", DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED)
        ),
        compaction_policy=(
            dict(data["compaction_policy"])
            if isinstance(data.get("compaction_policy"), dict)
            else None
        ),
        current_session_id=data.get("current_session_id") or "",
        created_at=data.get("created_at") or timestamp_default,
        updated_at=data.get("updated_at") or timestamp_default,
    )


def _validated_agent_data(agent_path: Path) -> JsonObject:
    data = load_validated_agent_json(agent_path)
    directory_id = agent_path.parent.name
    if data["id"] != directory_id:
        raise AgentError(
            f"{agent_path}: Agent id {data['id']!r} does not match directory {directory_id!r}"
        )
    return data


def _apply_defaults(agent: Agent, defaults: AgentDefaults) -> Agent:
    changes = bake_agent_defaults(
        model=agent.model,
        fallback_models=agent.fallback_models,
        temperature=agent.temperature,
        thinking_effort=agent.thinking_effort,
        defaults=defaults,
    )
    if not changes:
        return agent
    return replace(agent, **changes)


def _validate_agent_id(agent_id: str) -> None:
    if not is_valid_agent_id(agent_id):
        raise InvalidAgentIdError(
            "Agent id must be 1-64 characters using only letters, numbers, hyphen, or underscore"
        )
