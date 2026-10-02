"""Background activity RPCs: the list Settings shows and dismissing its entries."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]


def _activity_list(state: Any, params: JsonObject) -> JsonObject:
    """The current background activity (``server/activity.py``)."""
    _reject_unsupported(params, set(), "activity.list")
    return {"activities": state.activity.activities}


def _activity_dismiss(state: Any, params: JsonObject) -> JsonObject:
    """Hide a finished, failed or restart entry for every client until its work runs again."""
    _reject_unsupported(params, {"id"}, "activity.dismiss")
    activity_id = params.get("id")
    if not isinstance(activity_id, str) or not activity_id:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "id must be an activity id")
    return {"activities": state.activity.dismiss(activity_id)}


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {
        "activity.list": _activity_list,
        "activity.dismiss": _activity_dismiss,
    }
