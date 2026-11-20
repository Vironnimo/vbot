"""Desktop Voice ingress before an unmatched command enters ordinary Chat."""

from __future__ import annotations

from typing import Any

from core.runtime.wakeword import WakewordResult, execute_wakeword
from core.sessions import SESSION_ID_PATTERN
from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling
from server.events import RESOURCE_KIND_AGENTS
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed, publish_session_changed
from server.rpc.session_methods import _create_session
from server.rpc.validation import (
    _optional_string,
    _reject_unsupported,
    _required_agent_address,
    _required_string,
)

_LOGGER = get_logger("server.rpc.wakeword")


async def _record_reminder(
    state: Any, result: WakewordResult, agent_address: str, project_id: str | None
) -> None:
    assert result.reminder is not None
    if (session := result.session) is not None:
        async with state.runtime.chat_sessions.write_lock(session.address):
            await session.append_many_async(
                [result.reminder], expected_generation_id=session.generation_id
            )
        publish_session_changed(state, session.address)
    else:
        async with _agent_reference_lock(state):
            await _create_session(
                state,
                {"agent_id": agent_address, "make_current": project_id is None},
                initial_messages=[result.reminder],
                expected_agent_created_at=result.agent_created_at,
            )
            if project_id is None:
                publish_resource_changed(
                    state, RESOURCE_KIND_AGENTS, scope={"agent_id": agent_address}
                )


async def _wakeword_command(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unsupported(params, {"agent_id", "session_id", "content"}, "voice.wakeword_command")
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    if session_id is not None and not SESSION_ID_PATTERN.fullmatch(session_id):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.session_id must be a valid Session id")
    text = _required_string(params, "content")

    async def perform() -> dict[str, Any]:
        result = await execute_wakeword(state.runtime, text, agent_id, project_id, session_id)
        if result is None:
            return {"handled": False}
        reply = {"handled": True, "ok": result.ok, "message": result.message}
        if result.reminder is not None:
            try:
                await _record_reminder(state, result, params["agent_id"], project_id)
            except Exception as error:
                # The Tool already settled. A failed note cannot authorize another attempt.
                _LOGGER.warning("Wakeword reminder delivery failed (%s)", type(error).__name__)
                reply["notification_failed"] = True
        return reply

    try:
        return await settle_before_cancelling(perform())
    except Exception as error:
        raise _map_expected_error(error) from error


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {"voice.wakeword_command": _wakeword_command}
