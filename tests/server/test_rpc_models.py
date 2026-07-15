"""Server RPC model handlers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.models import Capabilities, Model, ModelQuery, ReasoningCapabilities
from core.models.discovery import ModelDiscoveryError
from server.rpc import (
    connection_methods,
)
from server.rpc.methods import dispatch_rpc
from server.rpc.payloads import _model_response
from server.rpc.provider_access import _provider_has_credentials
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
    openrouter_provider_with_secondary_connection,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_model_list_returns_all_models_across_providers_with_full_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
    state = make_state(tmp_path, StubAdapter())
    monkeypatch.setattr(
        state.runtime.providers,
        "list_ids",
        lambda: ["openai", "anthropic", "ollama"],
    )
    state.runtime.models._models["openai"] = [
        state.runtime.models._models["openai"][1],
        state.runtime.models._models["openai"][0],
    ]

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "models": [
                {
                    "id": "anthropic/claude-sonnet-4-20250219",
                    "provider_id": "anthropic",
                    "model_id": "claude-sonnet-4-20250219",
                    "name": "Claude Sonnet 4",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": False,
                        "reasoning": {"supported": True, "control": None, "levels": []},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": [
                            "chat",
                            "text_output",
                            "image_input",
                            "image_understanding",
                        ],
                    },
                    "context_window": 200000,
                    "effective_context_window": 200000,
                    "local": False,
                    "max_output_tokens": 64000,
                    "connections": [],
                },
                {
                    "id": "ollama/llama3.2",
                    "provider_id": "ollama",
                    "model_id": "llama3.2",
                    "name": "Llama 3.2",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": False,
                        "reasoning": {"supported": False, "control": None, "levels": []},
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 128000,
                    "effective_context_window": 128000,
                    "local": False,
                    "max_output_tokens": 8192,
                    "connections": [],
                },
                {
                    "id": "openai/gpt-4.1-mini",
                    "provider_id": "openai",
                    "model_id": "gpt-4.1-mini",
                    "name": "GPT-4.1 mini",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": False, "control": None, "levels": []},
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 128000,
                    "effective_context_window": 128000,
                    "local": False,
                    "max_output_tokens": 16000,
                    "connections": [],
                },
                {
                    "id": "openai/gpt-5.2",
                    "provider_id": "openai",
                    "model_id": "gpt-5.2",
                    "name": "GPT-5.2",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True, "control": None, "levels": []},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": [
                            "chat",
                            "text_output",
                            "image_input",
                            "image_understanding",
                        ],
                    },
                    "context_window": 256000,
                    "effective_context_window": 256000,
                    "local": False,
                    "max_output_tokens": 32000,
                    "connections": [],
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_model_list_filters_by_connection_usability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "models": [
                {
                    "id": "openai/gpt-4.1-mini",
                    "provider_id": "openai",
                    "model_id": "gpt-4.1-mini",
                    "name": "GPT-4.1 mini",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": False, "control": None, "levels": []},
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 128000,
                    "effective_context_window": 128000,
                    "local": False,
                    "max_output_tokens": 16000,
                    "connections": [],
                },
                {
                    "id": "openai/gpt-5.2",
                    "provider_id": "openai",
                    "model_id": "gpt-5.2",
                    "name": "GPT-5.2",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True, "control": None, "levels": []},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": [
                            "chat",
                            "text_output",
                            "image_input",
                            "image_understanding",
                        ],
                    },
                    "context_window": 256000,
                    "effective_context_window": 256000,
                    "local": False,
                    "max_output_tokens": 32000,
                    "connections": [],
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_model_list_outputs_per_model_connections_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``model.list`` propagates the per-model ``connections`` allowlist
    from the registry into the RPC payload. The WebUI uses this list to
    decide which provider connections to offer for a given model — a
    model tagged ``["oauth"]`` is not offered on ``api-key``. A model whose
    allowlist matches no usable connection is not listed at all."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_OAUTH_TOKEN", "oauth-token")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.models._models["openai"] = [
        Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=256000,
            max_output_tokens=32000,
            connections=("api-key",),
        ),
        Model(
            model_id="gpt-5.5",
            name="GPT-5.5",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=256000,
            max_output_tokens=32000,
            connections=("oauth",),
        ),
        Model(
            model_id="gpt-ghost",
            name="GPT Ghost",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=256000,
            max_output_tokens=32000,
            connections=("subscription",),
        ),
    ]

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response["ok"] is True
    by_id = {model["id"]: model for model in response["result"]["models"]}
    assert by_id["openai/gpt-5.2"]["connections"] == ["api-key"]
    assert by_id["openai/gpt-5.5"]["connections"] == ["oauth"]
    # No usable "subscription" connection exists on the stub provider, so the
    # allowlist-bound model is dropped from the listing entirely.
    assert "openai/gpt-ghost" not in by_id


@pytest.mark.asyncio
async def test_model_list_outputs_empty_connections_for_unrestricted_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model with no ``connections`` allowlist surfaces ``connections``
    as an empty list — the WebUI treats that as "valid for every
    connection of the provider"."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response["ok"] is True
    for model in response["result"]["models"]:
        assert model["connections"] == []


@pytest.mark.asyncio
async def test_model_list_filters_by_task_and_modality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.models._models["openai"].append(
        Model(
            model_id="gpt-image",
            name="GPT Image",
            capabilities=Capabilities(
                vision=True,
                tools=False,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
                input_modalities=("text", "image"),
                output_modalities=("text", "image"),
            ),
            context_window=128000,
            max_output_tokens=32000,
        )
    )

    image_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"task": "image_generation"}},
    )
    audio_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"output_modality": "audio"}},
    )
    context_response = await dispatch_rpc(
        state,
        {
            "method": "model.list",
            "params": {"capability": "tools", "min_context_window": 200000},
        },
    )

    assert [model["id"] for model in image_response["result"]["models"]] == ["openai/gpt-image"]
    assert audio_response["result"]["models"] == []
    assert [model["id"] for model in context_response["result"]["models"]] == ["openai/gpt-5.2"]


@pytest.mark.asyncio
async def test_model_list_filters_by_provider_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    state = make_state(tmp_path, StubAdapter())

    openai_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "openai"}},
    )
    anthropic_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "anthropic"}},
    )
    uppercase_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "OpenAI"}},
    )
    unknown_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "nonexistent"}},
    )

    assert [model["id"] for model in openai_response["result"]["models"]] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-5.2",
    ]
    assert [model["id"] for model in anthropic_response["result"]["models"]] == [
        "anthropic/claude-sonnet-4-20250219"
    ]
    assert [model["id"] for model in uppercase_response["result"]["models"]] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-5.2",
    ]
    assert unknown_response["result"]["models"] == []


@pytest.mark.asyncio
async def test_model_list_rejects_unsupported_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "openai", "extra": True}},
    )

    assert response == {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": "unsupported model.list fields: extra",
        },
    }


@pytest.mark.asyncio
async def test_model_list_rejects_invalid_filter_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"min_context_window": -1}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "non-negative integer" in response["error"]["message"]


@pytest.mark.asyncio
async def test_model_list_delegates_filtering_to_model_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RPC result must match what ``ModelQuery.from_filters`` + ``query`` produce.

    This locks in the byte-identical contract while routing filtering through
    the core query instead of duplicating it in the RPC layer.
    """

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "model.list",
            "params": {"task": "image_generation", "min_context_window": 1000},
        },
    )

    # Cross-check the RPC result against the core query path directly. If
    # either path diverges, this test fails — making the "delegate to the
    # core query" contract enforced.
    expected = sorted(
        (
            (
                provider_id,
                _model_response(provider_id, model),
            )
            for provider_id, model in state.runtime.models.query(
                ModelQuery.from_filters({"task": "image_generation", "min_context_window": 1000})
            )
            if _provider_has_credentials(state.runtime, provider_id)
        ),
        key=lambda item: (item[1]["provider_id"], item[1]["model_id"]),
    )
    expected_models = [item[1] for item in expected]

    assert response["result"]["models"] == expected_models


@pytest.mark.asyncio
async def test_model_refresh_db_refreshes_provider_models_and_runtime_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "openrouter",
            "model_count": 1,
            "fetched_at": "2026-05-08T19:08:00+00:00",
        },
    }
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openrouter"]
    assert FAKE_REFRESH_MODEL_CALLS == ["openrouter-key"]
    refreshed_model = state.runtime.models.get("openrouter", "fresh-model")
    assert refreshed_model.name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_provider_publishes_models_resource_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    # The per-provider path returns early but must still tell open windows to
    # reload their model lists.
    assert response["ok"] is True
    assert [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ] == [{"kind": "models"}]


@pytest.mark.asyncio
async def test_model_refresh_db_global_publishes_models_resource_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(state, {"method": "model.refresh_db"})

    assert response["ok"] is True
    assert [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ] == [{"kind": "models"}]


@pytest.mark.asyncio
async def test_model_refresh_db_updates_registry_in_place_for_captured_holders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh reloads the registry in place rather than rebinding it.

    Services that captured the registry at construction (task-model targets for
    speech/image/embeddings, the status display, the recall backend) hold the
    same instance, so the refreshed catalog must reach them without a restart.
    """

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    # The instance every holder captured at construction time.
    registry_before = state.runtime.models

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is True
    # Not rebound: holders still share this same instance...
    assert state.runtime.models is registry_before
    # ...and it now carries the refreshed catalog.
    assert registry_before.get("openrouter", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_without_params_refreshes_only_eligible_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("OPENROUTER_SECONDARY_API_KEY", "secondary-key")
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.providers.add(
        SimpleNamespace(
            id="refreshable-missing-credentials",
            name="Refreshable Missing Credentials",
            adapter="openai_compatible",
            base_url="https://missing.example/v1",
            defaults={},
            extra_headers={},
            models_endpoint="/models",
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=SimpleNamespace(credential_key="MISSING_REFRESH_API_KEY"),
                )
            ],
        )
    )
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

    assert response == {
        "ok": True,
        "result": {
            "providers": [
                {
                    "provider_id": "openrouter",
                    "model_count": 1,
                    "fetched_at": "2026-05-08T19:08:00+00:00",
                },
                {
                    "provider_id": "refreshable-secondary",
                    "model_count": 1,
                    "fetched_at": "2026-05-08T19:08:00+00:00",
                },
            ],
            "refreshed_count": 2,
            "model_count": 2,
            "canonical": None,
        },
    }
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openrouter", "refreshable-secondary"]
    assert FAKE_REFRESH_MODEL_CALLS == ["openrouter-key", "secondary-key"]
    assert state.runtime.models.get("openrouter", "fresh-model").name == "Fresh Model"
    assert state.runtime.models.get("refreshable-secondary", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_empty_params_reloads_runtime_registry_after_global_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    previous_models = state.runtime.models

    response = await dispatch_rpc(state, {"method": "model.refresh_db", "params": {}})

    assert response["ok"] is True
    # The global refresh reloads the registry in place: the same instance every
    # holder captured stays, now carrying the refreshed catalog.
    assert state.runtime.models is previous_models
    assert state.runtime.models.get("openrouter", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_passes_first_usable_connection_to_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
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
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
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
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
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
    monkeypatch.setattr(connection_methods, "refresh_models", fake_refresh_models)
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
    assert "provider 'openai' does not support model refresh" in response["error"]["message"]
    assert FAKE_REFRESH_MODEL_CALLS == []


@pytest.mark.asyncio
async def test_model_refresh_db_maps_discovery_failures_to_rpc_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_refresh_models(*_args: Any, **_kwargs: Any) -> JsonObject:
        raise ModelDiscoveryError("Model discovery failed for provider 'openrouter': bad JSON")

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(connection_methods, "refresh_models", failing_refresh_models)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert "bad JSON" in response["error"]["message"]


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
    monkeypatch.setattr(connection_methods, "refresh_models", selective_refresh_models)
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
    monkeypatch.setattr(connection_methods, "refresh_models", selective_refresh_models)
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
    assert "provider 'openai' does not support model refresh" in response["error"]["message"]


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
    assert (
        "Provider credentials not found for provider 'openrouter'" in response["error"]["message"]
    )


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
    assert response["error"]["message"] == "unsupported model refresh fields: extra"
