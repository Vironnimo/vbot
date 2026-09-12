"""Tests for rpc models refresh connections."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.models.discovery import ModelDiscoveryError
from server.rpc import (
    model_methods,
)
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    FAKE_REFRESH_MODEL_CALLS,
    FAKE_REFRESH_MODEL_KWARGS,
    FAKE_REFRESH_MODEL_PROVIDER_IDS,
    JsonObject,
    StubAdapter,
    fake_refresh_models,
    make_state,
    openrouter_provider,
    openrouter_provider_with_secondary_connection,
)
from tests.server.rpc_test_support import _no_models_dev_fetch as _no_models_dev_fetch


@pytest.mark.asyncio
async def test_model_refresh_db_passes_first_usable_connection_to_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider_with_secondary_connection())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is True
    assert FAKE_REFRESH_MODEL_CALLS == ["openrouter-key"]
    assert FAKE_REFRESH_MODEL_KWARGS[0]["credential_connection"].id == "api-key"


@pytest.mark.asyncio
async def test_model_refresh_db_iterates_every_refreshable_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider with multiple endpoint-bearing credentialed connections is
    refreshed once per connection.

    Confirms the RPC layer walks the full connection list rather than
    stopping at the first usable one. The registry is reloaded exactly
    once at the end of the call, and the merged catalog is the union of
    every connection's result (here all the same stub ``fresh-model``).
    """

    monkeypatch.setenv("OPENAI_PRIMARY_KEY", "primary-key")
    monkeypatch.setenv("OPENAI_SECONDARY_KEY", "secondary-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(
        SimpleNamespace(
            id="openai",
            name="OpenAI",
            adapter="openai",
            base_url="https://api.openai.com/v1",
            defaults={},
            extra_headers={},
            models_endpoint=None,
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_PRIMARY_KEY"),
                ),
                SimpleNamespace(
                    id="secondary",
                    type="api_key",
                    label="Secondary",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_SECONDARY_KEY"),
                ),
                SimpleNamespace(
                    id="missing-creds",
                    type="api_key",
                    label="Missing Credentials",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_MISSING_KEY"),
                ),
            ],
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openai"}},
    )

    assert response["ok"] is True, response
    # Two refreshes — the third connection has no credentials.
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openai", "openai"]
    assert sorted(FAKE_REFRESH_MODEL_CALLS) == ["primary-key", "secondary-key"]
    connection_ids = [kwargs["credential_connection"].id for kwargs in FAKE_REFRESH_MODEL_KWARGS]
    assert connection_ids == ["api-key", "secondary"]
    # The registry reloads once and the merged catalog is readable.
    refreshed_model = state.runtime.models.get("openai", "fresh-model")
    assert refreshed_model.name == "Fresh Model"


@pytest.mark.asyncio
async def test_global_refresh_counts_multi_connection_provider_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider with several connections counts as one provider, one catalog.

    The global refresh walks every connection (here two credentialed ones),
    but the summary reports per provider, not per connection: ``refreshed_count``
    is the number of distinct providers and ``model_count`` is the provider's
    catalog size counted once — not summed across its connections.
    """

    monkeypatch.setenv("OPENAI_PRIMARY_KEY", "primary-key")
    monkeypatch.setenv("OPENAI_SECONDARY_KEY", "secondary-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(
        SimpleNamespace(
            id="openai",
            name="OpenAI",
            adapter="openai",
            base_url="https://api.openai.com/v1",
            defaults={},
            extra_headers={},
            models_endpoint=None,
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_PRIMARY_KEY"),
                ),
                SimpleNamespace(
                    id="secondary",
                    type="api_key",
                    label="Secondary",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_SECONDARY_KEY"),
                ),
            ],
        )
    )

    response = await dispatch_rpc(state, {"method": "model.refresh_db"})

    assert response["ok"] is True, response
    # Both connections were refreshed...
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openai", "openai"]
    # ...yet the summary collapses them to one provider and one catalog.
    result = response["result"]
    assert result["refreshed_count"] == 1
    assert result["model_count"] == 1


@pytest.mark.asyncio
async def test_model_refresh_db_skips_connections_without_effective_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection without an effective ``models_endpoint`` is silently skipped.

    Provider-level ``models_endpoint=None`` and connection-level
    ``models_endpoint=None`` together mean there is no catalog to fetch,
    so the connection is excluded from the iteration — even when it has
    valid credentials.
    """

    monkeypatch.setenv("OPENAI_PRIMARY_KEY", "primary-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(
        SimpleNamespace(
            id="openai",
            name="OpenAI",
            adapter="openai",
            base_url="https://api.openai.com/v1",
            defaults={},
            extra_headers={},
            models_endpoint=None,
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    base_url=None,
                    models_endpoint=None,
                    auth=SimpleNamespace(credential_key="OPENAI_PRIMARY_KEY"),
                ),
            ],
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openai"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert FAKE_REFRESH_MODEL_CALLS == []


@pytest.mark.asyncio
async def test_model_refresh_db_maps_discovery_failures_to_rpc_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_refresh_models(*_args: Any, **_kwargs: Any) -> JsonObject:
        raise ModelDiscoveryError("Model discovery failed for provider 'openrouter': bad JSON")

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(model_methods, "refresh_models", failing_refresh_models)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"


@pytest.mark.asyncio
async def test_model_refresh_db_global_continues_when_one_provider_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single unreachable provider must not abort the whole global refresh.

    The healthy provider is still written and loaded into the runtime
    registry, and the failure is reported in ``errors`` instead of turning the
    whole RPC into an error.
    """

    async def selective_refresh_models(
        provider_config: Any,
        credential_value: str,
        resources_dir: Path,
        **kwargs: Any,
    ) -> JsonObject:
        if provider_config.id == "openrouter":
            raise ModelDiscoveryError(
                "Model discovery failed for provider 'openrouter': 503 upstream down"
            )
        return await fake_refresh_models(provider_config, credential_value, resources_dir, **kwargs)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("OPENROUTER_SECONDARY_API_KEY", "secondary-key")
    monkeypatch.setattr(model_methods, "refresh_models", selective_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.providers.add(
        SimpleNamespace(
            id="refreshable-secondary",
            name="Refreshable Secondary",
            adapter="openai_compatible",
            base_url="https://secondary.example/v1",
            defaults={},
            extra_headers={},
            models_endpoint="/models",
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=SimpleNamespace(credential_key="OPENROUTER_SECONDARY_API_KEY"),
                )
            ],
        )
    )

    response = await dispatch_rpc(state, {"method": "model.refresh_db"})

    assert response["ok"] is True, response
    result = response["result"]
    assert result["providers"] == [
        {
            "provider_id": "refreshable-secondary",
            "model_count": 1,
            "fetched_at": "2026-05-08T19:08:00+00:00",
        },
    ]
    assert result["refreshed_count"] == 1
    assert result["model_count"] == 1
    assert result["errors"] == [
        {
            "provider_id": "openrouter",
            "connection_id": "openrouter:api-key",
            "error": "Model discovery failed for provider 'openrouter': 503 upstream down",
        }
    ]
    # The healthy provider is loaded into the runtime registry despite the failure.
    assert state.runtime.models.get("refreshable-secondary", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_single_provider_reports_failed_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failing connection must not sink a provider's other connection.

    The healthy connection still refreshes and reloads; the failed one is
    reported in ``errors`` on the single-provider result.
    """

    async def selective_refresh_models(
        provider_config: Any,
        credential_value: str,
        resources_dir: Path,
        **kwargs: Any,
    ) -> JsonObject:
        if kwargs["credential_connection"].id == "secondary":
            raise ModelDiscoveryError(
                "Model discovery failed for provider 'openai': 401 unauthorized"
            )
        return await fake_refresh_models(provider_config, credential_value, resources_dir, **kwargs)

    monkeypatch.setenv("OPENAI_PRIMARY_KEY", "primary-key")
    monkeypatch.setenv("OPENAI_SECONDARY_KEY", "secondary-key")
    monkeypatch.setattr(model_methods, "refresh_models", selective_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(
        SimpleNamespace(
            id="openai",
            name="OpenAI",
            adapter="openai",
            base_url="https://api.openai.com/v1",
            defaults={},
            extra_headers={},
            models_endpoint=None,
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_PRIMARY_KEY"),
                ),
                SimpleNamespace(
                    id="secondary",
                    type="api_key",
                    label="Secondary",
                    base_url="https://api.openai.com/v1",
                    models_endpoint="/v1/models",
                    auth=SimpleNamespace(credential_key="OPENAI_SECONDARY_KEY"),
                ),
            ],
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openai"}},
    )

    assert response["ok"] is True, response
    result = response["result"]
    assert result["provider_id"] == "openai"
    assert result["model_count"] == 1
    assert result["errors"] == [
        {
            "provider_id": "openai",
            "connection_id": "openai:secondary",
            "error": "Model discovery failed for provider 'openai': 401 unauthorized",
        }
    ]
    assert state.runtime.models.get("openai", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_rejects_provider_without_models_endpoint(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openai"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"


@pytest.mark.asyncio
async def test_model_refresh_db_rejects_missing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "openrouter" in response["error"]["message"]


@pytest.mark.asyncio
async def test_model_refresh_db_fetches_public_catalog_without_inference_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(
        SimpleNamespace(
            id="ollama-cloud",
            name="Ollama Cloud",
            adapter="ollama_cloud",
            base_url="https://ollama.com",
            defaults={},
            extra_headers={},
            models_endpoint="/api/tags",
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API key",
                    mode="cloud",
                    models_endpoint=None,
                    base_url=None,
                    catalog_requires_credentials=False,
                    auth=SimpleNamespace(credential_key="OLLAMA_API_KEY"),
                )
            ],
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "ollama-cloud"}},
    )

    assert response["ok"] is True
    assert FAKE_REFRESH_MODEL_CALLS == [""]
    assert FAKE_REFRESH_MODEL_KWARGS[0]["credential_connection"].id == "api-key"


@pytest.mark.asyncio
async def test_model_refresh_db_rejects_unknown_provider(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "missing"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "missing" in response["error"]["message"]


@pytest.mark.asyncio
async def test_model_refresh_db_rejects_unsupported_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter", "extra": True}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "extra" in response["error"]["message"]
