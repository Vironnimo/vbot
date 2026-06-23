"""Agent and session RPC handlers."""

from __future__ import annotations

from typing import Any, cast

from core.channels import ChannelConfigError, channel_system_reminder
from core.memory import MEMORY_PROMPT_MODES
from core.settings import (
    ALLOWED_THINKING_EFFORTS,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    SettingsValidationError,
    validate_temperature,
    validate_thinking_effort,
)
from server.events import RESOURCE_KIND_AGENTS, RESOURCE_KIND_SESSIONS
from server.rpc.agent_refs import _agent_reference_ids, _agent_reference_lock
from server.rpc.channel_methods import _channel_config_by_id
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
    _optional_bool,
    _optional_string,
    _required_agent_address,
    _required_string,
)

JsonObject = dict[str, Any]

__all__ = ["ALLOWED_THINKING_EFFORTS", "MAX_TEMPERATURE", "MIN_TEMPERATURE"]


def _list_agents(state: Any) -> JsonObject:
    try:
        agents = sorted(state.runtime.agents.list(), key=lambda agent: agent.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agents": [_agent_response(state, agent) for agent in agents]}


def _get_agent(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported agent.get fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "id")
    try:
        agent = state.runtime.agents.get(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _agent_response(state, agent)


def _create_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    name = _required_string(params, "name")
    try:
        changes = _agent_changes(params, blocked={"id", "name"}, for_create=True)
        _ensure_agent_model_connections(state, changes)
        state.runtime.agents.create(agent_id, name, **changes)
        if changes.get("custom_system_prompt_enabled") is True:
            state.runtime.storage.copy_agent_prompt_fragments(agent_id)
        agent = state.runtime.agents.get(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(state, agent)
    # Agent CRUD rides the generic reload-on-change channel ("one app system"):
    # the signal carries no agent data, open windows re-fetch agent.list.
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    return response


def _update_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        changes = _agent_changes(params, blocked={"id"}, for_create=False)
        _ensure_agent_model_connections(state, changes)
        previous_agent = state.runtime.agents.get(agent_id)
        if (
            changes.get("custom_system_prompt_enabled") is True
            and not previous_agent.custom_system_prompt_enabled
        ):
            state.runtime.storage.copy_agent_prompt_fragments(agent_id)
        agent = state.runtime.agents.update(agent_id, **changes)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(state, agent)
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    return response


async def _delete_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        async with _agent_reference_lock(state):
            remaining_agents = [
                agent for agent in state.runtime.agents.list() if agent.id != agent_id
            ]
            if not remaining_agents:
                raise RpcError(RPC_ERROR_LAST_AGENT, "cannot delete the last agent")
            if _state_chat_runs(state).has_activity_for_agent(agent_id):
                raise RpcError(
                    RPC_ERROR_AGENT_BUSY,
                    f"cannot delete agent with active or queued runs: {agent_id}",
                )
            references = _agent_reference_ids(state, agent_id)
            if references:
                raise RpcError(
                    RPC_ERROR_AGENT_IN_USE,
                    f"cannot delete agent referenced by {', '.join(references)}: {agent_id}",
                )
            state.runtime.agents.delete(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    result = {
        "agent_id": agent_id,
        "remaining_agents": [_agent_response(state, agent) for agent in remaining_agents],
    }
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    return result


def _create_session(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    make_current = _optional_bool(params, "make_current", default=False)
    try:
        # One resolver seam validates both sources: identity agents through the
        # store, project agents through the team scan. The session is then created
        # under the matching anchor (identity dir vs. project anchor).
        state.runtime.agent_resolver.resolve_agent(project_id, agent_id)
        session = state.runtime.chat_sessions.create(
            agent_id, session_id=session_id, project_id=project_id
        )
        # ``current_session_id`` lives on the identity ``agent.json``; a project
        # config agent has no such pointer (the anchor owns project-session
        # selection), so the make-current update is identity-only.
        if make_current and project_id is None:
            state.runtime.agents.update(agent_id, current_session_id=session.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Session creation is the single emit point for the sessions channel: it also
    # covers /new and /handoff, which create their session through here. Other
    # windows refresh their session list (and the make-current marking) for this
    # agent; they do NOT switch to the new session. Scoped to the agent so windows
    # on a different agent ignore it.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    return {"agent_id": agent_id, "session_id": session.id}


def _list_sessions(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported session.list fields: {', '.join(unsupported_fields)}",
        )

    agent_id, project_id = _required_agent_address(params, "agent_id")
    try:
        sessions = state.runtime.chat_sessions.list_with_metadata(agent_id, project_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"sessions": sessions}


async def _link_session_to_channel(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"agent_id", "session_id", "channel_id", "platform_conv_id"}
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported session.link_channel fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    channel_id = _required_string(params, "channel_id")
    platform_conv_id = _required_string(params, "platform_conv_id")

    try:
        channel_service = state.runtime.channel_service
        channel_config = _channel_config_by_id(channel_service, channel_id)
        if channel_config.agent_id != agent_id:
            raise ChannelConfigError(
                f"Channel {channel_id} belongs to agent {channel_config.agent_id}, not {agent_id}"
            )
        session = state.runtime.chat_sessions.get(agent_id, session_id)
        metadata = dict(state.runtime.chat_sessions.get_metadata(agent_id, session_id))
        metadata.update(
            {
                "source_channel_id": channel_id,
                "platform": channel_config.platform,
                "platform_conv_id": platform_conv_id,
                "last_reply_target": {
                    "channel_id": channel_id,
                    "platform_target": platform_conv_id,
                },
            }
        )
        state.runtime.chat_sessions.set_metadata(agent_id, session_id, metadata)
        # Serialize the channel-link note against any open tool cycle on this
        # session (a Run via another accessor) so it cannot split that cycle.
        async with state.runtime.chat_sessions.write_lock(agent_id, session_id):
            session.add_note(
                channel_system_reminder(
                    platform_display_name=channel_config.platform.capitalize(),
                    channel_id=channel_id,
                    chat_id=platform_conv_id,
                )
            )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _rename_session(state: Any, params: JsonObject) -> JsonObject:
    """Set or clear a session's display title (the WebUI rename and ``/rename``).

    Thin over the single titling seam ``chat_sessions.set_title``: an empty (or
    absent) title clears it, so the session reverts to its automatic display.
    The response carries the stored title (``None`` when cleared) so the caller
    can confirm what was applied after normalization.
    """
    supported_fields = {"agent_id", "session_id", "title"}
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported session.rename fields: {', '.join(unsupported_fields)}",
        )

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    title = _session_title_param(params)
    try:
        stored_title = state.runtime.chat_sessions.set_title(
            agent_id, session_id, title, project_id
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # A rename changes the session's list display, so other windows on this agent
    # refresh their session list — scoped to the agent like session.create.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    return {"agent_id": agent_id, "session_id": session_id, "title": stored_title}


def _session_title_param(params: JsonObject) -> str:
    """Read the rename title: any string, empty allowed (an empty title clears).

    Unlike ``_required_string``/``_optional_string`` this accepts the empty
    string, which is the explicit "clear the title" signal; an absent field is
    treated the same way.
    """
    value = params.get("title", "")
    if not isinstance(value, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.title must be a string")
    return value


def _agent_changes(params: JsonObject, *, blocked: set[str], for_create: bool) -> JsonObject:
    public_fields = {
        "name",
        "model",
        "fallback_model",
        "memory_prompt_mode",
        "temperature",
        "thinking_effort",
        "allowed_tools",
        "allowed_skills",
        "custom_system_prompt_enabled",
    }
    if not for_create:
        public_fields.add("current_session_id")
        public_fields.add("workspace")

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
    """Reject agent model / fallback_model pinned to a connection they forbid."""
    models = state.runtime.models
    for field in ("model", "fallback_model"):
        if field in changes:
            _ensure_model_connection_supported(models, field, changes[field])


def _validate_agent_field(key: str, value: Any) -> Any:
    if key in {"name", "current_session_id", "workspace"}:
        if not isinstance(value, str) or not value:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
        return value
    if key in {"model", "fallback_model"}:
        if not isinstance(value, str):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a string")
        return value
    if key == "temperature":
        return _validate_temperature(value, allow_none=True)
    if key == "thinking_effort":
        return _validate_thinking_effort(value, allow_none=True)
    if key == "memory_prompt_mode":
        return _validate_memory_prompt_mode(value)
    if key in {"allowed_tools", "allowed_skills"}:
        return _validate_string_list(key, value)
    if key == "custom_system_prompt_enabled":
        if not isinstance(value, bool):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.custom_system_prompt_enabled must be a boolean",
            )
        return value
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


def _validate_string_list(key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a list of strings")
    return list(value)


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return agent and session RPC handlers."""

    def list_agents(state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _list_agents(state))

    return {
        "agent.list": list_agents,
        "agent.get": _get_agent,
        "agent.create": _create_agent,
        "agent.update": _update_agent,
        "agent.delete": _delete_agent,
        "session.create": _create_session,
        "session.list": _list_sessions,
        "session.rename": _rename_session,
        "session.link_channel": _link_session_to_channel,
    }
