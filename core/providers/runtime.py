"""Provider-owned adapter construction and live catalog refresh."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from core.debug import DebugTraceStore, ProviderDebugRecorder
from core.models.database import begin_runtime_model_database_refresh
from core.models.models import Model, ModelRegistry
from core.providers.accounts import DEFAULT_ACCOUNT_ID, ConnectionRef, split_connection_id
from core.providers.adapter import ModelLookup, ProviderAdapter
from core.providers.anthropic import AnthropicAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.kimi import KimiAdapter
from core.providers.lmstudio import LMStudioAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.mistral import MistralAdapter
from core.providers.nous import NousAdapter
from core.providers.ollama import OllamaAdapter, OllamaCloudAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import (
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
    model_is_local,
    resolve_effective_context_window,
)
from core.providers.reasoning import ReasoningIntent, ReasoningReplayPolicy
from core.providers.stepfun import StepFunAdapter
from core.providers.token_getter import (
    COPILOT_API_ENDPOINT_EXTRA_KEY,
    OAuthTokenGetter,
    StaticTokenGetter,
    TokenGetter,
)
from core.providers.token_store import TokenStore
from core.providers.xai import XAIAdapter
from core.storage import StorageManager
from core.utils.errors import ConfigError, StorageError

LOCAL_CATALOG_REFRESH_TTL_SECONDS = 30.0

ADAPTER_TYPES: dict[str, type[ProviderAdapter]] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "openai": OpenAIAdapter,
    "openrouter": OpenRouterAdapter,
    "kimi": KimiAdapter,
    "minimax": MiniMaxAdapter,
    "mistral": MistralAdapter,
    "nous": NousAdapter,
    "stepfun": StepFunAdapter,
    "opencode_go": OpenCodeGoAdapter,
    "opencode_zen": OpenCodeZenAdapter,
    "github_copilot": GitHubCopilotAdapter,
    "anthropic": AnthropicAdapter,
    "ollama": OllamaAdapter,
    "ollama_cloud": OllamaCloudAdapter,
    "lmstudio": LMStudioAdapter,
    "xai": XAIAdapter,
}


class ProviderRuntime:
    """Own Provider adapter creation and mutable local catalog state end to end."""

    def __init__(
        self,
        *,
        providers: ProviderRegistry,
        models: ModelRegistry,
        credentials: ProviderCredentialResolver,
        token_store: TokenStore,
        storage: StorageManager,
        resources_path: Path,
        logger: Any,
    ) -> None:
        self._providers = providers
        self._models = models
        self._credentials = credentials
        self._token_store = token_store
        self._storage = storage
        self._resources_path = resources_path
        self._logger = logger
        self._catalog_refresh_lock: asyncio.Lock | None = None
        self.refresh_at: float | None = None
        self._connection_reachability: dict[str, bool] = {}

    def rebind(
        self,
        *,
        providers: ProviderRegistry,
        models: ModelRegistry,
        credentials: Any,
        token_store: TokenStore,
        storage: StorageManager,
        logger: Any,
    ) -> None:
        """Refresh stable facade references after an in-place Runtime reconfiguration."""
        self._providers = providers
        self._models = models
        self._credentials = credentials
        self._token_store = token_store
        self._storage = storage
        self._logger = logger

    def get_adapter(self, connection: ConnectionRef) -> ProviderAdapter:
        provider_id = connection.provider_id
        connection_id = connection.connection_id
        provider_config = self._providers.get(provider_id)
        replay_override = self._models.provider_reasoning_replay(provider_id)
        if replay_override is not None:
            provider_config = replace(
                provider_config,
                reasoning_replay=cast(ReasoningReplayPolicy, replay_override),
            )
        connection_config, account_id = self._connection_config(
            provider_config,
            connection_id,
        )
        if not self._credentials.is_connection_enabled(provider_id, connection_id):
            raise ConfigError(
                f"Provider connection '{provider_id}:{connection_config.id}' is disabled — "
                "enable it in Settings → Providers or via the provider CLI"
            )
        token_getter = self._token_getter(connection, connection_config, account_id)
        adapter_class = ADAPTER_TYPES.get(provider_config.adapter)
        if adapter_class is None:
            raise ConfigError(
                f"Unknown adapter type '{provider_config.adapter}' for provider '{provider_id}'"
            )

        extra_kwargs: dict[str, Any] = {}
        if adapter_class in (OllamaAdapter, LMStudioAdapter):
            extra_kwargs["local_context_resolver"] = self._local_context_resolver(provider_id)
        if adapter_class is OpenRouterAdapter:
            extra_kwargs["routing"] = self._storage.load_openrouter_routing_settings()

        base_url = connection_config.base_url
        if adapter_class is GitHubCopilotAdapter:
            copilot_endpoint = self.get_connection_token_extra(connection).get(
                COPILOT_API_ENDPOINT_EXTRA_KEY
            )
            if copilot_endpoint:
                base_url = copilot_endpoint

        adapter = cast(Any, adapter_class)(
            provider_config,
            token_getter,
            base_url,
            connection_config.auth,
            model_lookup=self._model_lookup(provider_id),
            debug_recorder=self._debug_recorder(),
            connection_mode=connection_config.mode,
            **extra_kwargs,
        )
        return cast(ProviderAdapter, adapter)

    def get_connection_token_getter(self, connection: ConnectionRef) -> TokenGetter:
        provider_config = self._providers.get(connection.provider_id)
        connection_config, account_id = self._connection_config(
            provider_config,
            connection.connection_id,
        )
        return self._token_getter(connection, connection_config, account_id)

    def get_connection_token_extra(self, connection: ConnectionRef) -> Mapping[str, str]:
        provider_config = self._providers.get(connection.provider_id)
        connection_config, account_id = self._connection_config(
            provider_config,
            connection.connection_id,
        )
        resolved_account_id = account_id
        if resolved_account_id is None:
            try:
                resolved_account_id = self._credentials.resolve_account_id(
                    connection.provider_id,
                    connection_config.id,
                )
            except ConfigError:
                resolved_account_id = DEFAULT_ACCOUNT_ID
        token = self._token_store.load(
            connection.provider_id,
            connection_config.id,
            account_id=resolved_account_id,
        )
        return {} if token is None else dict(token.extra)

    def describe_reasoning_render(
        self,
        provider_id: str,
        model_id: str,
        effort: str | None,
    ) -> ReasoningIntent | None:
        try:
            provider_config = self._providers.get(provider_id)
        except KeyError:
            return None
        adapter_class = ADAPTER_TYPES.get(provider_config.adapter)
        if adapter_class is None:
            return None
        return adapter_class.describe_reasoning_render(
            model_lookup=self._model_lookup(provider_id),
            provider_config=provider_config,
            model_id=model_id,
            effort=effort,
        )

    async def maybe_refresh_local_catalogs(self, *, force: bool = False) -> None:
        if self._catalog_refresh_lock is None:
            self._catalog_refresh_lock = asyncio.Lock()
        async with self._catalog_refresh_lock:
            now = time.monotonic()
            if (
                not force
                and self.refresh_at is not None
                and now - self.refresh_at < LOCAL_CATALOG_REFRESH_TTL_SECONDS
            ):
                return
            targets = self._auto_refresh_targets()
            if not targets:
                return
            self.refresh_at = now

            from core.models.discovery import ModelDiscoveryError, refresh_models

            database_refresh = None
            refresh_resources_dir = None
            refreshed_any = False
            try:
                database_refresh = begin_runtime_model_database_refresh(
                    self._resources_path,
                    self._storage.data_dir,
                )
                refresh_resources_dir = database_refresh.resources_dir
                for provider_id, provider, connection in targets:
                    connection_id = f"{provider_id}:{connection.id}"
                    try:
                        credential_value = self._credentials.get_credentials(
                            provider_id,
                            connection_id,
                        )
                    except ConfigError as error:
                        self._logger.debug(
                            "Skipping local catalog refresh for %s: %s",
                            connection_id,
                            error,
                        )
                        continue
                    try:
                        await refresh_models(
                            provider,
                            credential_value,
                            refresh_resources_dir,
                            credential_connection=connection,
                        )
                    except ModelDiscoveryError as error:
                        previous = self._connection_reachability.get(connection_id)
                        self._connection_reachability[connection_id] = False
                        if previous is True:
                            self._logger.warning(
                                "Local provider connection became unreachable "
                                "(provider=%s connection=%s): %s",
                                provider_id,
                                connection.id,
                                error,
                            )
                        else:
                            self._logger.debug(
                                "Local catalog refresh failed for %s: %s",
                                connection_id,
                                error,
                            )
                        continue
                    previous = self._connection_reachability.get(connection_id)
                    self._connection_reachability[connection_id] = True
                    if previous is False:
                        self._logger.info(
                            "Local provider connection recovered (provider=%s connection=%s)",
                            provider_id,
                            connection.id,
                        )
                    refreshed_any = True

                if refreshed_any:
                    ModelRegistry.invalidate(refresh_resources_dir)
                    ModelRegistry.load(refresh_resources_dir)
                    ModelRegistry.invalidate(refresh_resources_dir)
                    database_refresh.commit()
                    self._models.reload(
                        self._resources_path,
                        runtime_models_dir=self._storage.layout.models,
                        custom_providers=self._storage.load_custom_providers_settings(),
                    )
            except Exception as error:
                self._logger.warning("Local catalog refresh could not be published: %s", error)
            finally:
                if refresh_resources_dir is not None:
                    ModelRegistry.invalidate(refresh_resources_dir)
                if database_refresh is not None:
                    database_refresh.discard()

    def connection_reachability(self, connection_id: str) -> bool | None:
        return self._connection_reachability.get(connection_id)

    def local_context_windows(self) -> Mapping[str, Any]:
        try:
            windows = self._storage.load_local_models_settings()["context_windows"]
        except StorageError as error:
            self._logger.warning("Failed to load local-model context windows: %s", error)
            return {}
        return cast(Mapping[str, Any], windows)

    def has_provider_credentials(self, provider_id: str) -> bool:
        return self._credentials.has_credentials(provider_id)

    def get_provider_credentials(self, provider_id: str) -> str:
        return self._credentials.get_credentials(provider_id)

    def get_model(self, provider_id: str, model_id: str) -> Model:
        return self._models.get(provider_id, model_id)

    def _auto_refresh_targets(self) -> list[tuple[str, ProviderConfig, ConnectionConfig]]:
        targets: list[tuple[str, ProviderConfig, ConnectionConfig]] = []
        for provider_id in self._providers.list_ids():
            provider = self._providers.get(provider_id)
            for connection in provider.connections:
                if not connection.auto_refresh:
                    continue
                if not (connection.models_endpoint or provider.models_endpoint):
                    continue
                if not self._credentials.is_usable(
                    provider_id,
                    f"{provider_id}:{connection.id}",
                ):
                    continue
                targets.append((provider_id, provider, connection))
        return targets

    def _debug_recorder(self) -> ProviderDebugRecorder | None:
        debug_settings = self._storage.load_debug_settings()
        if not debug_settings.get("enabled", False):
            return None
        trace_limit = debug_settings.get("trace_limit", 50)
        return ProviderDebugRecorder(
            store=DebugTraceStore(self._storage.data_dir, trace_limit=trace_limit)
        )

    def _model_lookup(self, provider_id: str) -> ModelLookup:
        def lookup(model_id: str) -> Model | None:
            try:
                return self._models.get(provider_id, model_id)
            except KeyError:
                return None

        return lookup

    def _local_context_resolver(self, provider_id: str) -> Callable[[str], int | None]:
        def resolve(model_id: str) -> int | None:
            bare_model_id = model_id.split("::", 1)[0]
            try:
                model = self._models.get(provider_id, bare_model_id)
            except (KeyError, AttributeError):
                return None
            if not model_is_local(model.metadata):
                return None
            try:
                provider_config = self._providers.get(provider_id)
            except (KeyError, AttributeError):
                provider_config = None
            return resolve_effective_context_window(
                model.context_window,
                provider_config,
                model_metadata=model.metadata,
                model_key=f"{provider_id}/{bare_model_id}",
                local_context_windows=self.local_context_windows(),
            )

        return resolve

    def _token_getter(
        self,
        connection: ConnectionRef,
        connection_config: ConnectionConfig,
        account_id: str | None,
    ) -> TokenGetter:
        provider_id = connection.provider_id
        if connection_config.type == "none":
            return StaticTokenGetter("")
        if connection_config.type == "api_key":
            return StaticTokenGetter(
                self._credentials.get_credentials(provider_id, connection.connection_id)
            )
        if connection_config.type == "oauth":
            if connection_config.oauth is None:
                return StaticTokenGetter(
                    self._credentials.get_credentials(provider_id, connection.connection_id)
                )
            resolved_account_id = account_id
            if resolved_account_id is None:
                resolved_account_id = self._credentials.resolve_account_id(
                    provider_id,
                    connection_config.id,
                )
            return OAuthTokenGetter(
                self._token_store,
                provider_id,
                connection_config.id,
                connection_config.oauth,
                account_id=resolved_account_id,
            )
        raise ConfigError(
            f"Unknown connection type '{connection_config.type}' for provider '{provider_id}' "
            f"connection '{connection_config.id}'"
        )

    @staticmethod
    def _connection_config(
        provider_config: ProviderConfig,
        connection_id: str,
    ) -> tuple[ConnectionConfig, str | None]:
        local_connection_id, account_id = split_connection_id(
            provider_config.id,
            connection_id,
        )
        try:
            return provider_config.get_connection(local_connection_id), account_id
        except KeyError as error:
            raise ConfigError(
                f"Unknown connection id '{connection_id}' for provider '{provider_config.id}'"
            ) from error
