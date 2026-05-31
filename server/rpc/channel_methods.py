"""Channel RPC method registry."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler


def method_handlers(delegates: Any) -> dict[str, RpcMethodHandler]:
    """Return channel RPC handlers from the delegates facade."""

    return {
        "channel.list": delegates._list_channels,
        "channel.create": delegates._create_channel,
        "channel.update": delegates._update_channel,
        "channel.delete": delegates._delete_channel,
        "channel.enable": delegates._enable_channel,
        "channel.disable": delegates._disable_channel,
        "channel.status": delegates._channel_status,
    }
