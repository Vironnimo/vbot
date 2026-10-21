"""RPCs for Agent Secrets, Secret requests and Secret Sources.

Values enter through ``create``, ``update`` and ``answer`` and leave the server
only through ``reveal``, the user's explicit action; no other result, error
message or log carries one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from core.chat.secret_giving import give_secret
from core.secrets.agent_secrets import (
    LIFETIMES,
    SECRET_KINDS,
    SINK_CLASSES,
    AgentSecret,
    AgentSecrets,
    SecretRecordError,
    SecretUseError,
    SourceBinding,
)
from core.secrets.sources import SecretSource
from core.sessions import SessionAddress
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_AGENT_SECRETS
from server.rpc._mutations import serialized_mutation, serialized_read
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]

_LOGGER = logging.getLogger("vbot.server.rpc.agent_secret_methods")
# Store writes and source calls block; one worker keeps them in order.
_WORKERS = BoundedWorkerPool(name="agent-secrets-rpc", max_workers=1)
_LOCK = "_agent_secrets_mutation_lock"
_UPDATE_FIELDS = {
    "id",
    "values",
    "description",
    "concealed",
    "agents",
    "sinks",
    "expires_at",
    "clear_expiry",
    "permanent",
}
_GIVE_FIELDS = {
    "project_id",
    "agent_id",
    "session_id",
    "name",
    "kind",
    "values",
    "lifetime",
    "expires_at",
    "description",
}
_CREATE_FIELDS = {
    "name",
    "kind",
    "values",
    "lifetime",
    "session_id",
    "expires_at",
    "agents",
    "description",
    "concealed",
    "sinks",
    "source",
    "source_reference",
    "source_fields",
}


def _invalid(message: str) -> RpcError:
    return RpcError(RPC_ERROR_INVALID_REQUEST, message)


def _text(params: JsonObject, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"params.{key} must be a non-empty string")
    return value.strip()


def _optional_text(params: JsonObject, key: str) -> str | None:
    return None if params.get(key) is None else _text(params, key)


def _strings(params: JsonObject, key: str) -> list[str] | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _invalid(f"params.{key} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _values(params: JsonObject, key: str = "values") -> dict[str, str] | None:
    """Field values; a refusal names the field, never a value."""
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping) or not all(
        isinstance(name, str) and isinstance(item, str) for name, item in value.items()
    ):
        raise _invalid(f"params.{key} must map field names to strings")
    return dict(value)


def _flags(params: JsonObject, key: str) -> dict[str, bool] | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping) or not all(
        isinstance(name, str) and isinstance(item, bool) for name, item in value.items()
    ):
        raise _invalid(f"params.{key} must map field names to true or false")
    return dict(value)


def _time(params: JsonObject, key: str) -> datetime | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"params.{key} must be an ISO 8601 time with a time zone")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid(f"params.{key} must be an ISO 8601 time with a time zone") from None
    if parsed.tzinfo is None:
        raise _invalid(f"params.{key} must name its time zone")
    return parsed


def _choice(params: JsonObject, key: str, choices: tuple[str, ...], default: str | None) -> Any:
    value = params.get(key, default)
    if value not in choices:
        raise _invalid(f"params.{key} must be one of: {', '.join(choices)}")
    return value


def _flag(params: JsonObject, key: str) -> bool:
    value = params.get(key, False)
    if not isinstance(value, bool):
        raise _invalid(f"params.{key} must be true or false")
    return value


def _secrets(state: Any) -> AgentSecrets:
    secrets: AgentSecrets = state.runtime.agent_secrets
    return secrets


def _describe(secrets: AgentSecrets, secret: AgentSecret) -> JsonObject:
    described = secrets.describe(secret)
    described["recent_uses"] = [
        {
            "field": use.field,
            "sink": use.sink,
            "agent_id": use.agent_id,
            "session_id": use.session_id,
            "at": use.at.isoformat(),
        }
        for use in secrets.recent_uses(secret.id)
    ]
    return described


def _source_payload(secrets: AgentSecrets, source: SecretSource) -> JsonObject:
    status = secrets.source_status(source.name)
    return {
        "name": source.name,
        "label": source.label,
        "state": status.state,
        "reason": status.reason,
        "capabilities": sorted(source.capabilities),
        "unlock_fields": [
            {
                "name": item.name,
                "label": item.label,
                "concealed": item.concealed,
                "required": item.required,
            }
            for item in source.unlock_fields
        ],
    }


def _overview(state: Any) -> JsonObject:
    secrets = _secrets(state)
    secrets.sweep()
    return {
        "secrets": [_describe(secrets, secret) for secret in secrets.all()],
        "sources": [_source_payload(secrets, source) for source in secrets.sources()],
        "requests": state.runtime.secret_requests.list(),
        "kinds": list(SECRET_KINDS),
        "lifetimes": list(LIFETIMES),
        "sinks": list(SINK_CLASSES),
    }


async def _list(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "agent_secrets.list")
    return await _run(_overview, state)


def _create_secret(state: Any, params: JsonObject) -> JsonObject:
    secrets = _secrets(state)
    source_name = _optional_text(params, "source")
    binding = None
    if source_name is not None:
        reference = _text(params, "source_reference")
        if secrets.source(source_name) is None:
            raise _invalid(f"{source_name} is not an available Secret Source")
        binding = SourceBinding(source_name, reference)
    secret = secrets.create(
        name=_text(params, "name"),
        kind=_choice(params, "kind", SECRET_KINDS, None),
        values=_values(params) or {},
        actor="user",
        lifetime=_choice(params, "lifetime", LIFETIMES, "permanent"),
        session_id=_optional_text(params, "session_id"),
        expires_at=_time(params, "expires_at"),
        agents=_strings(params, "agents") or (),
        description=params.get("description") or "",
        concealed=_flags(params, "concealed"),
        sinks=_strings(params, "sinks"),
        source=binding,
        source_fields=_values(params, "source_fields"),
    )
    return {"secret": _describe(secrets, secret)}


async def _create(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _CREATE_FIELDS, "agent_secrets.create")
    return await _run(_create_secret, state, params)


def _update_secret(state: Any, params: JsonObject) -> JsonObject:
    secrets = _secrets(state)
    description = params.get("description")
    if description is not None and not isinstance(description, str):
        raise _invalid("params.description must be a string")
    secret = secrets.update(
        _text(params, "id"),
        actor="user",
        values=_values(params),
        description=description,
        concealed=_flags(params, "concealed"),
        agents=_strings(params, "agents"),
        sinks=_strings(params, "sinks"),
        expires_at=_time(params, "expires_at"),
        clear_expiry=_flag(params, "clear_expiry"),
        permanent=_flag(params, "permanent"),
    )
    return {"secret": _describe(secrets, secret)}


async def _update(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _UPDATE_FIELDS, "agent_secrets.update")
    return await _run(_update_secret, state, params)


def _delete_secret(state: Any, params: JsonObject) -> JsonObject:
    secret = _secrets(state).delete(_text(params, "id"), actor="user")
    return {"id": secret.id, "deleted": True}


async def _delete(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "agent_secrets.delete")
    return await _run(_delete_secret, state, params)


def _reveal_value(state: Any, params: JsonObject) -> JsonObject:
    secrets = _secrets(state)
    secret_id = _text(params, "id")
    field_name = _text(params, "field")
    secret = secrets.get(secret_id)
    if secret is None:
        raise _invalid("No Secret with this id.")
    target = secret.field_named(field_name)
    if target is None:
        raise _invalid(f"{secret.name} has no field {field_name}.")
    try:
        # A stored field shows what the user stored (a TOTP seed, not its code).
        value = (
            target.value
            if secret.source is None and target.value is not None
            else (secrets.field_value(secret, target))
        )
    except SecretUseError as error:
        raise _invalid(str(error)) from None
    _LOGGER.info("Agent Secret %s.%s revealed to the user", secret.name, target.name)
    return {"id": secret.id, "field": target.name, "value": value}


async def _reveal(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "field"}, "agent_secrets.reveal")
    return await _run(_reveal_value, state, params)


async def _answer(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "values", "lifetime", "expires_at"}, "agent_secrets.answer")
    requests = state.runtime.secret_requests
    lifetime = params.get("lifetime")
    if lifetime is not None and lifetime not in LIFETIMES:
        raise _invalid(f"params.lifetime must be one of: {', '.join(LIFETIMES)}")
    try:
        secret = await requests.answer(
            _text(params, "id"),
            _values(params) or {},
            lifetime=lifetime,
            expires_at=_time(params, "expires_at"),
        )
    except SecretRecordError as error:
        raise _invalid(str(error)) from None
    return {"secret": _describe(_secrets(state), secret)}


async def _give(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _GIVE_FIELDS, "agent_secrets.give")
    runtime = state.runtime
    description = params.get("description") or ""
    if not isinstance(description, str):
        raise _invalid("params.description must be a string")
    try:
        given = await give_secret(
            secrets=runtime.agent_secrets,
            requests=runtime.secret_requests,
            sessions=runtime.chat_sessions,
            address=SessionAddress(
                _optional_text(params, "project_id"),
                _text(params, "agent_id"),
                _text(params, "session_id"),
            ),
            name=_text(params, "name"),
            kind=_choice(params, "kind", SECRET_KINDS, None),
            values=_values(params) or {},
            lifetime=_choice(params, "lifetime", LIFETIMES, "session"),
            expires_at=_time(params, "expires_at"),
            description=description,
        )
    except SecretRecordError as error:
        raise _invalid(str(error)) from None
    return {"secret": _describe(_secrets(state), given.secret), "replaced": given.replaced}


async def _decline(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "agent_secrets.decline")
    try:
        state.runtime.secret_requests.decline(_text(params, "id"))
    except SecretRecordError as error:
        raise _invalid(str(error)) from None
    return {"id": params["id"], "declined": True}


def _source(state: Any, params: JsonObject) -> SecretSource:
    name = _text(params, "source")
    source = _secrets(state).source(name)
    if source is None:
        raise _invalid(f"{name} is not an available Secret Source")
    return source


def _unlock_source(state: Any, params: JsonObject) -> JsonObject:
    source = _source(state, params)
    inputs = _values(params, "inputs") or {}
    try:
        status = source.unlock(inputs)
    except Exception as error:  # a source's own failure is reported, not raised
        raise _invalid(f"{source.label} did not unlock: {error}") from None
    _LOGGER.info("Secret Source %s unlocked by the user (state=%s)", source.name, status.state)
    return _source_payload(_secrets(state), source)


async def _unlock(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"source", "inputs"}, "agent_secrets.source_unlock")
    return await _source_changing(state, _unlock_source, params)


def _lock_source(state: Any, params: JsonObject) -> JsonObject:
    source = _source(state, params)
    source.lock()
    _LOGGER.info("Secret Source %s locked by the user", source.name)
    return _source_payload(_secrets(state), source)


async def _lock(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"source"}, "agent_secrets.source_lock")
    return await _source_changing(state, _lock_source, params)


def _source_entries(state: Any, params: JsonObject) -> JsonObject:
    source = _source(state, params)
    container = _optional_text(params, "container")
    try:
        entries = source.entries(container)
        containers = list(source.containers())
    except Exception as error:  # a source's own failure is reported, not raised
        raise _invalid(f"{source.label} could not list its entries: {error}") from None
    return {
        "source": source.name,
        "containers": containers,
        "entries": [
            {
                "reference": entry.reference,
                "title": entry.title,
                "kind": entry.kind,
                "container": entry.container,
                "fields": [
                    {
                        "name": item.name,
                        "concealed": item.concealed,
                        **({} if item.concealed or item.value is None else {"value": item.value}),
                    }
                    for item in entry.fields
                ],
            }
            for entry in entries
        ],
    }


async def _entries(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"source", "container"}, "agent_secrets.source_entries")
    return await _run(_source_entries, state, params)


async def _run(function: Any, *arguments: Any) -> JsonObject:
    try:
        return await _WORKERS.run(function, *arguments)
    except SecretRecordError as error:
        raise _invalid(str(error)) from None
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _source_changing(state: Any, function: Any, params: JsonObject) -> JsonObject:
    """A source's lock state has no listener; Secret changes publish through theirs."""
    result = await _run(function, state, params)
    publish_resource_changed(state, RESOURCE_KIND_AGENT_SECRETS)
    return result


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the Agent Secret handlers."""
    return {
        "agent_secrets.list": serialized_read(_list, lock_attribute=_LOCK),
        "agent_secrets.create": serialized_mutation(_create, lock_attribute=_LOCK),
        "agent_secrets.update": serialized_mutation(_update, lock_attribute=_LOCK),
        "agent_secrets.delete": serialized_mutation(_delete, lock_attribute=_LOCK),
        "agent_secrets.reveal": serialized_read(_reveal, lock_attribute=_LOCK),
        # Not serialized: an answer must not wait behind a slow source call.
        "agent_secrets.answer": _answer,
        "agent_secrets.give": _give,
        "agent_secrets.decline": _decline,
        "agent_secrets.source_unlock": serialized_mutation(_unlock, lock_attribute=_LOCK),
        "agent_secrets.source_lock": serialized_mutation(_lock, lock_attribute=_LOCK),
        "agent_secrets.source_entries": serialized_read(_entries, lock_attribute=_LOCK),
    }
