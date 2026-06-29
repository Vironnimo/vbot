"""Domain-indexed RPC method registry."""

from __future__ import annotations

from typing import Any

from server.rpc import (
    agent_methods,
    automation_methods,
    catalog_methods,
    channel_methods,
    chat_methods,
    client_methods,
    connection_methods,
    debug_methods,
    extensions_methods,
    operations_methods,
    project_methods,
    provider_usage_methods,
    settings_methods,
    skill_methods,
    statistics_methods,
)
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.dispatcher import dispatch_rpc as _dispatch_rpc_envelope

JsonObject = dict[str, Any]


def build_method_handlers() -> dict[str, RpcMethodHandler]:
    """Build the complete RPC method table from domain registries."""

    handlers: dict[str, RpcMethodHandler] = {}
    for registry in (
        connection_methods,
        catalog_methods,
        agent_methods,
        chat_methods,
        channel_methods,
        automation_methods,
        project_methods,
        settings_methods,
        extensions_methods,
        operations_methods,
        debug_methods,
        statistics_methods,
        provider_usage_methods,
        client_methods,
        skill_methods,
    ):
        handlers.update(registry.method_handlers())
    return handlers


METHODS = build_method_handlers()


async def dispatch_rpc(state: object, request: object) -> JsonObject:
    """Dispatch one JSON-RPC-like vBot server request against the method table."""

    return await _dispatch_rpc_envelope(state, request, METHODS)
