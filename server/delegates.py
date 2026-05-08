"""RPC dispatcher and transport-only delegates for server commands."""

from __future__ import annotations

import asyncio
import math
from typing import Any, cast

from core.agents import AgentError
from core.chat import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    REASONING_DELTA_EVENT,
    REASONING_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_STARTED_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    USER_MESSAGE_EVENT,
    ActiveRunError,
    ChatError,
    ChatLoop,
    ChatMessage,
    ChatSessionError,
    Run,
    RunCancelledError,
    RunError,
    RunEvent,
    RunNotFoundError,
)
from core.utils.errors import ConfigError, VBotError

JsonObject = dict[str, Any]

ALLOWED_THINKING_EFFORTS = {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0

RPC_ERROR_INVALID_REQUEST = "invalid_request"
RPC_ERROR_METHOD_NOT_FOUND = "method_not_found"
RPC_ERROR_DOMAIN = "domain_error"
RPC_ERROR_ACTIVE_RUN = "active_run"
RPC_ERROR_RUN_NOT_FOUND = "run_not_found"
RPC_ERROR_CANCELLED = "run_cancelled"
RPC_ERROR_LAST_AGENT = "last_agent"


class RpcError(Exception):
    """Expected RPC request or domain error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> JsonObject:
        """Return the provider-agnostic error envelope payload."""
        return {"code": self.code, "message": self.message}


async def dispatch_rpc(state: Any, request: Any) -> JsonObject:
    """Dispatch one JSON-RPC-like vBot server request."""
    try:
        method, params = _parse_rpc_request(request)
        result = await _dispatch_method(state, method, params)
    except RpcError as exc:
        return {"ok": False, "error": exc.to_dict()}
    return {"ok": True, "result": result}


async def _dispatch_method(state: Any, method: str, params: JsonObject) -> JsonObject:
    match method:
        case "model.list":
            return _list_models(state, params)
        case "tool.list":
            return _list_tools(state, params)
        case "agent.list":
            return _list_agents(state)
        case "agent.create":
            return _create_agent(state, params)
        case "agent.update":
            return _update_agent(state, params)
        case "agent.delete":
            return await _delete_agent(state, params)
        case "session.create":
            return _create_session(state, params)
        case "chat.history":
            return _chat_history(state, params)
        case "chat.send":
            return await _send_chat(state, params)
        case "chat.stream":
            return await _stream_chat(state, params)
        case "chat.cancel":
            return await _cancel_chat(state, params)
        case "settings.get":
            return _get_settings(state, params)
        case "settings.update":
            return _update_settings(state, params)
        case _:
            raise RpcError(RPC_ERROR_METHOD_NOT_FOUND, f"unknown RPC method: {method}")


def _list_agents(state: Any) -> JsonObject:
    try:
        agents = sorted(state.runtime.agents.list(), key=lambda agent: agent.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agents": [_agent_response(agent) for agent in agents]}


def _list_models(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "model.list does not accept params")
    try:
        runtime = state.runtime
        models = sorted(
            (
                _model_response(provider_id, model)
                for provider_id in runtime.providers.list_ids()
                if _provider_has_credentials(runtime, provider_id)
                for model in runtime.models.list_for_provider(provider_id)
            ),
            key=lambda model: (model["provider_id"], model["model_id"]),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"models": models}


def _list_tools(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "tool.list does not accept params")
    try:
        tools = state.runtime.tools.list_tools()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"tools": [_tool_response(tool) for tool in tools]}


def _create_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    name = _required_string(params, "name")
    try:
        agent = state.runtime.agents.create(
            agent_id, name, **_agent_changes(params, blocked={"id", "name"}, for_create=True)
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(agent)
    _publish_agent_event(state, "agent.created", response)
    return response


def _update_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        agent = state.runtime.agents.update(
            agent_id, **_agent_changes(params, blocked={"id"}, for_create=False)
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(agent)
    _publish_agent_event(state, "agent.updated", response)
    return response


async def _delete_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        async with state.agent_delete_lock:
            remaining_agents = [
                agent for agent in state.runtime.agents.list() if agent.id != agent_id
            ]
            if not remaining_agents:
                raise RpcError(RPC_ERROR_LAST_AGENT, "cannot delete the last agent")
            state.runtime.agents.delete(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    result = {
        "agent_id": agent_id,
        "remaining_agents": [_agent_response(agent) for agent in remaining_agents],
    }
    _publish_agent_event(state, "agent.deleted", result)
    return result


def _create_session(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    make_current = _optional_bool(params, "make_current", default=False)
    try:
        state.runtime.agents.get(agent_id)
        session = state.runtime.chat_sessions.create(agent_id, session_id=session_id)
        if make_current:
            state.runtime.agents.update(agent_id, current_session_id=session.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agent_id": agent_id, "session_id": session.id}


def _chat_history(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    try:
        agent = state.runtime.agents.get(agent_id)
        active_session_id = session_id or agent.current_session_id
        session = state.runtime.chat_sessions.get(agent_id, active_session_id)
        messages = [_visible_message(message) for message in session.load()]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agent_id": agent_id, "session_id": active_session_id, "messages": messages}


async def _send_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _required_string(params, "content")
    try:
        run = await state.chat_loop.start_run(agent_id, content, session_id=session_id)
        _bridge_run_to_event_bus(state, run)
        assistant_message = await run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, final_message=assistant_message)


async def _stream_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _required_string(params, "content")
    try:
        streaming_chat_loop = _streaming_chat_loop(state)
        run = await streaming_chat_loop.start_run(agent_id, content, session_id=session_id)
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, sse_url=f"/api/runs/{run.id}/events")


async def _cancel_chat(state: Any, params: JsonObject) -> JsonObject:
    run_id = _required_string(params, "run_id")
    try:
        run = await state.chat_runs.cancel(run_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run)


def _get_settings(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "settings.get does not accept params")
    try:
        return _settings_response(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _update_settings(state: Any, params: JsonObject) -> JsonObject:
    appearance = _parse_settings_update(params)
    try:
        state.runtime.storage.update_appearance_settings(appearance)
        return _settings_response(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _parse_rpc_request(request: Any) -> tuple[str, JsonObject]:
    if not isinstance(request, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "RPC request must be a JSON object")
    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "RPC method must be a non-empty string")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "RPC params must be an object")
    return method, params


def _parse_settings_update(params: JsonObject) -> JsonObject:
    supported_sections = {"appearance"}
    unsupported_sections = sorted(set(params) - supported_sections)
    if unsupported_sections:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported settings sections: {', '.join(unsupported_sections)}",
        )

    appearance = params.get("appearance")
    if not isinstance(appearance, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.appearance must be an object")

    unsupported_fields = sorted(set(appearance) - {"language"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported appearance settings: {', '.join(unsupported_fields)}",
        )

    language = appearance.get("language")
    if not isinstance(language, str) or not language:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.appearance.language must be a non-empty string",
        )

    return {"language": language}


def _required_string(params: JsonObject, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
    return value


def _settings_response(state: Any) -> JsonObject:
    runtime = state.runtime
    appearance = runtime.storage.load_appearance_settings()
    server_bind = _server_bind_response(state)

    return {
        "general": {
            "server": server_bind,
            "data_directory": str(runtime.storage.data_dir),
        },
        "providers": {
            "items": [
                _provider_settings_item(runtime, provider_id)
                for provider_id in runtime.providers.list_ids()
            ],
            "custom_endpoints": {
                "supported": False,
                "items": [],
            },
        },
        "appearance": {
            "language": appearance["language"],
            "available_languages": runtime.storage.supported_appearance_languages(),
        },
    }


def _server_bind_response(state: Any) -> JsonObject:
    server_bind = getattr(state, "server_bind", {})
    listen_host = server_bind.get("listen_host", "127.0.0.1")
    listen_port = server_bind.get("listen_port", 8420)
    port_source = server_bind.get("port_source", "default")
    return {
        "listen_host": listen_host,
        "listen_port": listen_port,
        "port_source": port_source,
    }


def _provider_settings_item(runtime: Any, provider_id: str) -> JsonObject:
    provider = runtime.providers.get(provider_id)
    credential_key = provider.auth.credential_key
    credentials_configured = _provider_has_credentials(runtime, provider_id)
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "credential_key": credential_key,
        "credentials_configured": credentials_configured,
        "status": "configured" if credentials_configured else "missing_credentials",
        "model_count": len(runtime.models.list_for_provider(provider_id)),
        "kind": "remote" if provider.base_url else "local",
        "editable": False,
    }


def _provider_has_credentials(runtime: Any, provider_id: str) -> bool:
    return bool(runtime.has_provider_credentials(provider_id))


def _optional_string(params: JsonObject, key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
    return value


def _optional_bool(params: JsonObject, key: str, *, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a boolean")
    return value


def _agent_changes(params: JsonObject, *, blocked: set[str], for_create: bool) -> JsonObject:
    public_fields = {
        "name",
        "model",
        "fallback_model",
        "temperature",
        "thinking_effort",
        "allowed_tools",
        "allowed_skills",
    }
    if not for_create:
        public_fields.add("current_session_id")

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


def _validate_agent_field(key: str, value: Any) -> Any:
    if key in {"name", "current_session_id"}:
        if not isinstance(value, str) or not value:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
        return value
    if key in {"model", "fallback_model"}:
        if not isinstance(value, str):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a string")
        return value
    if key == "temperature":
        return _validate_temperature(value)
    if key == "thinking_effort":
        return _validate_thinking_effort(value)
    if key in {"allowed_tools", "allowed_skills"}:
        return _validate_string_list(key, value)
    raise RpcError(RPC_ERROR_INVALID_REQUEST, f"unsupported agent field: {key}")


def _validate_temperature(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.temperature must be a number")
    temperature = float(value)
    if not math.isfinite(temperature):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.temperature must be finite")
    if temperature < MIN_TEMPERATURE or temperature > MAX_TEMPERATURE:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.temperature must be between {MIN_TEMPERATURE:g} and {MAX_TEMPERATURE:g}",
        )
    return temperature


def _validate_thinking_effort(value: Any) -> str:
    if not isinstance(value, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.thinking_effort must be a string")
    if value not in ALLOWED_THINKING_EFFORTS:
        allowed = ", ".join(repr(item) for item in sorted(ALLOWED_THINKING_EFFORTS))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.thinking_effort must be one of: {allowed}",
        )
    return value


def _validate_string_list(key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a list of strings")
    return list(value)


def _map_expected_error(error: Exception) -> RpcError:
    if isinstance(error, RpcError):
        return error
    if isinstance(error, ActiveRunError):
        return RpcError(RPC_ERROR_ACTIVE_RUN, str(error))
    if isinstance(error, RunNotFoundError):
        return RpcError(RPC_ERROR_RUN_NOT_FOUND, str(error))
    if isinstance(error, RunCancelledError):
        return RpcError(RPC_ERROR_CANCELLED, str(error))
    if isinstance(
        error, (AgentError, ChatError, ChatSessionError, ConfigError, RunError, VBotError, KeyError)
    ):
        return RpcError(RPC_ERROR_DOMAIN, str(error))
    raise error


def _run_response(
    run: Run,
    *,
    final_message: ChatMessage | None = None,
    sse_url: str | None = None,
) -> JsonObject:
    response: JsonObject = {
        "run_id": run.id,
        "agent_id": run.agent_id,
        "session_id": run.session_id,
        "status": run.status.value,
        "events": [_remove_opaque_provider_metadata(event.to_dict()) for event in run.events],
    }
    if final_message is not None:
        response["message"] = _visible_message(final_message)
    if sse_url is not None:
        response["sse_url"] = sse_url
    return response


def _visible_message(message: ChatMessage) -> JsonObject:
    return cast(JsonObject, _remove_opaque_provider_metadata(message.to_dict()))


def _agent_response(agent: Any) -> JsonObject:
    return {
        "id": agent.id,
        "name": agent.name,
        "model": agent.model,
        "fallback_model": agent.fallback_model,
        "connection": agent.connection,
        "fallback_connection": agent.fallback_connection,
        "workspace": agent.workspace,
        "temperature": agent.temperature,
        "thinking_effort": agent.thinking_effort,
        "allowed_tools": list(agent.allowed_tools),
        "allowed_skills": list(agent.allowed_skills),
        "current_session_id": agent.current_session_id,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def _model_response(provider_id: str, model: Any) -> JsonObject:
    return {
        "id": f"{provider_id}/{model.model_id}",
        "provider_id": provider_id,
        "model_id": model.model_id,
        "name": model.name,
        "capabilities": {
            "vision": model.capabilities.vision,
            "tools": model.capabilities.tools,
            "json_mode": model.capabilities.json_mode,
            "reasoning": {
                "supported": model.capabilities.reasoning.supported,
            },
        },
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
    }


def _tool_response(tool: Any) -> JsonObject:
    return {
        "name": tool.name,
        "description": tool.description,
    }


def _bridge_run_to_event_bus(state: Any, run: Run) -> None:
    event_bus = getattr(state, "event_bus", None)
    if event_bus is None:
        return
    asyncio.create_task(_publish_run_events(event_bus, run))


def _streaming_chat_loop(state: Any) -> Any:
    chat_loop = getattr(state, "streaming_chat_loop", None)
    if chat_loop is not None:
        return chat_loop
    chat_loop = ChatLoop(state.runtime, streaming=True)
    state.streaming_chat_loop = chat_loop
    return chat_loop


def _publish_agent_event(state: Any, event_type: str, payload: JsonObject) -> None:
    """Publish an agent CRUD event to the server event bus if available."""
    event_bus = getattr(state, "event_bus", None)
    if event_bus is None:
        return
    event_bus.publish(event_type, payload)


async def _publish_run_events(event_bus: Any, run: Run) -> None:
    async for event in run.subscribe():
        if event.type in RUN_DELTA_EVENT_TYPES:
            continue
        summary = _server_event_from_run_event(event)
        event_bus.publish(summary["type"], summary["payload"])


def _server_event_from_run_event(event: RunEvent) -> JsonObject:
    payload: JsonObject = {
        "run_id": event.run_id,
        "agent_id": event.agent_id,
        "session_id": event.session_id,
        "run_event_type": event.type,
        "run_event_sequence": event.sequence,
        "run_event_timestamp": event.timestamp,
    }
    if event.type in RUN_OUTPUT_EVENT_TYPES:
        payload["output"] = _remove_opaque_provider_metadata(event.payload)
    if event.type in RUN_TERMINAL_EVENT_TYPES:
        payload["status"] = event.payload.get("status")
    return {"type": SERVER_EVENT_TYPES.get(event.type, "run_output"), "payload": payload}


def _remove_opaque_provider_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_opaque_provider_metadata(item)
            for key, item in value.items()
            if key != "reasoning_meta"
        }
    if isinstance(value, list):
        return [_remove_opaque_provider_metadata(item) for item in value]
    return value


RUN_OUTPUT_EVENT_TYPES = {
    USER_MESSAGE_EVENT,
    REASONING_EVENT,
    TOOL_CALL_STARTED_EVENT,
    TOOL_CALL_RESULT_EVENT,
    ASSISTANT_OUTPUT_EVENT,
}
RUN_DELTA_EVENT_TYPES = {
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
}
RUN_TERMINAL_EVENT_TYPES = {RUN_COMPLETED_EVENT, RUN_CANCELLED_EVENT, RUN_FAILED_EVENT}
SERVER_EVENT_TYPES = {
    RUN_STARTED_EVENT: "run_started",
    USER_MESSAGE_EVENT: "run_output",
    REASONING_EVENT: "run_output",
    TOOL_CALL_STARTED_EVENT: "run_output",
    TOOL_CALL_RESULT_EVENT: "run_output",
    ASSISTANT_OUTPUT_EVENT: "run_output",
    RUN_COMPLETED_EVENT: "run_completed",
    RUN_CANCELLED_EVENT: "run_cancelled",
    RUN_FAILED_EVENT: "run_failed",
}
