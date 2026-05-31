"""Chat RPC method registry."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler


def method_handlers(delegates: Any) -> dict[str, RpcMethodHandler]:
    """Return chat RPC handlers from the delegates facade."""

    return {
        "chat.history": delegates._chat_history,
        "chat.send": delegates._send_chat,
        "chat.stream": delegates._stream_chat,
        "chat.retry_last_turn": delegates._retry_chat,
        "chat.cancel": delegates._cancel_chat,
        "chat.queue_list": delegates._chat_queue_list,
        "chat.queue_remove": delegates._chat_queue_remove,
        "chat.queue_update": delegates._chat_queue_update,
    }
