"""Domain-indexed RPC method registry."""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Any

from server.rpc import (
    access_methods,
    activity_methods,
    agent_methods,
    agent_secret_methods,
    application_methods,
    archive_methods,
    automation_methods,
    calendar_methods,
    catalog_methods,
    channel_methods,
    chat_methods,
    client_methods,
    computer_methods,
    connection_methods,
    data_store_methods,
    debug_methods,
    extensions_methods,
    external_history_methods,
    filesystem_methods,
    input_methods,
    learning_methods,
    librarian_methods,
    live_methods,
    managed_service_methods,
    memory_methods,
    model_methods,
    operations_methods,
    performance_methods,
    permission_methods,
    portal_methods,
    project_methods,
    provider_usage_methods,
    recall_methods,
    secret_methods,
    session_methods,
    settings_methods,
    setup_methods,
    skill_methods,
    statistics_methods,
    task_model_methods,
    terminal_methods,
)
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.dispatcher import dispatch_rpc as _dispatch_rpc_envelope
from server.rpc.validation import _reject_unsupported

if TYPE_CHECKING:
    from server.rpc._contracts import RpcParameterContract

JsonObject = dict[str, Any]


def build_method_handlers() -> dict[str, RpcMethodHandler]:
    """Build the complete RPC method table from domain registries."""

    handlers: dict[str, RpcMethodHandler] = {}
    for registry in (
        application_methods,
        connection_methods,
        model_methods,
        catalog_methods,
        filesystem_methods,
        agent_methods,
        session_methods,
        external_history_methods,
        archive_methods,
        memory_methods,
        learning_methods,
        chat_methods,
        channel_methods,
        automation_methods,
        librarian_methods,
        input_methods,
        calendar_methods,
        project_methods,
        settings_methods,
        setup_methods,
        task_model_methods,
        data_store_methods,
        secret_methods,
        agent_secret_methods,
        extensions_methods,
        live_methods,
        managed_service_methods,
        operations_methods,
        debug_methods,
        performance_methods,
        permission_methods,
        statistics_methods,
        provider_usage_methods,
        recall_methods,
        client_methods,
        computer_methods,
        skill_methods,
        terminal_methods,
        portal_methods,
        activity_methods,
        access_methods,
    ):
        handlers.update(registry.method_handlers())
    from server.rpc._contracts import PARAMETER_CONTRACTS

    return {
        name: _with_parameter_contract(handler, PARAMETER_CONTRACTS[name])
        if name in PARAMETER_CONTRACTS
        else handler
        for name, handler in handlers.items()
    }


def _with_parameter_contract(
    handler: RpcMethodHandler, contract: RpcParameterContract
) -> RpcMethodHandler:
    """Apply the shared wire envelope before any domain or mutation admission."""

    @wraps(handler)
    def validated(state: Any, params: JsonObject) -> Any:
        _reject_unsupported(params, contract.request_fields, contract.name)
        return handler(state, params)

    # Audit the actual registered callable, including wrappers around domain
    # locks and workers. A source parser cannot prove this for delegated RPCs.
    validated.__rpc_parameter_contract__ = contract  # type: ignore[attr-defined]
    return validated


METHODS = build_method_handlers()


async def dispatch_rpc(state: object, request: object) -> JsonObject:
    """Dispatch one JSON-RPC-like vBot server request against the method table."""

    return await _dispatch_rpc_envelope(state, request, METHODS)
