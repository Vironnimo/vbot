"""Bind the Extension service catalog to selected canonical application APIs.

This is an allowlist, not a second RPC dispatcher. Domain handlers keep their
validation, mutation locks, public projections and change publications. Private
control, authentication, credentials, approvals and raw Settings are absent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any

from core.extensions.services import ExtensionService, ExtensionServiceError, ServiceKind
from server.access import bound_principal
from server.events import ALLOWED_RESOURCE_KINDS
from server.rpc.dispatcher import RpcMethodHandler, dispatch_method
from server.rpc.errors import RpcError

type JsonObject = dict[str, Any]

_STRING = {"type": "string"}
_BOOL = {"type": "boolean"}
_OBJECT = {"type": "object"}
_ADDRESS = {"agent_id": _STRING, "session_id": _STRING}
_SERVICE_INVOCATION: ContextVar[bool] = ContextVar("extension_service_invocation", default=False)


def is_extension_service_call() -> bool:
    """Identify nonhuman integration ingress without trusting request parameters."""
    return _SERVICE_INVOCATION.get()


def _parameters(
    properties: JsonObject | None = None,
    required: tuple[str, ...] = (),
    *,
    owner_fields: bool = False,
) -> JsonObject:
    # Complex editors deliberately retain the owning API's complete evolving
    # field surface. Their handler is the validator, not a duplicated schema.
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": owner_fields,
    }


# Each entry names one operation intentionally offered to installed Extensions.
# The schema describes the addressing/control envelope; nested domain documents
# and owner_fields entries are validated fully by the canonical handler.
SERVICE_DEFINITIONS: tuple[tuple[str, ServiceKind, JsonObject, tuple[str, ...]], ...] = (
    ("agent.list", "query", _parameters(), ("agents",)),
    ("agent.get", "query", _parameters({"id": _STRING}, ("id",)), ("agents",)),
    (
        "agent.create",
        "execute",
        _parameters({"id": _STRING}, ("id",), owner_fields=True),
        ("agents",),
    ),
    (
        "agent.update",
        "execute",
        _parameters({"id": _STRING}, ("id",), owner_fields=True),
        ("agents",),
    ),
    (
        "agent.rename",
        "execute",
        _parameters({"id": _STRING, "new_id": _STRING}, ("id", "new_id")),
        ("agents", "sessions"),
    ),
    ("project.list", "query", _parameters(owner_fields=True), ("projects",)),
    ("project.show", "query", _parameters({"project_id": _STRING}, ("project_id",)), ("projects",)),
    ("project.add", "execute", _parameters(owner_fields=True), ("projects",)),
    (
        "project.set",
        "execute",
        _parameters({"project_id": _STRING}, ("project_id",), owner_fields=True),
        ("projects",),
    ),
    ("project.set_override", "execute", _parameters(owner_fields=True), ("projects",)),
    ("model.list", "query", _parameters(owner_fields=True), ("models", "providers")),
    ("model.get", "query", _parameters({"model": _STRING}, ("model",)), ("models", "providers")),
    ("model.favorites", "query", _parameters(), ("model_favorites",)),
    (
        "model.set_favorite",
        "execute",
        _parameters({"model": _STRING, "favorite": _BOOL}, ("model", "favorite")),
        ("model_favorites",),
    ),
    ("tool.list", "query", _parameters(), ("extensions", "settings")),
    ("skill.list", "query", _parameters(), ("skills",)),
    ("chat.commands", "query", _parameters(owner_fields=True), ("commands", "skills")),
    ("session.list", "query", _parameters(owner_fields=True), ("sessions",)),
    ("session.get", "query", _parameters(_ADDRESS, ("agent_id", "session_id")), ("sessions",)),
    ("session.show", "query", _parameters(owner_fields=True), ("sessions", "runs")),
    ("session.change_stats", "query", _parameters(owner_fields=True), ("sessions",)),
    (
        "session.create",
        "execute",
        _parameters({"agent_id": _STRING}, ("agent_id",), owner_fields=True),
        ("sessions",),
    ),
    (
        "session.rename",
        "execute",
        _parameters({**_ADDRESS, "title": _STRING}, ("agent_id", "session_id")),
        ("sessions",),
    ),
    (
        "session.set_pinned",
        "execute",
        _parameters({**_ADDRESS, "pinned": _BOOL}, ("agent_id", "session_id", "pinned")),
        ("sessions",),
    ),
    (
        "session.set_agent_overrides",
        "execute",
        _parameters(
            {**_ADDRESS, "agent_overrides": _OBJECT, "remember": _BOOL},
            ("agent_id", "session_id", "agent_overrides"),
        ),
        ("sessions", "agents", "projects"),
    ),
    (
        "session.set_fast_mode",
        "execute",
        _parameters({**_ADDRESS, "enabled": _BOOL}, ("agent_id", "session_id", "enabled")),
        ("sessions",),
    ),
    (
        "session.set_compaction_policy",
        "execute",
        _parameters(
            {**_ADDRESS, "policy": {"type": ["object", "null"]}}, ("agent_id", "session_id")
        ),
        ("sessions",),
    ),
    ("session.fork", "execute", _parameters(owner_fields=True), ("sessions",)),
    ("chat.history", "query", _parameters(owner_fields=True), ("sessions", "runs")),
    ("chat.timeline", "query", _parameters(owner_fields=True), ("sessions", "runs")),
    ("chat.timeline_children", "query", _parameters(owner_fields=True), ("sessions", "runs")),
    ("chat.tool_call", "query", _parameters(owner_fields=True), ("sessions", "runs")),
    ("chat.run_result", "query", _parameters(owner_fields=True), ("runs",)),
    ("chat.work", "query", _parameters(_ADDRESS, ("agent_id", "session_id")), ("runs", "queue")),
    ("chat.queue_list", "query", _parameters(owner_fields=True), ("queue",)),
    (
        "chat.send",
        "execute",
        {
            **_parameters(
                {**_ADDRESS, "content": {"type": ["string", "array"]}},
                ("agent_id", "session_id", "content"),
            ),
            "description": "Start a nonhuman system Run in an existing idle Session and await "
            "its result. Slash Commands are refused. No durable input_id or automatic replay.",
        },
        ("sessions", "runs", "queue"),
    ),
    (
        "chat.stream",
        "execute",
        {
            **_parameters(
                {**_ADDRESS, "content": {"type": ["string", "array"]}},
                ("agent_id", "session_id", "content"),
            ),
            "description": "Start a nonhuman system Run in an existing idle Session and return "
            "its Run id. Slash Commands are refused. No durable input_id or automatic replay.",
        },
        ("sessions", "runs", "queue"),
    ),
    (
        "chat.cancel",
        "execute",
        _parameters({"run_id": _STRING, "reason": _STRING}, ("run_id",)),
        ("runs",),
    ),
    (
        "chat.stop_all",
        "execute",
        _parameters(_ADDRESS, ("agent_id", "session_id")),
        ("runs", "queue"),
    ),
    ("chat.queue_steer", "execute", _parameters(owner_fields=True), ("queue", "runs")),
    ("chat.queue_remove", "execute", _parameters(owner_fields=True), ("queue",)),
    ("settings.values", "query", _parameters(), ("settings",)),
    (
        "settings.catalog",
        "query",
        _parameters(owner_fields=True),
        ("settings", "extensions", "providers"),
    ),
    (
        "settings.get_path",
        "query",
        _parameters({"path": _STRING, "allow_missing": _BOOL}, ("path",)),
        ("settings",),
    ),
    (
        "settings.patch",
        "execute",
        _parameters({"operations": {"type": "array", "items": _OBJECT}}, ("operations",)),
        ("settings",),
    ),
)


def bind_application_services(
    state: Any, handlers: Mapping[str, RpcMethodHandler]
) -> Callable[[], None]:
    """Publish audited handlers before Extension startup on the serving loop."""
    catalog = state.runtime.extension_services

    def handler_for(name: str) -> Callable[[JsonObject], Any]:
        async def invoke(params: JsonObject) -> JsonObject:
            token = _SERVICE_INVOCATION.set(True)
            try:
                # Background and nested Extension calls cannot impersonate the
                # HTTP request or Device whose task happened to invoke them.
                with bound_principal(None):
                    result = await dispatch_method(state, name, params, handlers)
            except RpcError as error:
                raise ExtensionServiceError(error.code, error.message, error.data) from error
            finally:
                _SERVICE_INVOCATION.reset(token)
            return result

        return invoke

    services = []
    for name, kind, parameters, resources in SERVICE_DEFINITIONS:
        if name not in handlers:
            raise ValueError(f"Application service has no public handler: {name}")
        services.append(ExtensionService(name, kind, parameters, handler_for(name), resources))
    state.extension_resource_observer = catalog.publish
    unbind = catalog.bind(services, resources=ALLOWED_RESOURCE_KINDS)

    def close() -> None:
        state.extension_resource_observer = None
        unbind()

    return close
