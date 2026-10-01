"""Recall semantic index RPC handlers.

``recall.status`` returns the state of background document embedding for
semantic Recall: whether the selected backend ranks by meaning, the indexer
state, the embedding model, coverage counts, the last failure, spent usage and
an estimate for what still waits. ``recall.rebuild_index`` drops the vectors
of the current embedding space and queues every Passage again; it returns the
status after the reset. Both read the index on its worker pool, off the Event
Loop. Status changes are also pushed as the ``recall_index_status`` event.
"""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]


async def _status(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "recall.status")
    try:
        status = await state.runtime.recall.index_status()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return dict(status.to_dict())


async def _rebuild_index(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "recall.rebuild_index")
    try:
        status = await state.runtime.recall.rebuild_index()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return dict(status.to_dict())


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the Recall RPC handlers."""

    return {
        "recall.status": _status,
        "recall.rebuild_index": _rebuild_index,
    }
