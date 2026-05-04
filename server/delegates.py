"""RPC dispatcher and transport-only delegates for server commands."""

from __future__ import annotations

from typing import Any

from core.chat import (
    ActiveRunError,
    ChatError,
    ChatMessage,
    ChatSessionError,
    Run,
    RunCancelledError,
    RunError,
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
