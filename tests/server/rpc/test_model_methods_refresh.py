"""``model.refresh_db``: target roots, Connection iteration, failures and OAuth credentials."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from shutil import copy2
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from core.database import write_bootstrap_marker
from core.models.database import read_model_database_manifest, write_model_database_manifest
from core.models.discovery import ModelDiscoveryError
from core.providers.accounts import ConnectionRef
from core.providers.credentials import ProviderCredentialResolver
from core.providers.errors import NetworkError, ProviderAuthError
from core.providers.providers import ProviderRegistry
from core.providers.token_store import OAuthToken
from core.runtime import Runtime
from core.utils.config import Config
from core.utils.retry import MAX_RETRIES, retry_async
from server.events import ServerEventBus
from server.rpc import model_methods
from tests.server.rpc.oauth_provider_test_support import (
    api_key_connection,
    copilot_provider,
    oauth_connection,
    oauth_provider_state,
)
from tests.server.rpc_test_support import (
    FAKE_REFRESH_MODEL_CALLS,
    FAKE_REFRESH_MODEL_KWARGS,
    FAKE_REFRESH_MODEL_PROVIDER_IDS,
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    fake_refresh_models,
    make_state,
    openrouter_provider,
    resource_changes,
    rpc_error,
    rpc_result,
)

__all__ = ["_no_models_dev_fetch"]

_REPO_RESOURCES = Path(__file__).resolve().parents[3] / "resources"
_FETCHED_AT = "2026-05-08T19:08:00+00:00"
# Stands for the serving checkout's resources directory in parametrized params.
_SERVING_RESOURCES = "<serving resources>"


@pytest.fixture
def fake_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route discovery to ``fake_refresh_models`` and start from empty call records."""

    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)


def _api_key_provider(provider_id: str, credential_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=provider_id,
        name=provider_id,
        adapter="openai_compatible",
        base_url=f"https://{provider_id}.example/v1",
        defaults={},
        extra_headers={},
        models_endpoint="/models",
        connections=[
            SimpleNamespace(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=SimpleNamespace(credential_key=credential_key),
            )
        ],
    )


def _custom_provider(provider_id: str, credential_key: str) -> SimpleNamespace:
    provider = _api_key_provider(provider_id, credential_key)
    provider.custom = True
    return provider


def _serve_resources_from(state: SimpleNamespace, resources_dir: Path) -> None:
    """Point the stub Runtime's system Model DB at a scratch checkout."""

    state.runtime.storage.resources_dir = resources_dir


def _multi_connection_openai() -> SimpleNamespace:
    """OpenAI with two credentialed catalog Connections and one without a key."""

    def connection(connection_id: str, credential_key: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=connection_id,
            type="api_key",
            label=connection_id,
            base_url="https://api.openai.com/v1",
            models_endpoint="/v1/models",
            auth=SimpleNamespace(credential_key=credential_key),
        )

    return SimpleNamespace(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        defaults={},
        extra_headers={},
        models_endpoint=None,
        connections=[
            connection("api-key", "OPENAI_PRIMARY_KEY"),
            connection("secondary", "OPENAI_SECONDARY_KEY"),
            connection("missing-creds", "OPENAI_MISSING_KEY"),
        ],
    )


# ---------------------------------------------------------------------------
# Refresh targets and registry reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
@pytest.mark.parametrize("target", ["runtime", "system"])
async def test_model_refresh_uses_started_runtime_storage_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, target: str
) -> None:
    """A runtime refresh copies the complete system DB (overrides included) into
    the runtime root; a system refresh writes only the serving checkout. Both
    count the entries Load ignores and log each of them once."""

    resources_dir = tmp_path / "configured-resources"
    (resources_dir / "providers").mkdir(parents=True)
    copy2(_REPO_RESOURCES / "providers/openrouter.json", resources_dir / "providers")
    (resources_dir / "models").mkdir()
    override_text = '{"models": {"retired-model": {"name": "Retired"}}}\n'
    (resources_dir / "models/openrouter.overrides.json").write_text(override_text, encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_bootstrap_marker(data_dir)
    monkeypatch.setenv("RESOURCES_PATH", str(resources_dir))
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    runtime = Runtime(Config(data_dir=data_dir), safe_startup_mode="test")
    runtime.start()
    try:
        registry = runtime.models
        state = SimpleNamespace(runtime=runtime, event_bus=ServerEventBus())
        params = {"provider_id": "openrouter", "target": target}
        if target == "system":
            params["expected_resources_dir"] = str(resources_dir)

        # The started Runtime's LogManager stops propagation to the root logger.
        models_logger = logging.getLogger("vbot.models")
        models_logger.addHandler(caplog.handler)
        caplog.clear()
        try:
            with caplog.at_level(logging.WARNING, logger=models_logger.name):
                result = await rpc_result(state, "model.refresh_db", **params)
        finally:
            models_logger.removeHandler(caplog.handler)

        assert result["invalid_entry_count"] == 1
        assert [
            "openrouter/retired-model" in record.getMessage()
            for record in caplog.records
            if record.name == models_logger.name
        ] == [True]
        assert runtime.models is registry
        assert registry.get("openrouter", "fresh-model").name == "Fresh Model"
        destination = (
            runtime.storage.layout.models if target == "runtime" else resources_dir / "models"
        )
        other = resources_dir / "models" if target == "runtime" else runtime.storage.layout.models
        assert (destination / "openrouter.json").is_file()
        assert not (other / "openrouter.json").exists()
        assert (destination / "openrouter.overrides.json").read_text(
            encoding="utf-8"
        ) == override_text
        manifest = read_model_database_manifest(destination)
        assert manifest is not None
        assert manifest.source == target
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
async def test_provider_refresh_reloads_the_shared_registry_and_signals_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh reloads the registry in place: holders that captured it at
    construction (task-model targets, status display, Recall) see the new catalog."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    registry = state.runtime.models

    result = await rpc_result(state, "model.refresh_db", provider_id="openrouter")

    assert result == {"provider_id": "openrouter", "model_count": 1, "fetched_at": _FETCHED_AT}
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openrouter"]
    assert FAKE_REFRESH_MODEL_CALLS == ["openrouter-key"]
    assert state.runtime.models is registry
    assert registry.get("openrouter", "fresh-model").name == "Fresh Model"
    assert resource_changes(state) == [{"kind": "models"}]


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
@pytest.mark.parametrize(
    ("target", "refreshed"),
    [
        ("runtime", ["custom", "openrouter", "secondary"]),
        # The system Model DB ships with vBot; Custom Providers exist only in Settings.
        ("system", ["openrouter", "secondary"]),
    ],
)
async def test_global_refresh_covers_only_eligible_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    refreshed: list[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("MISSING_REFRESH_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("SECONDARY_API_KEY", "secondary-key")
    monkeypatch.setenv("CUSTOM_API_KEY", "custom-key")
    state = make_state(tmp_path, StubAdapter())
    resources_dir = tmp_path / "system-resources"
    _serve_resources_from(state, resources_dir)
    state.runtime.providers.add(openrouter_provider())
    state.runtime.providers.add(_api_key_provider("missing-credentials", "MISSING_REFRESH_API_KEY"))
    state.runtime.providers.add(_api_key_provider("secondary", "SECONDARY_API_KEY"))
    state.runtime.providers.add(_custom_provider("custom", "CUSTOM_API_KEY"))
    registry = state.runtime.models
    params: JsonObject = {"target": target}
    if target == "system":
        params["expected_resources_dir"] = str(resources_dir)

    result = await rpc_result(state, "model.refresh_db", **params)

    assert result == {
        "providers": [
            {"provider_id": provider_id, "model_count": 1, "fetched_at": _FETCHED_AT}
            for provider_id in refreshed
        ],
        "refreshed_count": len(refreshed),
        "model_count": len(refreshed),
        "canonical": None,
    }
    assert refreshed == FAKE_REFRESH_MODEL_PROVIDER_IDS
    assert [f"{provider_id}-key" for provider_id in refreshed] == FAKE_REFRESH_MODEL_CALLS
    assert state.runtime.models is registry
    assert registry.get("openrouter", "fresh-model").name == "Fresh Model"
    assert registry.get("secondary", "fresh-model").name == "Fresh Model"
    assert resource_changes(state) == [{"kind": "models"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("second_refresh", ["manual", "automatic"])
async def test_manual_and_local_refreshes_preserve_each_others_complete_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_refresh: str
) -> None:
    import core.models.discovery as discovery

    resources_dir = tmp_path / "resources"
    providers_dir = resources_dir / "providers"
    providers_dir.mkdir(parents=True)
    for provider_id in ("openrouter", "ollama"):
        copy2(_REPO_RESOURCES / f"providers/{provider_id}.json", providers_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_bootstrap_marker(data_dir)
    monkeypatch.setenv("RESOURCES_PATH", str(resources_dir))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    started: list[str] = []

    async def refresh(provider, credential, target_resources, **kwargs):
        started.append(provider.id)
        if provider.id == "openrouter":
            first_started.set()
            await release_first.wait()
        return await fake_refresh_models(provider, credential, target_resources, **kwargs)

    monkeypatch.setattr(model_methods, "refresh_models", refresh)
    monkeypatch.setattr(discovery, "refresh_models", refresh)
    runtime = Runtime(Config(data_dir=data_dir), safe_startup_mode="test")
    runtime.start()
    runtime.storage.set_provider_connection_enabled("ollama:local", True)
    state = SimpleNamespace(runtime=runtime, event_bus=ServerEventBus())
    tasks: list[asyncio.Task] = []
    try:
        first = asyncio.create_task(rpc_result(state, "model.refresh_db", provider_id="openrouter"))
        tasks.append(first)
        await asyncio.wait_for(first_started.wait(), timeout=5)
        second = asyncio.create_task(
            runtime.maybe_refresh_local_catalogs(force=True)
            if second_refresh == "automatic"
            else rpc_result(state, "model.refresh_db", provider_id="ollama")
        )
        tasks.append(second)
        await asyncio.sleep(0)
        assert started == ["openrouter"]
        release_first.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert started == ["openrouter", "ollama"]
        for provider_id in ("openrouter", "ollama"):
            assert (runtime.storage.layout.models / f"{provider_id}.json").is_file()
            assert runtime.models.get(provider_id, "fresh-model").name == "Fresh Model"
    finally:
        release_first.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
async def test_runtime_refresh_drops_catalogs_of_providers_that_no_longer_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted Custom Provider's catalog leaves the runtime Model DB. Catalogs of
    existing Providers, Settings-only Custom Providers included, and catalogs the
    system Model DB ships stay."""

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.providers.add(_custom_provider("custom", "CUSTOM_API_KEY"))
    state.runtime.storage.save_custom_provider_settings(
        "settings-only",
        {
            "name": "Settings Only",
            "adapter": "openai_compatible",
            "base_url": "https://settings-only.example/v1",
        },
    )
    resources_dir = tmp_path / "system-resources"
    _serve_resources_from(state, resources_dir)
    runtime_models_dir = state.runtime.storage.layout.models
    catalogs = {
        resources_dir / "models": ["shipped"],
        runtime_models_dir: ["custom", "deleted-custom", "settings-only", "shipped"],
    }
    for models_dir, provider_ids in catalogs.items():
        models_dir.mkdir(parents=True, exist_ok=True)
        for provider_id in provider_ids:
            models_dir.joinpath(f"{provider_id}.json").write_text(
                json.dumps({"provider_id": provider_id, "models": {}}), encoding="utf-8"
            )
    write_model_database_manifest(runtime_models_dir, source="runtime")

    await rpc_result(state, "model.refresh_db", provider_id="openrouter")

    assert sorted(path.name for path in runtime_models_dir.glob("*.json")) == [
        "custom.json",
        "manifest.json",
        "openrouter.json",
        "settings-only.json",
        "shipped.json",
    ]


# ---------------------------------------------------------------------------
# Connection iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
async def test_provider_refresh_fetches_every_credentialed_catalog_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_PRIMARY_KEY", "primary-key")
    monkeypatch.setenv("OPENAI_SECONDARY_KEY", "secondary-key")
    monkeypatch.delenv("OPENAI_MISSING_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(_multi_connection_openai())

    await rpc_result(state, "model.refresh_db", provider_id="openai")

    # The Connection without a credential is skipped; the others merge into one catalog.
    assert FAKE_REFRESH_MODEL_CALLS == ["primary-key", "secondary-key"]
    assert [kwargs["credential_connection"].id for kwargs in FAKE_REFRESH_MODEL_KWARGS] == [
        "api-key",
        "secondary",
    ]
    assert state.runtime.models.get("openai", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
async def test_global_refresh_counts_multi_connection_provider_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The summary is per Provider: ``model_count`` is the shared catalog size,
    not summed across the Provider's Connections."""

    monkeypatch.setenv("OPENAI_PRIMARY_KEY", "primary-key")
    monkeypatch.setenv("OPENAI_SECONDARY_KEY", "secondary-key")
    monkeypatch.delenv("OPENAI_MISSING_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(_multi_connection_openai())

    result = await rpc_result(state, "model.refresh_db")

    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openai", "openai"]
    assert result["refreshed_count"] == 1
    assert result["model_count"] == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
async def test_public_catalogs_refresh_without_credentials(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(ProviderRegistry.load(_REPO_RESOURCES).get("opencode-zen"))

    await rpc_result(state, "model.refresh_db", provider_id="opencode-zen")

    # Both the API-key and the OAuth Connection fetch the public catalog keyless.
    assert FAKE_REFRESH_MODEL_CALLS == ["", ""]
    assert {call["credential_connection"].id for call in FAKE_REFRESH_MODEL_KWARGS} == {
        "api-key",
        "account",
    }


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def _openai_with_failing_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: set[str]
) -> tuple[Any, dict[str, int]]:
    """OpenAI whose ``failing`` Connections are unreachable; ``secondary`` auto-refreshes.

    Returns the state and the discovery attempts each failing Connection made.
    """

    attempts: dict[str, int] = {}

    async def selective_refresh_models(
        provider_config: Any, credential_value: str, resources_dir: Path, **kwargs: Any
    ) -> JsonObject:
        connection_id = kwargs["credential_connection"].id
        if connection_id in failing:
            # Like discovery, probe through the shared transient-failure retry.
            async def probe() -> None:
                attempts[connection_id] = attempts.get(connection_id, 0) + 1
                raise NetworkError("connection refused")

            try:
                await retry_async(probe)
            except NetworkError as error:
                raise ModelDiscoveryError(
                    f"Model discovery failed for {connection_id}: {error}"
                ) from error
        return await fake_refresh_models(provider_config, credential_value, resources_dir, **kwargs)

    async def skip_backoff(_delay: float) -> None:
        return None

    monkeypatch.setenv("OPENAI_PRIMARY_KEY", "primary-key")
    monkeypatch.setenv("OPENAI_SECONDARY_KEY", "secondary-key")
    monkeypatch.delenv("OPENAI_MISSING_KEY", raising=False)
    monkeypatch.setattr(model_methods, "refresh_models", selective_refresh_models)
    monkeypatch.setattr("core.utils.retry._sleep", skip_backoff)
    state = make_state(tmp_path, StubAdapter())
    provider = _multi_connection_openai()
    provider.connections[1].auto_refresh = True
    state.runtime.providers.add(provider)
    return state, attempts


@pytest.mark.asyncio
async def test_provider_refresh_reports_a_failed_connection_beside_healthy_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _attempts = _openai_with_failing_connections(tmp_path, monkeypatch, {"secondary"})

    result = await rpc_result(state, "model.refresh_db", provider_id="openai")

    assert result["provider_id"] == "openai"
    assert result["model_count"] == 1
    assert result["errors"] == [
        {
            "provider_id": "openai",
            "connection_id": "openai:secondary",
            "error": "Model discovery failed for secondary: connection refused",
        }
    ]
    assert state.runtime.models.get("openai", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_provider_refresh_fails_with_the_first_error_when_every_connection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, attempts = _openai_with_failing_connections(
        tmp_path, monkeypatch, {"api-key", "secondary"}
    )

    error = await rpc_error(state, "model.refresh_db", provider_id="openai")

    assert error == {
        "code": "domain_error",
        "message": "Model discovery failed for api-key: connection refused",
    }
    assert resource_changes(state) == []
    # An auto-refresh (local) Connection is probed once; others keep their retries.
    assert attempts == {"api-key": MAX_RETRIES + 1, "secondary": 1}


@pytest.mark.asyncio
async def test_global_refresh_continues_when_one_provider_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def selective_refresh_models(
        provider_config: Any, credential_value: str, resources_dir: Path, **kwargs: Any
    ) -> JsonObject:
        if provider_config.id == "openrouter":
            raise ModelDiscoveryError("Model discovery failed for 'openrouter': 503")
        return await fake_refresh_models(provider_config, credential_value, resources_dir, **kwargs)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("SECONDARY_API_KEY", "secondary-key")
    monkeypatch.setattr(model_methods, "refresh_models", selective_refresh_models)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.providers.add(_api_key_provider("secondary", "SECONDARY_API_KEY"))

    result = await rpc_result(state, "model.refresh_db")

    assert result["providers"] == [
        {"provider_id": "secondary", "model_count": 1, "fetched_at": _FETCHED_AT},
    ]
    assert result["refreshed_count"] == 1
    assert result["model_count"] == 1
    assert result["errors"] == [
        {
            "provider_id": "openrouter",
            "connection_id": "openrouter:api-key",
            "error": "Model discovery failed for 'openrouter': 503",
        }
    ]
    assert state.runtime.models.get("secondary", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
@pytest.mark.parametrize(
    ("params", "code", "named"),
    [
        ({"provider_id": "openrouter", "extra": True}, "invalid_request", "extra"),
        ({"target": "catalog"}, "invalid_request", "target must be 'runtime' or 'system'"),
        (
            {"expected_resources_dir": "C:/resources"},
            "invalid_request",
            "only valid for a system Model DB refresh",
        ),
        (
            {"target": "system", "expected_resources_dir": "C:/other-resources"},
            "invalid_request",
            "the serving checkout does not match",
        ),
        ({"provider_id": "missing"}, "domain_error", "unknown provider: missing"),
        (
            {
                "provider_id": "custom",
                "target": "system",
                "expected_resources_dir": _SERVING_RESOURCES,
            },
            "domain_error",
            "Custom Provider 'custom' can only be refreshed into the runtime Model DB",
        ),
        # Neither the Provider nor its Connection declares a catalog endpoint.
        ({"provider_id": "openai"}, "domain_error", "does not support model refresh"),
        (
            {"provider_id": "openrouter"},
            "domain_error",
            "Provider credentials not found for provider 'openrouter'",
        ),
    ],
)
async def test_refused_model_refreshes_change_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    params: JsonObject,
    code: str,
    named: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("CUSTOM_API_KEY", "custom-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    resources_dir = tmp_path / "system-resources"
    _serve_resources_from(state, resources_dir)
    state.runtime.providers.add(openrouter_provider())
    state.runtime.providers.add(_custom_provider("custom", "CUSTOM_API_KEY"))
    if params.get("expected_resources_dir") == _SERVING_RESOURCES:
        params = {**params, "expected_resources_dir": str(resources_dir)}

    error = await rpc_error(state, "model.refresh_db", **params)

    assert error["code"] == code
    assert named in error["message"]
    assert FAKE_REFRESH_MODEL_CALLS == []
    assert resource_changes(state) == []


# ---------------------------------------------------------------------------
# OAuth credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", ["default", "work"])
async def test_model_refresh_db_uses_oauth_token_getter_for_fresh_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account_id: str,
) -> None:
    provider = copilot_provider(oauth_connection(), models_endpoint="/models")
    state = oauth_provider_state(tmp_path, provider)
    state.runtime.token_store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="stale-token", extra={"github_oauth_token": "github-secret"}),
        account_id=account_id,
    )
    refreshed: dict[str, Any] = {}
    state.runtime.provider_credentials = ProviderCredentialResolver(
        ProviderRegistry({provider.id: provider}),
        process_env={},
        token_store=state.runtime.token_store,
    )

    def reject_static_credential(*_args: Any) -> str:
        raise AssertionError("OAuth discovery must use the live getter")

    monkeypatch.setattr(
        state.runtime.provider_credentials, "get_credentials", reject_static_credential
    )

    def token_extra(connection: ConnectionRef) -> dict[str, str]:
        assert connection == ConnectionRef("github-copilot", "github-copilot:oauth")
        assert "getter_args" in refreshed
        return {"copilot_api_endpoint": "https://api.enterprise.githubcopilot.com"}

    state.runtime.get_connection_token_extra = token_extra

    class StubOAuthTokenGetter:
        def __init__(
            self,
            token_store: Any,
            provider_id: str,
            connection_id: str,
            config: Any,
            *,
            account_id: str,
        ) -> None:
            self.args = (token_store, provider_id, connection_id, config)
            refreshed["account_id"] = account_id

        async def __call__(self) -> str:
            refreshed["getter_args"] = self.args
            return "fresh-runtime-token"

    async def fake_refresh(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        assert isinstance(_args[1], StubOAuthTokenGetter)
        refreshed["credential"] = await _args[1]()
        refreshed["connection"] = _kwargs["credential_connection"]
        return {
            "provider_id": "github-copilot",
            "model_count": 0,
            "fetched_at": "2026-05-12T00:00:00+00:00",
        }

    monkeypatch.setattr("server.rpc.provider_access.OAuthTokenGetter", StubOAuthTokenGetter)
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh)

    await rpc_result(state, "model.refresh_db", provider_id="github-copilot")

    assert refreshed["credential"] == "fresh-runtime-token"
    assert refreshed["connection"].id == "oauth"
    assert refreshed["account_id"] == account_id
    assert refreshed["connection"].base_url == "https://api.enterprise.githubcopilot.com"


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "error"),
    [
        # ``ProviderError`` subclasses and ``NetworkError`` are separate bases.
        pytest.param("global", ProviderAuthError("test-auth-failure"), id="global-auth"),
        pytest.param("provider", NetworkError("test-network-failure"), id="provider-network"),
    ],
)
async def test_model_refresh_continues_after_oauth_credential_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    error: Exception,
) -> None:
    provider = copilot_provider(oauth_connection(), api_key_connection(), models_endpoint="/models")
    state = oauth_provider_state(tmp_path, provider)
    models_dir = state.runtime.storage.resources_dir / "models"
    models_dir.mkdir(parents=True)
    old_model = {
        "name": "Old OAuth Model",
        "connections": ["oauth"],
        "capabilities": {"reasoning": {"supported": False}},
    }
    (models_dir / "github-copilot.json").write_text(
        json.dumps({"provider_id": provider.id, "models": {"old-model": old_model}}),
        encoding="utf-8",
    )

    async def failing_getter(_self: Any) -> str:
        raise error

    monkeypatch.setattr("core.providers.token_getter.OAuthTokenGetter.__call__", failing_getter)
    catalog_route = respx.get("https://api.githubcopilot.com/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "fresh-model"}]}),
    )
    params = {} if scope == "global" else {"provider_id": provider.id}

    result = await rpc_result(state, "model.refresh_db", **params)

    assert result["model_count"] == 2
    assert result["errors"] == [
        {"provider_id": provider.id, "connection_id": "github-copilot:oauth", "error": str(error)}
    ]
    assert catalog_route.call_count == 1
    assert catalog_route.calls[0].request.headers["Authorization"] == "Bearer api-key-secret"
    written = json.loads(
        (state.runtime.storage.layout.models / "github-copilot.json").read_text(encoding="utf-8")
    )
    assert written["models"]["old-model"] == old_model
    assert written["models"]["fresh-model"]["connections"] == ["api-key"]
