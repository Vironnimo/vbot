"""Catalog refresh policy and publication, internal to ProviderRuntime."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from core.models import discovery
from core.models.database import (
    ModelDatabaseRefresh,
    begin_runtime_model_database_refresh,
    begin_system_model_database_refresh,
    read_model_database_manifest,
)
from core.models.discovery import ModelDiscoveryError
from core.models.models import ModelRegistry
from core.models.models_dev import (
    ModelsDevCatalog,
    ModelsDevError,
    fetch_catalog,
    refresh_canonical_layer,
)
from core.providers.accounts import Account, ConnectionRef, split_connection_id
from core.providers.errors import NetworkError, ProviderError
from core.providers.providers import ConnectionConfig
from core.providers.token_getter import (
    COPILOT_API_ENDPOINT_EXTRA_KEY,
    OAuthTokenGetter,
    TokenGetter,
)
from core.utils.errors import ConfigError
from core.utils.logging import get_logger
from core.utils.retry import caller_owns_retries
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

if TYPE_CHECKING:
    from core.providers.runtime import ProviderRuntime

JsonObject = dict[str, Any]
_LOGGER = get_logger("providers.catalog_refresh")
_MODEL_DB_WORKERS = BoundedWorkerPool(name="model-db-refresh", max_workers=1)
_CATALOG_READ_WORKERS = BoundedWorkerPool(name="catalog-access", max_workers=4)
# Automatic sweeps make one attempt per Connection, then throttle from completion.
LOCAL_CATALOG_REFRESH_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class _RefreshOutcome:
    result: JsonObject
    providers: frozenset[str]
    canonical: bool
    publish: bool = True


async def refresh_model_database(
    runtime: ProviderRuntime, *, provider_id: str | None = None, target: str = "runtime"
) -> JsonObject:
    """Refresh one Provider or the whole Model DB, then publish and reload in place."""
    if target not in {"runtime", "system"}:
        raise ConfigError("model refresh target must be 'runtime' or 'system'")
    async with runtime.model_database_refresh():
        begin = (
            _runtime_refresh_beginner(runtime)
            if target == "runtime"
            else lambda: begin_system_model_database_refresh(
                runtime._storage.resources_dir,
                provider_ids=_refresh_provider_ids(runtime, include_instance_catalogs=False),
            )
        )

        async def refresh(resources_dir: Path) -> _RefreshOutcome:
            if provider_id is not None:
                return await _refresh_provider_model_db(
                    runtime,
                    provider_id,
                    resources_dir,
                    include_instance_catalogs=target == "runtime",
                )
            return await _refresh_global_model_db(
                runtime, resources_dir, include_instance_catalogs=target == "runtime"
            )

        outcome, invalid_entry_count = await _publish_model_db_refresh(runtime, begin, refresh)
    result = outcome.result
    if provider_id is None:
        provider_count = int(result.get("refreshed_count", 0))
        model_count = int(result.get("model_count", 0))
    else:
        provider_count = 1
        model_count = _model_count(result)
    errors = result.get("errors")
    error_count = len(errors) if isinstance(errors, list) else 0
    if invalid_entry_count:
        result["invalid_entry_count"] = invalid_entry_count
    _LOGGER.info(
        "Model database refreshed "
        "(scope=%s target=%s providers=%s models=%s errors=%s invalid_entries=%s)",
        provider_id if provider_id is not None else "all",
        target,
        provider_count,
        model_count,
        error_count,
        invalid_entry_count,
    )
    return result


async def maybe_refresh_local_catalogs(runtime: ProviderRuntime, *, force: bool = False) -> None:
    async with runtime.model_database_refresh():
        now = time.monotonic()
        if (
            not force
            and runtime.refresh_at is not None
            and now - runtime.refresh_at < LOCAL_CATALOG_REFRESH_TTL_SECONDS
        ):
            return
        targets = await _CATALOG_READ_WORKERS.run(runtime._auto_refresh_targets)
        if not targets:
            return

        async def refresh(resources_dir: Path) -> _RefreshOutcome:
            refreshed: set[str] = set()
            for provider_id, provider, connection in targets:
                connection_id = f"{provider_id}:{connection.id}"
                try:
                    credential_value = await _CATALOG_READ_WORKERS.run(
                        runtime._credentials.get_credentials,
                        provider_id,
                        connection_id,
                    )
                except ConfigError as error:
                    runtime._logger.debug(
                        "Skipping local catalog refresh for %s: %s",
                        connection_id,
                        error,
                    )
                    continue
                try:
                    with caller_owns_retries():
                        await discovery.refresh_models(
                            provider,
                            credential_value,
                            resources_dir,
                            credential_connection=connection,
                            wire_files=runtime._wire_profiles.files,
                        )
                except ModelDiscoveryError as error:
                    previous = runtime._connection_reachability.get(connection_id)
                    runtime._connection_reachability[connection_id] = False
                    if previous is True:
                        runtime._logger.warning(
                            "Local provider connection became unreachable "
                            "(provider=%s connection=%s): %s",
                            provider_id,
                            connection.id,
                            error,
                        )
                    else:
                        runtime._logger.debug(
                            "Local catalog refresh failed for %s: %s",
                            connection_id,
                            error,
                        )
                    continue
                previous = runtime._connection_reachability.get(connection_id)
                runtime._connection_reachability[connection_id] = True
                if previous is False:
                    runtime._logger.info(
                        "Local provider connection recovered (provider=%s connection=%s)",
                        provider_id,
                        connection.id,
                    )
                refreshed.add(provider_id)

            return _RefreshOutcome({}, frozenset(refreshed), False, publish=bool(refreshed))

        try:
            await _publish_model_db_refresh(
                runtime,
                lambda: begin_runtime_model_database_refresh(
                    runtime._storage.resources_dir, runtime._storage.data_dir
                ),
                refresh,
                notify_changed=True,
            )
        except Exception as error:
            runtime._logger.warning("Local catalog refresh could not be published: %s", error)
        finally:
            # Failed and slow sweeps also throttle from completion.
            runtime.refresh_at = time.monotonic()


def _runtime_refresh_beginner(runtime: ProviderRuntime) -> Callable[[], ModelDatabaseRefresh]:
    """Return how a runtime refresh stages its working copy of the Model DB."""

    def begin() -> ModelDatabaseRefresh:
        return begin_runtime_model_database_refresh(
            runtime._storage.resources_dir,
            Path(runtime._storage.data_dir),
            provider_ids=_existing_provider_ids(runtime),
        )

    return begin


async def _publish_model_db_refresh(
    runtime: ProviderRuntime,
    begin: Callable[[], ModelDatabaseRefresh],
    refresh: Callable[[Path], Awaitable[_RefreshOutcome]],
    *,
    notify_changed: bool = False,
) -> tuple[_RefreshOutcome, int]:
    """Stage, refresh, validate, publish, and reload one Model DB refresh.

    The caller holds the Runtime's Model DB refresh admission. Any failure
    discards the working copy and leaves the published root untouched. Returns
    the outcome and the number of entries Load ignores.
    """

    staged: list[ModelDatabaseRefresh] = []

    def stage() -> None:
        # Retain the result inside the worker: a cancelled caller still owns
        # the staged directory after cancellation waits for staging to finish.
        staged.append(begin())

    async def publish(database_refresh: ModelDatabaseRefresh, outcome: _RefreshOutcome) -> None:
        await _MODEL_DB_WORKERS.run(
            database_refresh.commit, providers=outcome.providers, canonical=outcome.canonical
        )
        changed = await _reload_runtime_model_registry(runtime, runtime._storage.resources_dir)
        if notify_changed and changed:
            runtime._notify_catalog_changed()

    try:
        await _MODEL_DB_WORKERS.run(stage)
        database_refresh = staged[0]
        outcome = await refresh(database_refresh.resources_dir)
        if not outcome.publish:
            return outcome, 0
        # Validation collects instead of logging: the live reload below logs
        # each entry Load ignores, so the refresh reports it once.
        invalid_entry_count = len(
            await _MODEL_DB_WORKERS.run(ModelRegistry.validate, database_refresh.resources_dir)
        )
        # Publication and the in-memory swap are one admitted operation. Once
        # publication starts, cancellation may not leave the registry stale.
        await settle_before_cancelling(publish(database_refresh, outcome))
    finally:
        for database_refresh in staged:
            ModelRegistry.invalidate(database_refresh.resources_dir)
            await _MODEL_DB_WORKERS.run(database_refresh.discard)
    return outcome, invalid_entry_count


def start_installation_catalog_restore(runtime: ProviderRuntime) -> asyncio.Task[None] | None:
    """Rebuild a missing or incompatible runtime Model DB once, in the background.

    Without a usable runtime Model DB, Load serves every catalog from the bundled
    Model DB, so the catalogs only this installation produces are gone. Call this
    once after startup, before any refresh can publish a new runtime root. It
    refreshes each Custom Provider with a catalog endpoint through the same
    admission and publication as ``model.refresh_db``; the published root also
    replaces an incompatible one, so this happens once. Local catalogs return
    with their automatic refresh; built-in Providers use the bundled catalogs
    until the next manual refresh. Returns the started background refresh, if
    any; Runtime shutdown drains it.
    """

    runtime_models_dir = runtime._storage.layout.models
    if read_model_database_manifest(runtime_models_dir) is not None:
        return None
    condition = (
        "incompatible"
        if runtime_models_dir.exists() and any(runtime_models_dir.iterdir())
        else "missing"
    )
    provider_ids = [
        provider_id
        for provider_id in runtime._providers.list_ids()
        if _is_custom_provider(provider := runtime._providers.get(provider_id))
        and _provider_supports_refresh(provider)
    ]
    if condition == "missing" and not provider_ids:
        return None
    task = asyncio.create_task(
        _restore_installation_catalogs(runtime, provider_ids, condition),
        name="model-db-restore",
    )
    runtime._track_catalog_refresh(task)
    return task


async def _restore_installation_catalogs(
    runtime: ProviderRuntime,
    provider_ids: list[str],
    condition: str,
) -> None:

    async def refresh(resources_dir: Path) -> _RefreshOutcome:
        successes, errors = await _refresh_providers(runtime, provider_ids, resources_dir, None)
        return _RefreshOutcome(
            result={"providers": successes, "errors": errors},
            providers=_refreshed_provider_ids(successes),
            canonical=False,
        )

    try:
        async with runtime.model_database_refresh():
            outcome, _invalid_entry_count = await _publish_model_db_refresh(
                runtime,
                _runtime_refresh_beginner(runtime),
                refresh,
            )
    except Exception as error:
        _LOGGER.warning("Could not rebuild the %s runtime Model DB: %s", condition, error)
        return
    runtime._notify_catalog_changed()
    provider_count, model_count = _summarize_refreshed_providers(outcome.result["providers"])
    _LOGGER.info(
        "Rebuilt the %s runtime Model DB "
        "(custom_providers=%s refreshed=%s models=%s errors=%s); built-in Providers use "
        "the bundled catalogs and local catalogs return with their automatic refresh",
        condition,
        len(provider_ids),
        provider_count,
        model_count,
        len(outcome.result["errors"]),
    )


async def _fetch_catalog_for_refresh() -> ModelsDevCatalog | None:
    """Fetch the models.dev catalog once for a refresh — best-effort.

    Shared by both refresh entry points so the public catalog is fetched a
    single time and threaded to every per-provider refresh + the canonical
    projection. A fetch failure logs and returns ``None``: the canonical join is
    enrichment, not a dependency, so refresh still writes pure provider wire
    facts without it.
    """

    try:
        return await fetch_catalog()
    except (ModelsDevError, ProviderError, NetworkError, httpx.HTTPError) as exc:
        _LOGGER.warning(
            "models.dev catalog unavailable for this refresh; "
            "writing provider catalogs without canonical enrichment: %s",
            exc,
        )
        return None


async def _refresh_global_model_db(
    runtime: ProviderRuntime,
    resources_dir: Path,
    *,
    include_instance_catalogs: bool,
) -> _RefreshOutcome:
    catalog = await _fetch_catalog_for_refresh()
    provider_ids = _refresh_provider_ids(
        runtime, include_instance_catalogs=include_instance_catalogs
    )
    refreshed_providers, refresh_errors = await _refresh_providers(
        runtime, provider_ids, resources_dir, catalog
    )
    canonical_result = await _refresh_canonical_layer_if_possible(
        catalog, resources_dir, runtime, provider_ids
    )
    provider_count, model_count = _summarize_refreshed_providers(refreshed_providers)
    result: JsonObject = {
        "providers": refreshed_providers,
        "refreshed_count": provider_count,
        "model_count": model_count,
        "canonical": canonical_result,
    }
    if refresh_errors:
        result["errors"] = refresh_errors
    return _RefreshOutcome(
        result=result,
        providers=_refreshed_provider_ids(refreshed_providers),
        canonical=canonical_result is not None,
    )


async def _refresh_providers(
    runtime: ProviderRuntime,
    provider_ids: Iterable[str],
    resources_dir: Path,
    catalog: ModelsDevCatalog | None,
) -> tuple[list[JsonObject], list[JsonObject]]:
    """Refresh every refreshable Provider of ``provider_ids``; collect successes and errors."""

    successes: list[JsonObject] = []
    errors: list[JsonObject] = []
    for provider_id in provider_ids:
        provider = runtime._providers.get(provider_id)
        if not _provider_supports_refresh(provider):
            continue
        provider_successes, provider_errors = await _refresh_provider_connections(
            runtime,
            provider_id,
            provider,
            resources_dir,
            catalog,
        )
        successes.extend(provider_successes)
        errors.extend(provider_errors)
    return successes, errors


def _refreshed_provider_ids(successes: list[JsonObject]) -> frozenset[str]:
    """Return the Providers whose catalog at least one Connection refreshed."""

    return frozenset(
        entry["provider_id"] for entry in successes if isinstance(entry.get("provider_id"), str)
    )


async def _refresh_canonical_layer_if_possible(
    catalog: ModelsDevCatalog | None,
    resources_dir: Path,
    runtime: ProviderRuntime,
    provider_ids: list[str],
) -> JsonObject | None:
    """Project the canonical layer when a catalog is available; else ``None``.

    Writes ``models.json``, seeds ``models.overrides.json`` and reprices the
    existing catalogs of ``provider_ids``. Skipped (returns ``None``) when the
    catalog could not be fetched.
    """

    if catalog is None:
        return None
    return await refresh_canonical_layer(
        resources_dir,
        catalog=catalog,
        provider_catalog_ids={
            provider_id: runtime._providers.get(provider_id).effective_models_dev_id()
            for provider_id in provider_ids
        },
    )


def _refresh_provider_ids(
    runtime: ProviderRuntime, *, include_instance_catalogs: bool
) -> list[str]:
    """Return the Providers one refresh covers.

    The shipped Model DB excludes instance inventories, including local
    servers and Custom Providers, independently of credentials or reachability.
    """

    return [
        provider_id
        for provider_id in runtime._providers.list_ids()
        if include_instance_catalogs
        or not _is_instance_catalog(runtime._providers.get(provider_id))
    ]


def _is_custom_provider(provider: Any) -> bool:
    return bool(getattr(provider, "custom", False))


def _is_instance_catalog(provider: Any) -> bool:
    return (
        _is_custom_provider(provider) or getattr(provider, "catalog_scope", "shared") == "instance"
    )


def _existing_provider_ids(runtime: ProviderRuntime) -> set[str]:
    """Return every Provider that currently exists, Custom Providers included.

    Settings may hold a Custom Provider the registry skipped as invalid; its
    catalog still belongs to an existing Provider.
    """

    return set(runtime._providers.list_ids()) | set(
        runtime._storage.load_custom_providers_settings()
    )


async def _refresh_provider_model_db(
    runtime: ProviderRuntime,
    provider_id: str,
    resources_dir: Path,
    *,
    include_instance_catalogs: bool,
) -> _RefreshOutcome:
    try:
        provider = runtime._providers.get(provider_id)
    except KeyError as exc:
        raise ConfigError(f"unknown provider: {provider_id}") from exc
    if not include_instance_catalogs and _is_instance_catalog(provider):
        raise ConfigError(
            f"Provider '{provider_id}' has an instance-specific catalog; "
            "refresh it into the runtime Model DB",
        )
    if not _provider_supports_refresh(provider):
        raise ConfigError(
            f"provider '{provider_id}' does not support model refresh",
        )

    catalog = await _fetch_catalog_for_refresh()
    successes, errors = await _refresh_provider_connections(
        runtime,
        provider_id,
        provider,
        resources_dir,
        catalog,
    )
    if not successes:
        # An explicit single-provider refresh that produced nothing useful
        # still reports why: a discovery failure surfaces its message, a
        # disabled catalog Connection is named, an absent credential keeps the
        # "not found" wording.
        if errors:
            raise ConfigError(str(errors[0]["error"]))
        raise ConfigError(
            await _CATALOG_READ_WORKERS.run(
                _no_refreshable_connection_message, runtime, provider_id, provider
            ),
        )
    canonical_result = await _refresh_canonical_layer_if_possible(
        catalog,
        resources_dir,
        runtime,
        _refresh_provider_ids(runtime, include_instance_catalogs=include_instance_catalogs),
    )
    result = dict(successes[0])
    if errors:
        result["errors"] = errors
    return _RefreshOutcome(
        result=result,
        providers=frozenset({provider_id}),
        canonical=canonical_result is not None,
    )


def _provider_supports_refresh(provider: Any) -> bool:
    """Return whether *provider* exposes a refreshable ``models_endpoint``.

    A provider counts as refreshable when it has a provider-level
    ``models_endpoint`` or at least one connection-level one. This guard is
    separate from credential presence so the RPC layer can distinguish
    "no refresh endpoint" from "no credentials".
    """

    if getattr(provider, "models_endpoint", None):
        return True
    return any(
        getattr(connection, "models_endpoint", None)
        for connection in getattr(provider, "connections", [])
    )


def _no_refreshable_connection_message(
    runtime: ProviderRuntime, provider_id: str, provider: Any
) -> str:
    """Explain why no catalog Connection of *provider* could be refreshed."""

    disabled = [
        f"{provider_id}:{connection.id}"
        for connection in getattr(provider, "connections", [])
        if _connection_models_endpoint(connection, provider)
        and not runtime._credentials.is_connection_enabled(
            provider_id, f"{provider_id}:{connection.id}"
        )
    ]
    if not disabled:
        return f"Provider credentials not found for provider '{provider_id}'"
    verb = "is" if len(disabled) == 1 else "are"
    return (
        f"Provider '{provider_id}' has no enabled Connection to refresh: "
        f"{', '.join(disabled)} {verb} disabled; enable it to refresh its Models"
    )


async def _refresh_provider_connections(
    runtime: ProviderRuntime,
    provider_id: str,
    provider: Any,
    resources_dir: Path,
    models_dev_catalog: ModelsDevCatalog | None = None,
) -> tuple[list[JsonObject], list[JsonObject]]:
    """Refresh every connection on *provider* that supports it.

    Connections without an effective ``models_endpoint`` are skipped.
    Credentialed catalogs also require a usable Connection; explicitly public
    catalogs refresh without a credential while inference usability remains
    unchanged. Successful refreshes accumulate into the shared
    ``<provider>.json`` catalog via discovery's merge logic. The
    pre-fetched ``models_dev_catalog`` is threaded into each refresh so the
    public catalog is fetched once per refresh, not once per connection.

    A connection whose discovery fails (provider unreachable, bad key, fatal
    HTTP status, malformed catalog) is logged and recorded as an error rather
    than raised: one broken connection must never abort its sibling
    connections or — in a global refresh — the remaining providers. Returns
    ``(successes, errors)``; each error carries the connection id and message.
    """

    successes: list[JsonObject] = []
    errors: list[JsonObject] = []
    for connection in getattr(provider, "connections", []):
        if not _connection_models_endpoint(connection, provider):
            continue
        connection_id = f"{provider_id}:{connection.id}"
        requires_credentials = getattr(connection, "catalog_requires_credentials", True)
        if requires_credentials and not await _CATALOG_READ_WORKERS.run(
            runtime._credentials.is_usable, provider_id, connection_id
        ):
            continue
        try:
            async with discovery_credential(runtime, provider_id, connection_id, connection) as (
                discovery_connection,
                credential_value,
            ):
                with _single_attempt_if(getattr(connection, "auto_refresh", False)):
                    result = await discovery.refresh_models(
                        provider,
                        credential_value,
                        resources_dir,
                        credential_connection=discovery_connection,
                        models_dev_catalog=models_dev_catalog,
                        wire_files=runtime.wire_profile_files,
                    )
        except (ConfigError, ModelDiscoveryError, ProviderError, NetworkError) as exc:
            _LOGGER.warning(
                "Model refresh failed for provider '%s' connection '%s': %s",
                provider_id,
                connection.id,
                exc,
            )
            errors.append(
                {
                    "provider_id": provider_id,
                    "connection_id": connection_id,
                    "error": str(exc),
                }
            )
            continue
        successes.append(result)
    return successes, errors


def _single_attempt_if(auto_refresh: bool) -> AbstractContextManager[None]:
    """Probe an auto-refresh (local) Connection once instead of with backoff.

    Such a Connection is usually a local server that is simply not running;
    retrying only delays the refresh, and the automatic sweep re-probes it.
    """

    return caller_owns_retries() if auto_refresh else nullcontext()


async def _reload_runtime_model_registry(
    runtime: ProviderRuntime, system_resources_dir: Path
) -> bool:
    # Reload in place rather than rebinding ``runtime._models``: services that
    # captured the registry at construction (task-model targets for
    # speech/image/embeddings, the status display, the recall backend) hold the
    # same instance, so an in-place swap reaches all of them without re-wiring.
    # The catalog files are read and assembled off the Event Loop.
    return await runtime._models.reload_async(
        system_resources_dir,
        runtime_models_dir=runtime._storage.layout.models,
        custom_providers=await _CATALOG_READ_WORKERS.run(
            runtime._storage.load_custom_providers_settings
        ),
    )


def _model_count(result: JsonObject) -> int:
    model_count = result.get("model_count", 0)
    if isinstance(model_count, bool) or not isinstance(model_count, int):
        return 0
    return int(model_count)


def _summarize_refreshed_providers(successes: list[JsonObject]) -> tuple[int, int]:
    """Collapse per-connection refresh results to ``(providers, models)``.

    ``_refresh_provider_connections`` emits one entry per refreshed *connection*,
    and each entry's ``model_count`` is the size of the provider's full catalog
    after that connection merged into it — not just the models that connection
    contributed. Counting entries would overstate the provider total for a
    provider with several connections, and summing every ``model_count`` would
    count the shared catalog once per connection. Both collapse to one value per
    provider: the provider total is the number of distinct providers, and a
    provider's model total is the size it reported on its last (so most
    complete) write, which is what the catalog file actually holds.
    """

    per_provider: dict[str, int] = {}
    for entry in successes:
        provider_id = entry.get("provider_id")
        if not isinstance(provider_id, str):
            continue
        per_provider[provider_id] = _model_count(entry)
    return len(per_provider), sum(per_provider.values())


@asynccontextmanager
async def _runtime_provider_credential(
    runtime: ProviderRuntime,
    provider_id: str,
    connection_id: str,
    connection: ConnectionConfig,
) -> AsyncIterator[str | TokenGetter]:
    if connection.type != "oauth" or connection.oauth is None:
        yield str(
            await _CATALOG_READ_WORKERS.run(
                runtime._credentials.get_credentials, provider_id, connection_id
            )
        )
        return

    token_store = runtime._token_store
    _local_connection_id, requested_account_id = split_connection_id(provider_id, connection_id)
    account_id = await _CATALOG_READ_WORKERS.run(
        runtime._credentials.resolve_account_id,
        provider_id,
        connection.id,
        account_id=requested_account_id,
    )
    getter = OAuthTokenGetter(
        token_store, Account(provider_id, connection.id, account_id), connection.oauth
    )
    # Resolve expiring tokens before callers read token-derived endpoint metadata,
    # but keep recovery and per-attempt credential access alive through discovery.
    await getter()
    yield getter


def _connection_models_endpoint(connection: Any, provider: Any) -> str | None:
    """Return the Connection's effective catalog endpoint, or ``None`` when absent."""

    return getattr(connection, "models_endpoint", None) or getattr(
        provider, "models_endpoint", None
    )


@asynccontextmanager
async def discovery_credential(
    runtime: ProviderRuntime,
    provider_id: str,
    connection_id: str,
    connection: ConnectionConfig,
) -> AsyncIterator[tuple[ConnectionConfig, str | TokenGetter]]:
    """Yield the Connection and credential that Model catalog requests use.

    A public catalog (``catalog_requires_credentials: false``) uses an empty
    credential. Otherwise the credential resolves through
    :func:`_runtime_provider_credential`, and GitHub Copilot discovery then
    targets the Account's exchanged API endpoint read from current token data.
    """

    credential_context = (
        _runtime_provider_credential(runtime, provider_id, connection_id, connection)
        if getattr(connection, "catalog_requires_credentials", True)
        else nullcontext("")
    )
    async with credential_context as credential_value:
        yield (
            await _CATALOG_READ_WORKERS.run(
                _copilot_discovery_connection, runtime, provider_id, connection_id, connection
            ),
            credential_value,
        )


def _copilot_discovery_connection(
    runtime: ProviderRuntime,
    provider_id: str,
    connection_id: str,
    connection: ConnectionConfig,
) -> ConnectionConfig:
    if provider_id != "github-copilot":
        return connection
    token_extra = runtime.get_connection_token_extra(ConnectionRef(provider_id, connection_id))
    copilot_endpoint = token_extra.get(COPILOT_API_ENDPOINT_EXTRA_KEY)
    return replace(connection, base_url=copilot_endpoint) if copilot_endpoint else connection
