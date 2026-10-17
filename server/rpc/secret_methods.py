"""RPCs for the data-directory credentials in the Secret Store.

Names, sources and states only: a value enters through ``secrets.set`` and
never leaves the server, in a result, an error message or a log.
"""

from __future__ import annotations

import logging
from typing import Any

from core.secrets import CREDENTIALS
from core.storage.storage import ENV_KEY_PATTERN
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_DATA_STORE, RESOURCE_KIND_PROVIDERS
from server.rpc._mutations import serialized_mutation, serialized_read
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]

_LOGGER = logging.getLogger("vbot.server.rpc.secret_methods")
_SECRET_WORKERS = BoundedWorkerPool(name="secrets-rpc", max_workers=1)
# Credential changes serialize with the Provider key and Settings mutations.
_LOCK = "_settings_mutation_lock"
_MAX_NAME_LENGTH = 200


def _credential_name(params: JsonObject) -> str:
    """The validated ``name`` param; a refusal never repeats it (it may be a pasted value)."""
    name = params.get("name")
    if (
        not isinstance(name, str)
        or len(name) > _MAX_NAME_LENGTH
        or ENV_KEY_PATTERN.fullmatch(name) is None
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.name must be an environment variable name: letters, digits and "
            f"underscores, not starting with a digit, at most {_MAX_NAME_LENGTH} characters",
        )
    return name


def _credential_value(params: JsonObject) -> str:
    value = params.get("value")
    if not isinstance(value, str) or not value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.value must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.value must be a single line")
    return value


def _credential_state(runtime: Any, name: str) -> JsonObject:
    """Where the Runtime finds ``name`` now and whether its value is usable."""
    return {
        "name": name,
        # Trimmed like the credential consumers do: a blank value is missing.
        "configured": bool(runtime.resolve_environment_credential(name).strip()),
        "source": runtime.environment_credential_source(name),
    }


def _credential_list(runtime: Any) -> JsonObject:
    store = runtime.storage.secrets
    status = store.status()
    damaged = {
        entry.partition("/")[2] for entry in status.damaged if entry.startswith(f"{CREDENTIALS}/")
    }
    return {
        "state": status.state,
        "reason": status.reason,
        "credentials": [
            {**_credential_state(runtime, name), "damaged": name in damaged}
            for name in store.entry_names(CREDENTIALS)
        ],
    }


async def _list_credentials(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "secrets.list")
    try:
        return await _SECRET_WORKERS.run(_credential_list, state.runtime)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _set_credential(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"name", "value"}, "secrets.set")
    name = _credential_name(params)
    value = _credential_value(params)
    runtime = state.runtime
    try:
        changed = await _SECRET_WORKERS.run(runtime.storage.set_data_dir_credential, name, value)
        if changed:
            await runtime.reload_environment_credentials_async()
        result = _credential_state(runtime, name)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if changed:
        _publish(state)
        _LOGGER.info(
            "Data-directory credential saved (name=%s effective_source=%s)",
            name,
            result["source"],
        )
    return {**result, "changed": changed}


async def _unset_credential(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"name"}, "secrets.unset")
    name = _credential_name(params)
    runtime = state.runtime
    try:
        removed = await _SECRET_WORKERS.run(runtime.storage.remove_data_dir_credential, name)
        if removed:
            await runtime.reload_environment_credentials_async()
        result = _credential_state(runtime, name)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if removed:
        _publish(state)
        _LOGGER.info(
            "Data-directory credential removed (name=%s effective_source=%s)",
            name,
            result["source"],
        )
    return {**result, "removed": removed}


def _publish(state: Any) -> None:
    # The store's counts are part of the data-store status, and a credential can
    # make a Provider usable or unusable.
    publish_resource_changed(state, RESOURCE_KIND_DATA_STORE)
    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the data-directory credential handlers."""
    return {
        "secrets.list": serialized_read(_list_credentials, lock_attribute=_LOCK),
        "secrets.set": serialized_mutation(_set_credential, lock_attribute=_LOCK),
        "secrets.unset": serialized_mutation(_unset_credential, lock_attribute=_LOCK),
    }
