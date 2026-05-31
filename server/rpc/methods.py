"""Domain-indexed RPC method registry."""

from __future__ import annotations

from typing import Any

from server.rpc import (
    agent_methods,
    automation_methods,
    catalog_methods,
    channel_methods,
    chat_methods,
    connection_methods,
    operations_methods,
    settings_methods,
)
from server.rpc.dispatcher import RpcMethodHandler


def build_method_handlers(delegates: Any) -> dict[str, RpcMethodHandler]:
    """Build the complete RPC method table from domain registries."""

    handlers: dict[str, RpcMethodHandler] = {}
    for registry in (
        connection_methods,
        catalog_methods,
        agent_methods,
        chat_methods,
        channel_methods,
        automation_methods,
        settings_methods,
        operations_methods,
    ):
        handlers.update(registry.method_handlers(delegates))
    return handlers
