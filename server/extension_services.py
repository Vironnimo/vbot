"""Bind the Extension service catalog to selected canonical application APIs.

This is an allowlist, not a second RPC dispatcher. Domain handlers keep their
validation, mutation locks, public projections and change publications. Trusted
Extensions can update structured Settings and supply write-only Channel tokens
through their canonical configuration operations. Raw Settings access, global
credential management, credential reads, authentication and human approvals are
excluded; these Services never acquire a human or Device identity.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from core.extensions.services import ExtensionService, ExtensionServiceError
from core.settings.paths import parse_settings_path
from server.access import bound_principal
from server.events import ALLOWED_RESOURCE_KINDS, EXTENSION_RESOURCE_CHANGED_EVENT
from server.rpc._contracts import PARAMETER_CONTRACTS, SERVICE_DEFINITIONS, SERVICE_EXCLUSIONS
from server.rpc.dispatcher import RpcMethodHandler, dispatch_method
from server.rpc.errors import RpcError

type JsonObject = dict[str, Any]

_SERVICE_INVOCATION: ContextVar[bool] = ContextVar("extension_service_invocation", default=False)


def is_extension_service_call() -> bool:
    """Identify nonhuman integration ingress without trusting request parameters."""
    return _SERVICE_INVOCATION.get()


def _check_settings_authority(params: JsonObject) -> None:
    # Parse through the canonical owner: bracket spelling cannot bypass the
    # same restrictions advertised by the structured update schema.
    protected = (
        ("defaults", "agent", "permission_rules"),
        ("permission_profiles",),
        ("defaults", "agent", "tool_access"),
        ("providers", "claude_agent", "executable_path"),
    )
    for index, operation in enumerate(params["operations"]):
        try:
            path = parse_settings_path(operation["path"]).values
        except ValueError as exc:
            raise ExtensionServiceError("invalid_request", str(exc)) from exc
        if any(
            path[: len(prefix)] == prefix or prefix[: len(path)] == path for prefix in protected
        ):
            raise ExtensionServiceError(
                "operator_required",
                "Permission Rules, Tool access and executable paths require the operator surface",
                {"path": ["operations", index, "path"]},
            )


def audit_application_services(handlers: Mapping[str, RpcMethodHandler]) -> None:
    """Refuse unreviewed RPC additions and stale/overlapping catalog decisions.

    This intentionally compares against the canonical method registry, never
    infers exposure, kind or schemas from a method name or handler signature.
    """
    from server.rpc.methods import _ParameterContractHandler

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
    uncoupled = sorted(
        name
        for name in names
        if type(handler := handlers[name]) is not _ParameterContractHandler
        or handler.contract is not PARAMETER_CONTRACTS[name]
    )
    if uncoupled:
        raise ValueError(f"Application services lack their canonical RPC field gate: {uncoupled}")


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
    audit_application_services(handlers)

    def handler_for(name: str) -> Callable[[JsonObject], Any]:
        async def invoke(params: JsonObject) -> JsonObject:
            try:
                if name == "settings.patch":
                    _check_settings_authority(params)
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
        # The catalog contract test meta-validates these shipped RPC declarations.
        # Extension exports and ordinary catalog bindings validate at registration.
        static_application_schemas=True,
        resources=ALLOWED_RESOURCE_KINDS,
        exclusions=SERVICE_EXCLUSIONS,
        export_context=extension_service_context,
        publish_export=lambda event: state.event_bus.publish(
            EXTENSION_RESOURCE_CHANGED_EVENT, event
        ),
    )

    def close() -> None:
        state.extension_resource_observer = None
        unbind()

    return close
