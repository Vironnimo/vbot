"""Bind the Extension service catalog to selected canonical application APIs.

This is an allowlist, not a second RPC dispatcher. Domain handlers keep their
validation, mutation locks, public projections and change publications. Private
control, authentication, credentials, approvals and raw Settings are absent.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from core.extensions.services import ExtensionService, ExtensionServiceError
from server._extension_service_contracts import content, conversation, system
from server._extension_service_contracts.common import ServiceDefinition
from server.access import bound_principal
from server.events import ALLOWED_RESOURCE_KINDS
from server.rpc.dispatcher import RpcMethodHandler, dispatch_method
from server.rpc.errors import RpcError
from server.rpc.event_bridge import publish_resource_changed

type JsonObject = dict[str, Any]

_SERVICE_INVOCATION: ContextVar[bool] = ContextVar("extension_service_invocation", default=False)


def is_extension_service_call() -> bool:
    """Identify nonhuman integration ingress without trusting request parameters."""
    return _SERVICE_INVOCATION.get()


SERVICE_DEFINITIONS: tuple[ServiceDefinition, ...] = (
    *conversation.SERVICE_DEFINITIONS,
    *content.SERVICE_DEFINITIONS,
    *system.SERVICE_DEFINITIONS,
)
SERVICE_EXCLUSIONS: dict[str, str] = {
    **conversation.SERVICE_EXCLUSIONS,
    **content.SERVICE_EXCLUSIONS,
    **system.SERVICE_EXCLUSIONS,
}


def audit_application_services(handlers: Mapping[str, RpcMethodHandler]) -> None:
    """Refuse unreviewed RPC additions and stale/overlapping catalog decisions.

    This intentionally compares against the canonical method registry, never
    infers exposure, kind or schemas from a method name or handler signature.
    """
    names = [entry[0] for entry in SERVICE_DEFINITIONS]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate application service declaration")
    if set(names) & SERVICE_EXCLUSIONS.keys():
        raise ValueError("Application service is both exposed and excluded")
    classified = set(names) | SERVICE_EXCLUSIONS.keys()
    missing = sorted(handlers.keys() - classified)
    stale = sorted(classified - handlers.keys())
    if missing or stale:
        raise ValueError(
            "Application service audit differs from RPC registry: "
            f"unreviewed={missing}, stale={stale}"
        )


@contextmanager
def extension_service_context() -> Iterator[None]:
    """Make both builtin and exported Service calls explicitly nonhuman."""
    token = _SERVICE_INVOCATION.set(True)
    try:
        with bound_principal(None):
            yield
    finally:
        _SERVICE_INVOCATION.reset(token)


def bind_application_services(
    state: Any, handlers: Mapping[str, RpcMethodHandler]
) -> Callable[[], None]:
    """Publish audited handlers before Extension startup on the serving loop."""
    catalog = state.runtime.extension_services
    # A supplied test registry may contain only the public catalog, but an
    # unknown method must never silently acquire or miss an exposure decision.
    unreviewed = (
        handlers.keys() - {entry[0] for entry in SERVICE_DEFINITIONS} - SERVICE_EXCLUSIONS.keys()
    )
    if unreviewed:
        raise ValueError(f"Unreviewed application RPC methods: {sorted(unreviewed)}")

    def handler_for(name: str) -> Callable[[JsonObject], Any]:
        async def invoke(params: JsonObject) -> JsonObject:
            try:
                # Background and nested Extension calls cannot impersonate the
                # HTTP request or Device whose task happened to invoke them.
                with extension_service_context():
                    result = await dispatch_method(state, name, params, handlers)
            except RpcError as error:
                raise ExtensionServiceError(error.code, error.message, error.data) from error
            return result

        return invoke

    services = []
    for name, kind, parameters, resources in SERVICE_DEFINITIONS:
        if name not in handlers:
            raise ValueError(f"Application service has no public handler: {name}")
        services.append(ExtensionService(name, kind, parameters, handler_for(name), resources))
    state.extension_resource_observer = catalog.publish
    unbind = catalog.bind(
        services,
        resources=ALLOWED_RESOURCE_KINDS,
        exclusions=SERVICE_EXCLUSIONS,
        export_context=extension_service_context,
        publish_export=lambda _resource, _scope: publish_resource_changed(state, "extensions"),
    )

    def close() -> None:
        state.extension_resource_observer = None
        unbind()

    return close
