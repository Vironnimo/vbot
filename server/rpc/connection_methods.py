"""Connection methods."""

from __future__ import annotations

import logging
from typing import Any

from core.providers.accounts import (
    CREDENTIAL_KEY_ACCOUNT_SEPARATOR,
    DEFAULT_ACCOUNT_ID,
    account_id_from_credential_key,
    compose_connection_id,
    derive_credential_key,
    split_connection_id,
    validate_account_id,
)
from core.providers.providers import custom_provider_credential_key
from core.providers.wire_profiles import custom_provider_wire_file
from core.secrets import CREDENTIALS, SecretStoreUnavailableError, credential_name_key
from core.settings.normalizers import (
    custom_provider_revision,
    normalize_custom_provider_id,
    normalize_custom_provider_settings,
)
from core.utils.errors import ConfigError
from core.utils.ids import is_reserved_name, reserved_name_message
from core.utils.workers import BoundedWorkerPool
from server.events import (
    RESOURCE_KIND_DATA_STORE,
    RESOURCE_KIND_MODELS,
    RESOURCE_KIND_PROVIDERS,
)
from server.rpc._mutations import serialized_mutation, serialized_read
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import (
    _publish_provider_auth_completed_event,
    publish_resource_changed,
)
from server.rpc.provider_access import (
    _api_key_connection,
    _connection_reachability,
    _connection_response,
    _device_flow_active,
    _device_flow_engine,
    _oauth_connection,
    _oauth_device_connection,
    _provider_connection,
    _runtime_token_store,
)
from server.rpc.validation import _reject_unsupported, _required_string

JsonObject = dict[str, Any]

_LOGGER = logging.getLogger("vbot.server.rpc.connection_methods")
_CONNECTION_WORKERS = BoundedWorkerPool(name="connection-rpc", max_workers=2)


async def _set_connection_enabled(state: Any, params: JsonObject) -> JsonObject:
    """Enable or disable one provider connection (persisted settings override).

    Enabling a local auto-refresh connection also forces an immediate catalog
    probe so the caller gets live reachability feedback ("enabled, but the
    service is not running" is a valid, reported outcome — the enable sticks).
    """

    _reject_unsupported(
        params, {"provider_id", "connection_id", "enabled"}, "connection set-enabled"
    )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.enabled must be a boolean")

    try:
        runtime = state.runtime
        local_connection_id, account_id = split_connection_id(provider_id, connection_id)
        if account_id is not None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "connection set-enabled targets a connection, not an account",
            )
        connection = _provider_connection(runtime, provider_id, connection_id)
        public_connection_id = compose_connection_id(provider_id, connection.id)
        was_enabled = await _CONNECTION_WORKERS.run(
            runtime.provider_credentials.is_connection_enabled, provider_id, public_connection_id
        )
        await _CONNECTION_WORKERS.run(
            runtime.storage.set_provider_connection_enabled, public_connection_id, enabled
        )

        reachable: bool | None = None
        if enabled and getattr(connection, "auto_refresh", False):
            await runtime.maybe_refresh_local_catalogs(force=True)
            reachable = _connection_reachability(runtime, public_connection_id)
        configured = await _CONNECTION_WORKERS.run(
            runtime.provider_credentials.has_credentials, provider_id, public_connection_id
        )
        usable = await _CONNECTION_WORKERS.run(
            runtime.provider_credentials.is_usable, provider_id, public_connection_id
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # An enable/disable immediately alters which models are selectable.
    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    response: JsonObject = {
        "provider_id": provider_id,
        "connection_id": public_connection_id,
        "enabled": enabled,
        "configured": configured,
    }
    if getattr(connection, "auto_refresh", False):
        response["reachable"] = reachable
    if was_enabled != enabled:
        _LOGGER.info(
            "Provider connection %s (provider=%s connection=%s configured=%s usable=%s%s)",
            "enabled" if enabled else "disabled",
            provider_id,
            connection.id,
            configured,
            usable,
            f" reachable={reachable}" if reachable is not None else "",
        )
    return response


async def _list_connections(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "connection.list does not accept params")
    try:
        runtime = state.runtime
        connections = [
            await _CONNECTION_WORKERS.run(_connection_response, runtime, provider_id, connection)
            for provider_id in runtime.providers.list_ids()
            for connection in runtime.providers.get(provider_id).connections
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"connections": connections}


def custom_provider_items(runtime: Any) -> list[JsonObject]:
    """Return secret-free Custom Provider records with live runtime state."""

    providers = runtime.storage.load_custom_providers_settings()
    items: list[JsonObject] = []
    for provider_id, provider in sorted(providers.items()):
        connection_id = f"{provider_id}:default"
        items.append(
            {
                "id": provider_id,
                **provider,
                "revision": custom_provider_revision(provider),
                "connection_id": connection_id,
                "credentials_configured": runtime.provider_credentials.has_credentials(
                    provider_id,
                    connection_id,
                ),
                "usable": runtime.provider_credentials.is_usable(
                    provider_id,
                    connection_id,
                ),
                "model_count": len(runtime.models.list_for_provider(provider_id)),
            }
        )
    return items


async def _list_custom_providers(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "provider.custom_list does not accept params")
    return {"providers": await _CONNECTION_WORKERS.run(custom_provider_items, state.runtime)}


async def _save_custom_provider(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, {"provider", "api_key", "expected_revision"}, "provider custom-save"
    )
    expected_revision = params.get("expected_revision")
    if expected_revision is not None and (
        not isinstance(expected_revision, str) or not expected_revision
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, "params.expected_revision must be a non-empty string"
        )
    raw_provider = params.get("provider")
    if not isinstance(raw_provider, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.provider must be an object")

    raw_provider_id = raw_provider.get("id")
    try:
        provider_id = normalize_custom_provider_id(raw_provider_id)
        provider = normalize_custom_provider_settings(
            provider_id,
            {key: value for key, value in raw_provider.items() if key != "id"},
        )
    except Exception as exc:
        if isinstance(exc, RpcError):
            raise
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    _reject_ignored_wire_entries(provider_id, provider)

    api_key = params.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.api_key must be a non-empty string")
    if api_key is not None and provider["auth"] != "api_key":
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.api_key is only supported when provider.auth is 'api_key'",
        )

    runtime = state.runtime
    existing_custom = await _CONNECTION_WORKERS.run(runtime.storage.load_custom_providers_settings)
    if provider_id not in existing_custom:
        try:
            existing_provider = runtime.providers.get(provider_id)
        except KeyError:
            existing_provider = None
        if existing_provider is not None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"Custom Provider id '{provider_id}' conflicts with a bundled Provider",
            )
        # Model discovery stores its results in files named after the id.
        if is_reserved_name(provider_id):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                reserved_name_message("Custom Provider id", provider_id),
            )

    previous = existing_custom.get(provider_id)
    credentials_changed = False
    try:
        await _CONNECTION_WORKERS.run(
            runtime.storage.save_custom_provider_settings,
            provider_id,
            provider,
            expected_revision=expected_revision,
        )
        await runtime.reload_custom_providers_async()
        if api_key is not None:
            await _CONNECTION_WORKERS.run(
                runtime.storage.set_data_dir_credential,
                custom_provider_credential_key(provider_id),
                api_key.strip(),
            )
            credentials_changed = True
        elif previous is not None and previous["auth"] == "api_key" and provider["auth"] == "none":
            credentials_changed = bool(
                await _CONNECTION_WORKERS.run(
                    _remove_custom_provider_credentials, runtime, provider_id
                )
            )
        await runtime.reload_environment_credentials_async()
        item = next(
            item
            for item in await _CONNECTION_WORKERS.run(custom_provider_items, runtime)
            if item["id"] == provider_id
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    publish_resource_changed(state, RESOURCE_KIND_MODELS)
    if credentials_changed:
        publish_resource_changed(state, RESOURCE_KIND_DATA_STORE)
    _LOGGER.info(
        "Custom Provider saved (provider=%s auth=%s models=%s)",
        provider_id,
        provider["auth"],
        len(provider["models"]),
    )
    return {"provider": item}


def _reject_ignored_wire_entries(provider_id: str, provider: JsonObject) -> None:
    """Refuse a ``wire`` block with entries the Runtime would ignore.

    A hand-edited ``settings.json`` degrades gracefully (the Runtime logs and
    ignores invalid entries); a save names every problem instead, and
    ``data.wire_issues`` lists them for inline display.
    """

    issues: list[str] = []
    custom_provider_wire_file(provider_id, provider, report=issues.append, source="wire")
    if issues:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"Custom Provider '{provider_id}' was not saved: its wire block has entries "
            f"vBot would ignore: {'; '.join(issues)}",
            data={"wire_issues": issues},
        )


async def _delete_custom_provider(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"provider_id"}, "provider custom-delete")
    try:
        provider_id = normalize_custom_provider_id(_required_string(params, "provider_id"))
    except Exception as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    runtime = state.runtime
    try:
        removed = await _CONNECTION_WORKERS.run(
            runtime.storage.delete_custom_provider_settings, provider_id
        )
        if removed is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"Custom Provider '{provider_id}' does not exist",
            )
        credentials_removed = await _CONNECTION_WORKERS.run(
            _remove_custom_provider_credentials, runtime, provider_id
        )
        await runtime.reload_custom_providers_async()
        await runtime.reload_environment_credentials_async()
    except RpcError:
        raise
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    publish_resource_changed(state, RESOURCE_KIND_MODELS)
    if credentials_removed:
        publish_resource_changed(state, RESOURCE_KIND_DATA_STORE)
    _LOGGER.info("Custom Provider deleted (provider=%s)", provider_id)
    return {
        "provider_id": provider_id,
        "deleted": True,
        "credentials_removed": credentials_removed,
    }


def _remove_custom_provider_credentials(runtime: Any, provider_id: str) -> int:
    """Remove every stored credential of the Provider, damaged ones included.

    Returns how many were removed. An unavailable Secret Store cannot be
    written: its entries stay, unusable until its key returns, and a warning
    names the Provider.
    """
    base_key = credential_name_key(custom_provider_credential_key(provider_id))
    account_prefix = f"{base_key}{CREDENTIAL_KEY_ACCOUNT_SEPARATOR}"
    try:
        names = [
            name
            for name in runtime.storage.secrets.entry_names(CREDENTIALS)
            if credential_name_key(name) == base_key
            or credential_name_key(name).startswith(account_prefix)
        ]
        return sum(bool(runtime.storage.remove_data_dir_credential(name)) for name in names)
    except SecretStoreUnavailableError:
        _LOGGER.warning(
            "Custom Provider credentials stay in the unavailable Secret Store (provider=%s)",
            provider_id,
        )
        return 0


def _account_param(params: JsonObject) -> str | None:
    """Return the validated optional ``account`` param, or ``None`` when absent."""

    if params.get("account") is None:
        return None
    account = _required_string(params, "account")
    try:
        return validate_account_id(account)
    except ConfigError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


def _effective_account_id(
    provider_id: str,
    connection_id: str | None,
    account: str | None,
) -> str:
    """Combine the ``account`` param with an account-carrying connection id.

    An account embedded in the compositional connection id and an explicit
    ``account`` param must agree; either alone wins over the default.
    """

    embedded_account_id = None
    if connection_id is not None:
        _local_connection_id, embedded_account_id = split_connection_id(provider_id, connection_id)
    if account is not None and embedded_account_id is not None and account != embedded_account_id:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.account '{account}' conflicts with account "
            f"'{embedded_account_id}' in params.connection_id",
        )
    if account is not None:
        return account
    if embedded_account_id is not None:
        return embedded_account_id
    return DEFAULT_ACCOUNT_ID


async def _set_provider_key(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, {"provider_id", "connection_id", "value", "account"}, "provider set-key"
    )

    provider_id = _required_string(params, "provider_id")
    value = _required_string(params, "value")
    raw_connection_id = params.get("connection_id")
    connection_id = (
        _required_string(params, "connection_id") if raw_connection_id is not None else None
    )
    account = _account_param(params)

    try:
        runtime = state.runtime
        connection = _api_key_connection(runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        public_connection_id = compose_connection_id(provider_id, connection.id)
        credential_key = derive_credential_key(connection.auth.credential_key, account_id)
        environment = await _CONNECTION_WORKERS.run(runtime.storage.load_environment)
        previous_value = environment.get(credential_key)
        await _CONNECTION_WORKERS.run(
            runtime.storage.set_data_dir_credential, credential_key, value
        )
        for key in environment:
            # On Windows another spelling of the name is the same credential, which
            # the write has just replaced; removing it would remove the new value.
            if credential_name_key(key) != credential_name_key(credential_key) and (
                account_id_from_credential_key(connection.auth.credential_key, key) == account_id
            ):
                await _CONNECTION_WORKERS.run(runtime.storage.remove_data_dir_credential, key)
        await runtime.reload_environment_credentials_async()
        account_connection_id = compose_connection_id(provider_id, connection.id, account_id)
        usable = await _CONNECTION_WORKERS.run(
            runtime.provider_credentials.is_usable, provider_id, account_connection_id
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # A credential change immediately alters which models are selectable, and the
    # Secret Store's counts are part of the data-store status.
    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    publish_resource_changed(state, RESOURCE_KIND_DATA_STORE)
    if previous_value != value:
        _LOGGER.info(
            "Provider credential saved (provider=%s connection=%s configured=true usable=%s)",
            provider_id,
            connection.id,
            usable,
        )
    return {
        "provider_id": provider_id,
        "connection_id": public_connection_id,
        "account": account_id,
        "credential_key": credential_key,
        "configured": True,
    }


async def _unset_provider_key(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"provider_id", "connection_id", "account"}, "provider unset-key")

    provider_id = _required_string(params, "provider_id")
    raw_connection_id = params.get("connection_id")
    connection_id = (
        _required_string(params, "connection_id") if raw_connection_id is not None else None
    )
    account = _account_param(params)

    try:
        runtime = state.runtime
        connection = _api_key_connection(runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        public_connection_id = compose_connection_id(provider_id, connection.id)
        credential_key = derive_credential_key(connection.auth.credential_key, account_id)
        credential_keys = {credential_key} | {
            key
            for key in await _CONNECTION_WORKERS.run(runtime.storage.load_environment)
            if account_id_from_credential_key(connection.auth.credential_key, key) == account_id
        }
        removed = (
            sum(
                [
                    bool(
                        await _CONNECTION_WORKERS.run(
                            runtime.storage.remove_data_dir_credential, key
                        )
                    )
                    for key in sorted(credential_keys)
                ]
            )
            > 0
        )
        await runtime.reload_environment_credentials_async()
        configured = await _CONNECTION_WORKERS.run(
            runtime.provider_credentials.has_credentials,
            provider_id,
            compose_connection_id(provider_id, connection.id, account_id),
        )
        usable = await _CONNECTION_WORKERS.run(
            runtime.provider_credentials.is_usable,
            provider_id,
            compose_connection_id(provider_id, connection.id, account_id),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # A credential change immediately alters which models are selectable, and the
    # Secret Store's counts are part of the data-store status.
    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    if removed:
        publish_resource_changed(state, RESOURCE_KIND_DATA_STORE)
        _LOGGER.info(
            "Provider credential removed (provider=%s connection=%s configured=%s usable=%s)",
            provider_id,
            connection.id,
            configured,
            usable,
        )
    return {
        "provider_id": provider_id,
        "connection_id": public_connection_id,
        "account": account_id,
        "credential_key": credential_key,
        "removed": removed,
        "configured": configured,
    }


async def _connect_provider(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"provider_id", "connection_id", "account"}, "provider connect")

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    account = _account_param(params)

    try:
        connection = _oauth_device_connection(state.runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        public_connection_id = compose_connection_id(provider_id, connection.id)
        engine = _device_flow_engine(state)
        oauth_config = connection.oauth

        async def on_complete(*, success: bool) -> None:
            _publish_provider_auth_completed_event(
                state,
                provider_id=provider_id,
                connection_id=public_connection_id,
                account=account_id,
                success=success,
            )
            # The targeted auth event drives the OAuth modal; a successful login
            # also newly enables this provider's models, so signal the generic
            # reload alongside it (only on success — connect-start does not).
            if success:
                publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)

        session = await engine.connect(
            provider_id,
            connection.id,
            oauth_config,
            on_complete,
            account_id=account_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "user_code": session.user_code,
        "verification_uri": session.verification_uri,
        "expires_in": session.expires_in,
        "account": account_id,
    }


async def _disconnect_provider(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"provider_id", "connection_id", "account"}, "provider disconnect")

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    account = _account_param(params)

    try:
        connection = _oauth_connection(state.runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        token_store = _runtime_token_store(state.runtime)
        # Existence, not a load: disconnect must also remove a token file that fails to load.
        had_token = await _CONNECTION_WORKERS.run(
            token_store.exists, provider_id, connection.id, account_id=account_id
        )
        engine = getattr(state, "device_flow_engine", None)
        flow_active = _device_flow_active(engine, provider_id, connection.id, account_id)
        if engine is not None:
            await engine.cancel_flow_and_wait(provider_id, connection.id, account_id)
        await _CONNECTION_WORKERS.run(
            token_store.delete, provider_id, connection.id, account_id=account_id
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # Dropping a connection immediately alters which models are selectable.
    publish_resource_changed(state, RESOURCE_KIND_PROVIDERS)
    if had_token or flow_active:
        _LOGGER.info(
            "OAuth provider disconnected (provider=%s connection=%s)",
            provider_id,
            connection.id,
        )
    return {
        "provider_id": provider_id,
        "connection_id": compose_connection_id(provider_id, connection.id),
        "account": account_id,
        "status": "disconnected",
    }


async def _provider_connection_status(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(
        params, {"provider_id", "connection_id", "account"}, "provider connection status"
    )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    account = _account_param(params)

    try:
        connection = _oauth_connection(state.runtime, provider_id, connection_id)
        account_id = _effective_account_id(provider_id, connection_id, account)
        token_store = _runtime_token_store(state.runtime)
        engine = getattr(state, "device_flow_engine", None)
        connected = await _CONNECTION_WORKERS.run(
            token_store.has_valid_token, provider_id, connection.id, account_id=account_id
        )
        flow_active = _device_flow_active(engine, provider_id, connection.id, account_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "provider_id": provider_id,
        "connection_id": compose_connection_id(provider_id, connection.id),
        "account": account_id,
        "connected": connected,
        "flow_active": flow_active,
    }


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the registered connection RPC handlers."""
    return {
        "connection.list": serialized_read(
            _list_connections, lock_attribute="_settings_mutation_lock"
        ),
        "connection.set_enabled": serialized_mutation(
            _set_connection_enabled, lock_attribute="_settings_mutation_lock"
        ),
        "provider.custom_list": serialized_read(
            _list_custom_providers, lock_attribute="_settings_mutation_lock"
        ),
        "provider.custom_save": serialized_mutation(
            _save_custom_provider, lock_attribute="_settings_mutation_lock"
        ),
        "provider.custom_delete": serialized_mutation(
            _delete_custom_provider, lock_attribute="_settings_mutation_lock"
        ),
        "provider.set_key": serialized_mutation(
            _set_provider_key, lock_attribute="_settings_mutation_lock"
        ),
        "provider.unset_key": serialized_mutation(
            _unset_provider_key, lock_attribute="_settings_mutation_lock"
        ),
        "provider.connect": serialized_mutation(
            _connect_provider, lock_attribute="_settings_mutation_lock"
        ),
        "provider.disconnect": serialized_mutation(
            _disconnect_provider, lock_attribute="_settings_mutation_lock"
        ),
        "provider.connection_status": serialized_read(
            _provider_connection_status, lock_attribute="_settings_mutation_lock"
        ),
    }
