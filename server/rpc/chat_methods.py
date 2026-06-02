"""Chat RPC handlers."""

from __future__ import annotations

from typing import Any

from core.chat import CommandAction, CommandHandled
from core.chat.content_blocks import ContentBlock, TextBlock
from core.runs import ActiveRunError, ChatRunManager
from server.rpc.agent_methods import _create_session
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RPC_ERROR_QUEUE_ITEM_NOT_FOUND, RpcError
from server.rpc.event_bridge import _bridge_queued_item_to_event_bus, _bridge_run_to_event_bus
from server.rpc.payloads import (
    _is_visible_history_message,
    _queued_response,
    _run_response,
    _visible_message,
)
from server.rpc.runtime_access import (
    _build_streaming_queue_update,
    _state_chat_runs,
    _state_command_dispatcher,
    _streaming_chat_loop,
)
from server.rpc.validation import (
    _optional_chat_input_origin,
    _optional_positive_integer,
    _optional_string,
    _parse_chat_content,
    _required_string,
)

JsonObject = dict[str, Any]
MAX_CHAT_HISTORY_LIMIT = 500


def _chat_history(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"agent_id", "session_id", "limit", "before"}
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.history fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    limit = _optional_positive_integer(params, "limit", max_value=MAX_CHAT_HISTORY_LIMIT)
    before = _optional_string(params, "before")
    try:
        agent = state.runtime.agents.get(agent_id)
        active_session_id = session_id or agent.current_session_id
        session = state.runtime.chat_sessions.get(agent_id, active_session_id)
        visible_messages = [
            _visible_message(message)
            for message in session.load()
            if _is_visible_history_message(message)
        ]
        messages, has_more = _history_page(visible_messages, limit=limit, before=before)
        active_run = _active_run_response(state, agent_id, active_session_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response: JsonObject = {
        "agent_id": agent_id,
        "session_id": active_session_id,
        "messages": messages,
        "has_more": has_more,
    }
    if active_run is not None:
        response["active_run"] = active_run
    return response


def _history_page(
    messages: list[JsonObject], *, limit: int | None, before: str | None
) -> tuple[list[JsonObject], bool]:
    page_source = messages
    if before is not None:
        before_index = next(
            (index for index, message in enumerate(messages) if message.get("id") == before),
            None,
        )
        if before_index is None:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.before must reference a message id")
        page_source = messages[:before_index]

    if limit is None:
        return list(page_source), False

    page = page_source[-limit:]
    return page, len(page_source) > len(page)


def _extract_command_text(content: str | list[ContentBlock]) -> str | None:
    if isinstance(content, str):
        return content

    if len(content) != 1:
        return None

    block = content[0]
    if isinstance(block, TextBlock):
        return block.text
    return None


def _command_handled_response(result: CommandHandled | str | None) -> JsonObject:
    if isinstance(result, CommandHandled):
        reply = result.reply
        data = result.data
    else:
        reply = result
        data = None

    response: JsonObject = {
        "command_handled": True,
        "reply": reply or "",
    }
    if data:
        response["data"] = dict(data)
    return response


async def _dispatch_chat_command(
    state: Any,
    agent_id: str,
    session_id: str,
    command_text: str,
    *,
    streaming: bool,
) -> JsonObject | None:
    try:
        command_result = _state_command_dispatcher(state).dispatch(
            agent_id,
            session_id,
            command_text,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if isinstance(command_result, CommandHandled):
        return _command_handled_response(command_result)
    if isinstance(command_result, CommandAction):
        return await _handle_command_action(
            state,
            agent_id,
            session_id,
            command_result,
            streaming=streaming,
        )
    return None


async def _handle_command_action(
    state: Any,
    agent_id: str,
    session_id: str,
    command_action: CommandAction,
    *,
    streaming: bool,
) -> JsonObject:
    match command_action.name:
        case "compact":
            return await _handle_compact_command(state, agent_id, session_id)
        case "new_session":
            return _handle_new_session_command(state, agent_id, session_id)
        case "retry_last_turn":
            return await _retry_chat_for_ids(state, agent_id, session_id, streaming=streaming)
    raise AssertionError(f"unsupported command action: {command_action.name}")


def _handle_new_session_command(state: Any, agent_id: str, session_id: str) -> JsonObject:
    try:
        active_run = _state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)
        if active_run is not None:
            return _command_handled_response(
                "A new session can be started after the current run finishes.",
            )

        response = _create_session(
            state,
            {"agent_id": agent_id, "make_current": True},
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    new_session_id = _required_string(response, "session_id")
    return _command_handled_response(
        CommandHandled(
            reply=f"New session started: {new_session_id}",
            data={"command": "new", "session_id": new_session_id},
        )
    )


async def _send_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)

    command_text = _extract_command_text(content)
    if command_text is not None:
        command_response = await _dispatch_chat_command(
            state,
            agent_id,
            session_id,
            command_text,
            streaming=False,
        )
        if command_response is not None:
            return command_response

    try:
        if input_origin is None:
            run = await state.chat_loop.start_run(agent_id, content, session_id=session_id)
        else:
            run = await state.chat_loop.start_run(
                agent_id,
                content,
                session_id=session_id,
                input_origin=input_origin,
            )
    except ActiveRunError:
        try:
            if input_origin is None:
                queued_item = await state.chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                )
            else:
                queued_item = await state.chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    input_origin=input_origin,
                )
            _bridge_queued_item_to_event_bus(state, queued_item)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return _queued_response(queued_item)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        _bridge_run_to_event_bus(state, run)
        assistant_message = await run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, final_message=assistant_message)


async def _stream_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)

    command_text = _extract_command_text(content)
    if command_text is not None:
        command_response = await _dispatch_chat_command(
            state,
            agent_id,
            session_id,
            command_text,
            streaming=True,
        )
        if command_response is not None:
            return command_response

    streaming_chat_loop = _streaming_chat_loop(state)
    try:
        if input_origin is None:
            run = await streaming_chat_loop.start_run(agent_id, content, session_id=session_id)
        else:
            run = await streaming_chat_loop.start_run(
                agent_id,
                content,
                session_id=session_id,
                input_origin=input_origin,
            )
    except ActiveRunError:
        try:
            if input_origin is None:
                queued_item = await streaming_chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                )
            else:
                queued_item = await streaming_chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    input_origin=input_origin,
                )
            _bridge_queued_item_to_event_bus(state, queued_item)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return _queued_response(queued_item)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, sse_url=f"/api/runs/{run.id}/events")


async def _handle_compact_command(state: Any, agent_id: str, session_id: str) -> JsonObject:
    try:
        reply = await state.runtime.trigger_service.compact_session(agent_id, session_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _command_handled_response(reply)


async def _retry_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    return await _retry_chat_for_ids(state, agent_id, session_id, streaming=True)


async def _retry_chat_for_ids(
    state: Any,
    agent_id: str,
    session_id: str,
    *,
    streaming: bool,
) -> JsonObject:
    try:
        chat_loop = _streaming_chat_loop(state) if streaming else state.chat_loop
        run = await chat_loop.retry_run(agent_id, session_id)
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if streaming:
        return _run_response(run, sse_url=f"/api/runs/{run.id}/events")

    try:
        assistant_message = await run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, final_message=assistant_message)


async def _cancel_chat(state: Any, params: JsonObject) -> JsonObject:
    run_id = _required_string(params, "run_id")
    try:
        run = await state.chat_runs.cancel(run_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run)


def _chat_queue_list(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "session_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_list fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    try:
        items = [
            item
            for item in _state_chat_runs(state).list_queued(agent_id, session_id)
            if not item.internal
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"items": [item.to_dict() for item in items]}


def _chat_queue_remove(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "session_id", "item_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_remove fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    try:
        chat_runs = _state_chat_runs(state)
        if not _queue_item_is_public(chat_runs, agent_id, session_id, item_id):
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        removed = chat_runs.remove_queued(agent_id, session_id, item_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not removed:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    return {"ok": True}


def _chat_queue_update(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(
        set(params) - {"agent_id", "session_id", "item_id", "content", "input_origin"}
    )
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_update fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)

    try:
        chat_runs = _state_chat_runs(state)
        if not _queue_item_is_public(chat_runs, agent_id, session_id, item_id):
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")

        if input_origin is None:
            (
                resolved_session_id,
                updated_executor,
                updated_display_content,
            ) = _build_streaming_queue_update(state, agent_id, session_id, content)
        else:
            (
                resolved_session_id,
                updated_executor,
                updated_display_content,
            ) = _build_streaming_queue_update(
                state,
                agent_id,
                session_id,
                content,
                input_origin=input_origin,
            )
        updated = chat_runs.update_queued(
            agent_id,
            resolved_session_id,
            item_id,
            updated_executor,
            updated_display_content,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not updated:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    return {"ok": True}


def _active_run_response(state: Any, agent_id: str, session_id: str) -> JsonObject | None:
    run = _state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)
    if run is None:
        return None
    return _run_response(run, sse_url=f"/api/runs/{run.id}/events")


def _queue_item_is_public(
    chat_runs: ChatRunManager,
    agent_id: str,
    session_id: str,
    item_id: str,
) -> bool:
    for item in chat_runs.list_queued(agent_id, session_id):
        if item.item_id == item_id:
            return not item.internal
    return False


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return chat RPC handlers."""

    return {
        "chat.history": _chat_history,
        "chat.send": _send_chat,
        "chat.stream": _stream_chat,
        "chat.retry_last_turn": _retry_chat,
        "chat.cancel": _cancel_chat,
        "chat.queue_list": _chat_queue_list,
        "chat.queue_remove": _chat_queue_remove,
        "chat.queue_update": _chat_queue_update,
    }
