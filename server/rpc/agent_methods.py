"""Agent methods."""

from __future__ import annotations

import inspect
from contextlib import AsyncExitStack
from typing import Any, cast

from core.memory import MEMORY_PROMPT_MODES
from core.prompts import load_bundled_default_layout
from core.runs import RunAdmissionBlockedError
from core.settings import (
    ALLOWED_THINKING_EFFORTS,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    SettingsValidationError,
    validate_temperature,
    validate_thinking_effort,
)
from core.tools.availability import (
    BASH_ALLOWED_ENV_KEY,
    BASH_TOOL_SETTINGS_KEY,
    normalize_env_keys,
    normalize_tool_access,
)
from core.utils.logging import get_logger
from server.events import (
    RESOURCE_KIND_AGENTS,
    RESOURCE_KIND_CHANNELS,
    RESOURCE_KIND_CRON,
    RESOURCE_KIND_SESSIONS,
)
from server.rpc._session_workers import _SESSION_RPC_WORKERS
from server.rpc.agent_refs import (
    _agent_reference_ids,
    _agent_reference_lock,
    _rename_agent_and_retarget_references,
    _subagents_reference_identity_agent,
)
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_AGENT_BUSY,
    RPC_ERROR_AGENT_IN_USE,
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_LAST_AGENT,
    RpcError,
)
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.payloads import _agent_response
from server.rpc.runtime_access import _state_chat_runs
from server.rpc.validation import (
    _ensure_model_connection_supported,
    _reject_unsupported,
    _required_string,
    _validate_string_list,
)

JsonObject = dict[str, Any]

_LOGGER = get_logger("server.rpc.agents")

__all__ = ["ALLOWED_THINKING_EFFORTS", "MAX_TEMPERATURE", "MIN_TEMPERATURE"]


def _list_agents(state: Any) -> JsonObject:
    try:
        listing = state.runtime.agents.list_with_order()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _agent_list_response(state, listing)


async def _reorder_agents(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_ids", "expected_revision"}, "agent.reorder")
    agent_ids = _validate_string_list("agent_ids", params.get("agent_ids"))
    expected_revision = params.get("expected_revision")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.expected_revision must be a non-negative integer",
        )

    try:
        async with _agent_reference_lock(state):
            listing = state.runtime.agents.reorder(
                agent_ids,
                expected_revision=expected_revision,
            )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if listing.order_changed:
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
        _LOGGER.info("Agent order updated (agents=%s)", ",".join(agent_ids))
    return _agent_list_response(state, listing)


def _agent_list_response(state: Any, listing: Any) -> JsonObject:
    return {
        "agents": [_agent_response(state, agent) for agent in listing.agents],
        "order_revision": listing.order_revision,
    }


def _get_agent(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "agent.get")

    agent_id = _required_string(params, "id")
    try:
        agent = state.runtime.agents.get(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _agent_response(state, agent)


def _create_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        changes = _agent_changes(params, blocked={"id"}, for_create=True)
        name = changes.pop("name", None)
        _ensure_agent_model_connections(state, changes)
        state.runtime.agents.create(agent_id, name, **changes)
        if changes.get("custom_system_prompt_enabled") is True:
            _seed_agent_custom_prompt(state, agent_id)
        agent = state.runtime.agents.get(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(state, agent)
    # Agent CRUD rides the generic reload-on-change channel ("one app system"):
    # the signal carries no agent data, open windows re-fetch agent.list.
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    _LOGGER.info("Agent created (agent=%s)", agent_id)
    return response


def _update_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        changes = _agent_changes(params, blocked={"id"}, for_create=False)
        copy_workspace_identity_files = changes.pop("copy_workspace_identity_files", False)
        root_project_id = changes.get("root_project_id")
        if root_project_id is not None and not state.runtime.projects.exists(root_project_id):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"unknown Project: {root_project_id}",
            )
        _ensure_agent_model_connections(state, changes)
        previous_agent = state.runtime.agents.get(agent_id)
        if (
            changes.get("custom_system_prompt_enabled") is True
            and not previous_agent.custom_system_prompt_enabled
        ):
            _seed_agent_custom_prompt(state, agent_id)
        update_result = state.runtime.agents.update_with_metadata(
            agent_id,
            copy_workspace_identity_files=copy_workspace_identity_files,
            **changes,
        )
        agent = update_result.agent
        changed_fields = sorted(
            field
            for field in changes
            if getattr(previous_agent, field, None) != getattr(agent, field, None)
        )
        if update_result.copied_files or update_result.backed_up_files:
            changed_fields.append("workspace_identity_files")
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(state, agent)
    response["workspace_relocation"] = {
        "copied_files": list(update_result.copied_files),
        "backed_up_files": list(update_result.backed_up_files),
        "backup_created": update_result.backup_dir is not None,
    }
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    if changed_fields:
        _LOGGER.info(
            "Agent updated (agent=%s fields=%s)",
            agent_id,
            ",".join(changed_fields),
        )
    return response


async def _rename_agent(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "new_id"}, "agent.rename")
    agent_id = _required_string(params, "id")
    new_agent_id = _required_string(params, "new_id")
    if agent_id == new_agent_id:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.new_id must differ from params.id",
        )

    try:
        async with _agent_reference_lock(state):
            try:
                async with AsyncExitStack() as guards:
                    for guarded_agent_id in sorted((agent_id, new_agent_id)):
                        await guards.enter_async_context(
                            _state_chat_runs(state).agent_admission_guard(
                                guarded_agent_id,
                                project_id=None,
                            )
                        )
                    busy_subagent_ids = [
                        guarded_agent_id
                        for guarded_agent_id in sorted((agent_id, new_agent_id))
                        if _subagents_reference_identity_agent(state, guarded_agent_id)
                    ]
                    if busy_subagent_ids:
                        raise RpcError(
                            RPC_ERROR_AGENT_BUSY,
                            (
                                "cannot rename agent while the old or new id has open "
                                f"Sub-Agent activity: {', '.join(busy_subagent_ids)}"
                            ),
                        )
                    result = await _SESSION_RPC_WORKERS.run(
                        _rename_agent_and_retarget_references,
                        state,
                        agent_id,
                        new_agent_id,
                    )
                    invalidate_agent_skills = getattr(
                        state.runtime,
                        "invalidate_agent_skills",
                        None,
                    )
                    if callable(invalidate_agent_skills):
                        invalidate_agent_skills(agent_id)
                        invalidate_agent_skills(new_agent_id)
            except RunAdmissionBlockedError as exc:
                raise RpcError(
                    RPC_ERROR_AGENT_BUSY,
                    (
                        "cannot rename agent while the old or new id has active "
                        f"or queued runs: {agent_id} -> {new_agent_id}"
                    ),
                ) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    await _remove_renamed_sessions_from_recall(state, agent_id, result.session_ids)

    response = _agent_response(state, result.agent)
    response["rename"] = {
        "old_id": agent_id,
        "new_id": new_agent_id,
        "channels_updated": list(result.channel_ids),
        "cron_jobs_updated": list(result.cron_job_ids),
        "bootstrap_jobs_updated": list(result.bootstrap_job_ids),
        "agent_policies_updated": list(result.policy_agent_ids),
        "session_links_updated": result.session_reference_count,
    }
    rename_scope = {"old_agent_id": agent_id, "new_agent_id": new_agent_id}
    publish_resource_changed(state, RESOURCE_KIND_AGENTS, scope=rename_scope)
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope=rename_scope)
    if result.channel_ids:
        publish_resource_changed(state, RESOURCE_KIND_CHANNELS)
    if result.cron_job_ids:
        publish_resource_changed(state, RESOURCE_KIND_CRON)
    _LOGGER.info(
        "Agent renamed (agent=%s new_agent=%s channels=%s cron=%s "
        "bootstrap=%s policies=%s session_links=%s)",
        agent_id,
        new_agent_id,
        len(result.channel_ids),
        len(result.cron_job_ids),
        len(result.bootstrap_job_ids),
        len(result.policy_agent_ids),
        result.session_reference_count,
    )
    return response


async def _remove_renamed_sessions_from_recall(
    state: Any,
    old_agent_id: str,
    session_ids: tuple[str, ...],
) -> None:
    """Best-effort cleanup of disposable old-address recall index rows."""
    remove_session = getattr(state.runtime, "remove_session_from_recall", None)
    if not callable(remove_session):
        return
    for session_id in session_ids:
        try:
            cleanup = remove_session(old_agent_id, session_id, None)
            if inspect.isawaitable(cleanup):
                await cleanup
        except Exception as error:
            _LOGGER.warning(
                "Recall cleanup failed after Agent rename (agent=%s session=%s): %s",
                old_agent_id,
                session_id,
                error,
            )


def _seed_agent_custom_prompt(state: Any, agent_id: str) -> None:
    """Seed an agent's prompt scope when its custom System Prompt is just enabled.

    Both halves of the System Prompt move into the agent's scope together (D4):
    the editable text fragments and the block layout, each seeded from the current
    effective default scope and independent afterwards. The effective default
    layout is the saved default-scope layout, falling back to the bundled default
    when the default scope owns none. Both seeds preserve an existing agent file,
    so re-enabling never clobbers an already-customized agent scope; text overrides
    are intentionally not copied — the agent inherits block text until it overrides.
    """

    storage = state.runtime.storage
    storage.copy_agent_prompt_fragments(agent_id)
    default_layout = storage.read_block_layout(None) or load_bundled_default_layout()
    storage.seed_agent_block_layout(agent_id, default_layout)


async def _delete_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        async with _agent_reference_lock(state):
            remaining_agents = [
                agent for agent in state.runtime.agents.list() if agent.id != agent_id
            ]
            if not remaining_agents:
                raise RpcError(RPC_ERROR_LAST_AGENT, "cannot delete the last agent")
            try:
                # Identity scope only: a same-named Project Team agent remains
                # independent. The guard makes the idle check and the following
                # archive one atomic boundary against every Run ingress path.
                async with _state_chat_runs(state).agent_admission_guard(agent_id, project_id=None):
                    references = _agent_reference_ids(state, agent_id)
                    if references:
                        raise RpcError(
                            RPC_ERROR_AGENT_IN_USE,
                            (
                                "cannot delete agent referenced by "
                                f"{', '.join(references)}: {agent_id}"
                            ),
                        )
                    await state.runtime.terminal_manager.close_agent_scope(agent_id, None)
                    state.runtime.agents.delete(agent_id)
            except RunAdmissionBlockedError as exc:
                raise RpcError(
                    RPC_ERROR_AGENT_BUSY,
                    f"cannot delete agent with active or queued runs: {agent_id}",
                ) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    result = {
        "agent_id": agent_id,
        "remaining_agents": [_agent_response(state, agent) for agent in remaining_agents],
    }
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    _LOGGER.info("Agent archived (agent=%s)", agent_id)
    return result


def _agent_changes(params: JsonObject, *, blocked: set[str], for_create: bool) -> JsonObject:
    public_fields = {
        "name",
        "model",
        "fallback_models",
        "memory_prompt_mode",
        "temperature",
        "thinking_effort",
        "tool_access",
        "allowed_skills",
        "tools",
        "custom_system_prompt_enabled",
        "compaction_policy",
    }
    if not for_create:
        public_fields.add("current_session_id")
        public_fields.add("workspace")
        public_fields.add("root_project_id")
        public_fields.add("copy_workspace_identity_files")

    rejected_fields = sorted(set(params) - public_fields - blocked)
    if rejected_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported agent fields: {', '.join(rejected_fields)}",
        )

    changes: JsonObject = {}
    for key, value in params.items():
        if key in blocked:
            continue
        changes[key] = _validate_agent_field(key, value)
    return changes


def _ensure_agent_model_connections(state: Any, changes: JsonObject) -> None:
    """Reject agent model / fallback_models pinned to a connection they forbid."""
    models = state.runtime.models
    model_binding = changes.get("model")
    if isinstance(model_binding, str):
        _ensure_model_connection_supported(models, "model", model_binding)
    fallback_models = changes.get("fallback_models")
    if isinstance(fallback_models, list):
        for index, binding in enumerate(fallback_models):
            if isinstance(binding, str):
                _ensure_model_connection_supported(
                    models,
                    f"fallback_models[{index}]",
                    binding,
                )


def _validate_agent_field(key: str, value: Any) -> Any:
    if key == "name":
        if value is not None and not isinstance(value, str):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.name must be a string or null",
            )
        return value
    if key == "workspace":
        if value is not None and not isinstance(value, str):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.workspace must be a string or null",
            )
        return value
    if key == "current_session_id":
        if not isinstance(value, str) or not value:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
        return value
    if key == "root_project_id":
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.root_project_id must be null or a non-empty string",
            )
        return value
    if key == "copy_workspace_identity_files":
        if not isinstance(value, bool):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.copy_workspace_identity_files must be a boolean",
            )
        return value
    if key in {"model", "fallback_models"}:
        if key == "fallback_models":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    f"params.{key} must be a list of strings",
                )
            return value
        if not isinstance(value, str):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a string")
        return value
    if key == "temperature":
        return _validate_temperature(value, allow_none=True)
    if key == "thinking_effort":
        return _validate_thinking_effort(value, allow_none=True)
    if key == "memory_prompt_mode":
        return _validate_memory_prompt_mode(value)
    if key == "tool_access":
        try:
            return normalize_tool_access(value)
        except ValueError as exc:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    if key == "allowed_skills":
        return _validate_string_list(key, value)
    if key == "tools":
        if not isinstance(value, dict):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.tools must be an object")
        bash = value.get(BASH_TOOL_SETTINGS_KEY)
        if bash is not None and not isinstance(bash, dict):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.tools.{BASH_TOOL_SETTINGS_KEY} must be an object",
            )
        if isinstance(bash, dict):
            unsupported_bash = sorted(set(bash) - {BASH_ALLOWED_ENV_KEY})
            if unsupported_bash:
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    f"unsupported tools.{BASH_TOOL_SETTINGS_KEY} fields: "
                    + ", ".join(unsupported_bash),
                )
            if BASH_ALLOWED_ENV_KEY in bash:
                try:
                    bash[BASH_ALLOWED_ENV_KEY] = normalize_env_keys(
                        bash[BASH_ALLOWED_ENV_KEY],
                        field_name=(f"tools.{BASH_TOOL_SETTINGS_KEY}.{BASH_ALLOWED_ENV_KEY}"),
                    )
                except ValueError as error:
                    raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error
        subagent = value.get("subagent")
        if subagent is not None and not isinstance(subagent, dict):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.tools.subagent must be an object",
            )
        if isinstance(subagent, dict) and "allowed_agents" in subagent:
            _validate_string_list(
                "tools.subagent.allowed_agents",
                subagent["allowed_agents"],
            )
        return dict(value)
    if key == "custom_system_prompt_enabled":
        if not isinstance(value, bool):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.custom_system_prompt_enabled must be a boolean",
            )
        return value
    if key == "compaction_policy":
        if value is None:
            return None
        try:
            from core.settings.normalizers import normalize_compaction_policy

            return normalize_compaction_policy(value)
        except Exception as exc:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    raise RpcError(RPC_ERROR_INVALID_REQUEST, f"unsupported agent field: {key}")


def _validate_memory_prompt_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in MEMORY_PROMPT_MODES:
        allowed = ", ".join(repr(item) for item in MEMORY_PROMPT_MODES)
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.memory_prompt_mode must be one of: {allowed}",
        )
    return value


def _validate_temperature(
    value: Any,
    *,
    label: str = "params.temperature",
    allow_none: bool = False,
) -> float | None:
    try:
        return validate_temperature(value, label=label, allow_none=allow_none)
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


def _validate_thinking_effort(
    value: Any,
    *,
    label: str = "params.thinking_effort",
    allow_none: bool = False,
) -> str | None:
    try:
        return validate_thinking_effort(value, label=label, allow_none=allow_none)
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the registered agent RPC handlers."""

    def list_agents(state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _list_agents(state))

    return {
        "agent.list": list_agents,
        "agent.reorder": _reorder_agents,
        "agent.get": _get_agent,
        "agent.create": _create_agent,
        "agent.update": _update_agent,
        "agent.rename": _rename_agent,
        "agent.delete": _delete_agent,
    }
