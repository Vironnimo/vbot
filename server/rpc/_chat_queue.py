"""Public Chat Queue handlers and scoped invalidation."""

from __future__ import annotations

from typing import Any

from core.chat import (
    ChatLoop,
    queue_content_is_editable,
)
from core.runs import ChatRunManager, QueuedRunItem
from core.sessions import (
    SessionAddress,
)
from server.events import RESOURCE_KIND_QUEUE
from server.rpc._chat_input import _expand_content_file_mentions
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_QUEUE_ITEM_NOT_FOUND,
    RPC_ERROR_QUEUE_ITEM_STEERING,
    RpcError,
)
from server.rpc.event_bridge import (
    publish_resource_changed,
    session_resource_scope,
)
from server.rpc.validation import (
    _chat_reply_surface,
    _optional_chat_input_origin,
    _optional_file_mentions,
    _parse_chat_content,
    _reject_unsupported,
    _required_session_address,
    _required_string,
)

JsonObject = dict[str, Any]


def _publish_queue_changed(state: Any, address: SessionAddress) -> None:
    """Signal that one session's queue changed so other windows reload it live.

    Scoped to the complete Session address so windows on a different Session
    or Project ignore it. Only the browser/RPC send surface
    emits this — core enqueues (automation, channels, sub-agents) deliberately
    do not, keeping the chat core untouched; those windows still catch up on the
    next terminal event.
    """
    publish_resource_changed(
        state,
        RESOURCE_KIND_QUEUE,
        scope=session_resource_scope(address),
    )


def _chat_queue_list(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "session_id"}, "chat.queue_list")

    address = _required_session_address(params)
    chat_runs: ChatRunManager = state.chat_runs
    try:
        items = [
            item
            for item in chat_runs.list_queued(
                address.agent_id, address.session_id, project_id=address.project_id
            )
            if not item.internal
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"items": [item.to_dict() for item in items]}


def _chat_queue_steer(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "session_id", "item_id"}, "chat.queue_steer")
    address = _required_session_address(params)
    item_id = _required_string(params, "item_id")
    try:
        manager: ChatRunManager = state.chat_runs
        if _public_queue_item(manager, address, item_id) is None:
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        item = manager.steer_queued(
            address.agent_id, address.session_id, item_id, project_id=address.project_id
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    _publish_queue_changed(state, address)
    return {"item": item.to_dict()}


def _chat_queue_remove(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "session_id", "item_id"}, "chat.queue_remove")

    address = _required_session_address(params)
    item_id = _required_string(params, "item_id")
    try:
        chat_runs: ChatRunManager = state.chat_runs
        queued_item = _public_queue_item(chat_runs, address, item_id)
        if queued_item is None:
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        if queued_item.steering_in_flight:
            raise RpcError(
                RPC_ERROR_QUEUE_ITEM_STEERING,
                "queued item is being delivered into the running Run and can no longer be removed",
            )
        removed = chat_runs.remove_queued(
            address.agent_id, address.session_id, item_id, project_id=address.project_id
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not removed:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    _publish_queue_changed(state, address)
    return {"ok": True}


async def _chat_queue_update(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params,
        {
            "agent_id",
            "session_id",
            "item_id",
            "content",
            "input_origin",
            "file_mentions",
            "reply_surface",
        },
        "chat.queue_update",
    )

    address = _required_session_address(params)
    item_id = _required_string(params, "item_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)
    reply_surface = _chat_reply_surface(params) if "reply_surface" in params else None
    # An edit replaces the queued content wholesale, so mentions are re-expanded
    # against the edited text — a fresh snapshot at edit time.
    content = await _expand_content_file_mentions(
        state, address, content, _optional_file_mentions(params)
    )

    try:
        chat_runs: ChatRunManager = state.chat_runs
        queued_item = _public_queue_item(chat_runs, address, item_id)
        if queued_item is None:
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        if queued_item.steering:
            raise RpcError(
                RPC_ERROR_QUEUE_ITEM_STEERING,
                "queued item is selected for steering and can no longer be edited",
            )
        if not queued_item.editable:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "queued item content cannot be edited losslessly",
            )

        # The address's project is the anchor the item was queued under — the queue
        # key carries it, and the item above was found via that key. Rebuild against
        # the same anchor; otherwise a project session is looked up in the identity
        # anchor and the rebuild fails with session-not-found.
        chat_loop: ChatLoop = state.chat_loop
        (
            resolved_session_id,
            updated_executor,
            updated_display_content,
        ) = await chat_loop.build_queue_update(
            address.agent_id,
            address.session_id,
            content,
            queued_item,
            input_origin=input_origin,
            project_id=address.project_id,
            reply_surface=reply_surface,
        )
        updated = chat_runs.update_queued(
            address.agent_id,
            resolved_session_id,
            item_id,
            updated_executor,
            updated_display_content,
            project_id=address.project_id,
            editable=queue_content_is_editable(content),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not updated:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    # Scope on the resolved session id — content can move an item to a different
    # session, and that resolved id (not the raw input) is what was mutated.
    _publish_queue_changed(
        state, SessionAddress(address.project_id, address.agent_id, resolved_session_id)
    )
    return {"ok": True}


def _public_queue_item(
    chat_runs: ChatRunManager,
    address: SessionAddress,
    item_id: str,
) -> QueuedRunItem | None:
    """Return the queued item if it exists and is public (not internal), else ``None``.

    Internal items (e.g. subagent-driven) stay hidden from the queue RPCs, so they are
    treated as absent here just like a missing id.
    """
    for item in chat_runs.list_queued(
        address.agent_id, address.session_id, project_id=address.project_id
    ):
        if item.item_id == item_id:
            return item if not item.internal else None
    return None
