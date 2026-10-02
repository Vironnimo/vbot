"""The real Provider Runtime of a data directory, with a fresh learned-facts store.

Verification must judge the wire profile data, not facts an earlier session
learned, so the Adapter is rebound to profiles that use an empty in-memory
observation store. Whatever the checks teach it is reported, never persisted.
The profile data is what the Runtime uses: the bundled files plus the ``wire``
blocks of the data directory's Custom Providers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from core.models.models import Model, ModelRegistry
from core.providers.adapter import ProviderAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import ProviderConfig, ProviderRegistry
from core.providers.runtime import ConnectionRef, ProviderRuntime
from core.providers.token_store import TokenStore
from core.providers.wire_observations import WireObservations
from core.providers.wire_profiles import (
    WireProfiles,
    log_wire_profile_issue,
    wire_profile_files,
)
from core.storage.storage import StorageManager
from core.utils.config import read_env_file

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESOURCES_DIR = PROJECT_ROOT / "resources"


@dataclass(frozen=True)
class VerificationTarget:
    """One authenticated Adapter and the learned facts it records during checks."""

    provider_id: str
    connection_id: str
    model_id: str
    model: Model | None
    adapter: ProviderAdapter
    observations: WireObservations
    storage: StorageManager
    custom: bool
    """Whether the Provider is a Custom Provider (its profile data lives in Settings)."""


def open_target(
    data_dir: Path,
    provider_id: str,
    model_id: str,
    connection_id: str | None,
) -> VerificationTarget:
    """Build the production Adapter for one Model on one Connection of ``data_dir``."""

    storage = StorageManager(data_dir=data_dir, resources_dir=RESOURCES_DIR)
    custom_providers = storage.load_custom_providers_settings()
    providers = ProviderRegistry.load(RESOURCES_DIR, custom_providers=custom_providers)
    models = ModelRegistry.load(
        RESOURCES_DIR,
        runtime_models_dir=storage.layout.models,
        custom_providers=custom_providers,
    )
    token_store = TokenStore(data_dir)
    env_path = data_dir / ".env"
    credentials = ProviderCredentialResolver(
        providers,
        fallback_credentials=read_env_file(env_path) if env_path.is_file() else {},
        process_env=dict(os.environ),
        token_store=token_store,
        enabled_overrides_loader=lambda: {},
    )
    runtime = ProviderRuntime(
        providers=providers,
        models=models,
        credentials=credentials,
        token_store=token_store,
        storage=storage,
        resources_path=RESOURCES_DIR,
        logger=logging.getLogger("vbot.wire_verify"),
        custom_providers=custom_providers,
    )
    config = providers.get(provider_id)
    connection = connection_id or _usable_connection(credentials, provider_id, config)
    adapter = runtime.get_adapter(ConnectionRef(provider_id, f"{provider_id}:{connection}"))

    bare_model = model_id.split("::", 1)[0]

    def resolve_model(_provider_id: str, requested: str) -> Model | None:
        try:
            return models.get(provider_id, requested)
        except KeyError:
            return None

    observations = WireObservations(None, save_delay=None)
    profiles = WireProfiles(
        files=wire_profile_files(custom_providers),
        protocol_support=lambda _provider_id: type(adapter).WIRE_PROTOCOLS,
        model_resolver=resolve_model,
        report=log_wire_profile_issue,
        observations=observations,
    )
    adapter.bind_wire_profiles(profiles.bind(provider_id, connection))
    return VerificationTarget(
        provider_id=provider_id,
        connection_id=connection,
        model_id=model_id,
        model=resolve_model(provider_id, bare_model),
        adapter=adapter,
        observations=observations,
        storage=storage,
        custom=config.custom,
    )


def _usable_connection(
    credentials: ProviderCredentialResolver, provider_id: str, config: ProviderConfig
) -> str:
    connections = config.connections
    usable = [
        connection.id
        for connection in connections
        if credentials.has_credentials(provider_id, f"{provider_id}:{connection.id}")
    ]
    if len(usable) == 1:
        return usable[0]
    choices = ", ".join(connection.id for connection in connections)
    if not usable:
        raise SystemExit(f"no Connection of {provider_id!r} has credentials; choices: {choices}")
    raise SystemExit(
        f"several usable Connections for {provider_id!r}; pass --connection: {choices}"
    )
