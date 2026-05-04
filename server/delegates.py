"""RPC dispatcher and transport-only delegates for server commands."""

from __future__ import annotations

import asyncio
from typing import Any

from core.agents import AgentError
from core.chat import (
    ASSISTANT_OUTPUT_EVENT,
    REASONING_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_STARTED_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    USER_MESSAGE_EVENT,
    ActiveRunError,
    ChatError,
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
        case "agent.list":
            return _list_agents(state)
        case "agent.create":
            return _create_agent(state, params)
        case "agent.update":
            return _update_agent(state, params)
        case "agent.delete":
            return _delete_agent(state, params)
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
        case _:
            raise RpcError(RPC_ERROR_METHOD_NOT_FOUND, f"unknown RPC method: {method}")


def _list_agents(state: Any) -> JsonObject:
    try:
        agents = sorted(state.runtime.agents.list(), key=lambda agent: agent.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agents": [_agent_response(agent) for agent in agents]}


def _create_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    name = _required_string(params, "name")
    try:
        agent = state.runtime.agents.create(
            agent_id, name, **_agent_changes(params, blocked={"id", "name"})
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _agent_response(agent)


def _update_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        agent = state.runtime.agents.update(agent_id, **_agent_changes(params, blocked={"id"}))
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _agent_response(agent)


def _delete_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        remaining_agents = [agent for agent in state.runtime.agents.list() if agent.id != agent_id]
        if not remaining_agents:
            raise RpcError(RPC_ERROR_LAST_AGENT, "cannot delete the last agent")
        state.runtime.agents.delete(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "agent_id": agent_id,
        "remaining_agents": [_agent_response(agent) for agent in remaining_agents],
    }


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
        run = await state.chat_loop.start_run(agent_id, content, session_id=session_id)
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


def _required_string(params: JsonObject, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
    return value


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


def _agent_changes(params: JsonObject, *, blocked: set[str]) -> JsonObject:
    mutable_fields = {
        "name",
        "model",
        "fallback_model",
        "workspace",
        "temperature",
        "thinking_effort",
        "allowed_tools",
        "allowed_skills",
        "current_session_id",
    }
    return {
        key: value for key, value in params.items() if key in mutable_fields and key not in blocked
    }


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
        "events": [event.to_dict() for event in run.events],
    }
    if final_message is not None:
        response["message"] = _visible_message(final_message)
    if sse_url is not None:
        response["sse_url"] = sse_url
    return response


def _visible_message(message: ChatMessage) -> JsonObject:
    payload = message.to_dict()
    payload.pop("reasoning_meta", None)
    return payload


def _agent_response(agent: Any) -> JsonObject:
    return {
        "id": agent.id,
        "name": agent.name,
        "model": agent.model,
        "fallback_model": agent.fallback_model,
        "workspace": agent.workspace,
        "temperature": agent.temperature,
        "thinking_effort": agent.thinking_effort,
        "allowed_tools": list(agent.allowed_tools),
        "allowed_skills": list(agent.allowed_skills),
        "current_session_id": agent.current_session_id,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def _bridge_run_to_event_bus(state: Any, run: Run) -> None:
    event_bus = getattr(state, "event_bus", None)
    if event_bus is None:
        return
    asyncio.create_task(_publish_run_events(event_bus, run))


async def _publish_run_events(event_bus: Any, run: Run) -> None:
    async for event in run.subscribe():
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
