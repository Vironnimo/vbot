"""RPC dispatcher and transport-only delegates for server commands."""

from __future__ import annotations

import asyncio
from typing import Any

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
        case "session.create":
            return _create_session(state, params)
        case "chat.send":
            return await _send_chat(state, params)
        case "chat.stream":
            return await _stream_chat(state, params)
        case "chat.cancel":
            return await _cancel_chat(state, params)
        case _:
            raise RpcError(RPC_ERROR_METHOD_NOT_FOUND, f"unknown RPC method: {method}")


def _create_session(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    try:
        state.runtime.agents.get(agent_id)
        session = state.runtime.chat_sessions.create(agent_id, session_id=session_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agent_id": agent_id, "session_id": session.id}


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


def _map_expected_error(error: Exception) -> RpcError:
    if isinstance(error, RpcError):
        return error
    if isinstance(error, ActiveRunError):
        return RpcError(RPC_ERROR_ACTIVE_RUN, str(error))
    if isinstance(error, RunNotFoundError):
        return RpcError(RPC_ERROR_RUN_NOT_FOUND, str(error))
    if isinstance(error, RunCancelledError):
        return RpcError(RPC_ERROR_CANCELLED, str(error))
    if isinstance(error, (ChatError, ChatSessionError, ConfigError, RunError, VBotError, KeyError)):
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
