"""Models: the generic ``refresh_models`` pipeline and its catalog request.

Provider-specific projections live in ``test_discovery_projection.py``; Account
token recovery lives in ``test_discovery_auth_recovery.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import core.models.discovery as discovery_module
from core.models.discovery import ModelDiscoveryError, build_discovery_request, refresh_models
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped
from core.providers.ollama import OllamaAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.runtime import ADAPTER_TYPES
from core.utils.retry import MAX_RETRIES

from .discovery_test_support import (
    API_KEY,
    OPENAI_SUBSCRIPTION_MODELS_URL,
    SIMPLE_MODELS_URL,
    jwt_with_openai_account,
    keyless_connection,
    mock_openai_codex_package,
    model_data,
    openai_subscription_config,
    openrouter_config,
    read_models_file,
    simple_compatible_config,
)


def _capabilities(**changes: Any) -> Capabilities:
    facts: dict[str, Any] = {
        "vision": False,
        "tools": True,
        "json_mode": True,
        "reasoning": ReasoningCapabilities(supported=False),
    }
    return Capabilities(**(facts | changes))


class _RejectIds:
    def __init__(self, *rejected: str) -> None:
        self._rejected = rejected

    def accepts(self, entry: Mapping[str, Any] | Model) -> bool:
        model_id = entry.model_id if isinstance(entry, Model) else entry.get("id")
        return model_id not in self._rejected


@respx.mock
@pytest.mark.asyncio
async def test_refresh_writes_the_raw_dump_and_a_pure_projection(tmp_path: Path) -> None:
    """The raw dump keeps the whole response; the projection keeps only accepted
    Models and no override. Overrides apply when the registry loads."""

    resources_dir = tmp_path / "resources"
    models_dir = resources_dir / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "simple.overrides.json").write_text(
        json.dumps(
            {
                "provider_id": "simple",
                "models": {
                    "model-a": {"name": "Corrected Model A"},
                    "override-only": model_data("Override Only"),
                },
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "data": [
            {"id": "model-a", "name": "Model A", "future_field": "value"},
            {"id": "model-b", "name": "Model B"},
            {"id": "raw-filtered", "name": "Raw Filtered"},
            {"id": "model-filtered", "name": "Model Filtered"},
        ],
        "extra_key": "preserved",
    }
    route = respx.get(SIMPLE_MODELS_URL).mock(return_value=httpx.Response(200, json=payload))

    result = await refresh_models(
        simple_compatible_config(extra_headers={"X-Title": "vBot"}),
        API_KEY,
        resources_dir,
        raw_filter=_RejectIds("raw-filtered"),
        model_filter=_RejectIds("model-filtered"),
    )

    projection = read_models_file(resources_dir, "simple.json")
    assert result == {
        "provider_id": "simple",
        "model_count": 2,
        "fetched_at": projection["fetched_at"],
    }
    assert read_models_file(resources_dir, "simple.raw.json") == {
        "provider_id": "simple",
        "fetched_at": result["fetched_at"],
        "raw_response": payload,
    }
    assert set(projection) == {"provider_id", "source", "fetched_at", "models"}
    assert (projection["provider_id"], projection["source"]) == ("simple", "discovery")
    assert set(projection["models"]) == {"model-a", "model-b"}
    assert projection["models"]["model-a"]["name"] == "Model A"
    assert "future_field" not in projection["models"]["model-a"]
    registry = ModelRegistry.load(resources_dir)
    assert registry.get("simple", "model-a").name == "Corrected Model A"
    assert registry.get("simple", "override-only").name == "Override Only"
    headers = route.calls.last.request.headers
    assert (headers["Authorization"], headers["X-Title"]) == (f"Bearer {API_KEY}", "vBot")


_STUB_MODELS = {
    model.model_id: model
    for model in (
        Model(
            model_id="voices",
            name="Voices",
            capabilities=_capabilities(
                tools=False,
                output_modalities=("speech",),
                supported_parameters=("response_format", "seed"),
                supported_voices=("af_aoede", "af_sky"),
                task_types=("audio_generation", "text_to_speech"),
            ),
            context_window=4096,
            max_output_tokens=None,
        ),
        Model(
            model_id="window-less",
            name="Window-less",
            capabilities=_capabilities(),
            context_window=None,
            max_output_tokens=None,
        ),
        Model(
            model_id="shared-wire",
            name="Shared Wire",
            capabilities=_capabilities(),
            context_window=272_000,
            max_output_tokens=128_000,
            connection_context_windows={"api-key": 1_050_000, "subscription": 272_000},
        ),
        Model(
            model_id="bare-reasoning",
            name="Bare Reasoning",
            capabilities=_capabilities(reasoning=ReasoningCapabilities(supported=True)),
            context_window=32000,
            max_output_tokens=4096,
        ),
        Model(
            model_id="levels",
            name="Levels",
            capabilities=_capabilities(
                reasoning=ReasoningCapabilities(
                    supported=True, control="levels", levels=("low", "medium", "high")
                )
            ),
            context_window=128000,
            max_output_tokens=16000,
            family="gpt-5.2",
        ),
        Model(
            model_id="budget",
            name="Budget",
            capabilities=_capabilities(
                reasoning=ReasoningCapabilities(supported=True, control="budget", budget_max=32000)
            ),
            context_window=200000,
            max_output_tokens=64000,
        ),
    )
}


class _StubAdapter:
    @staticmethod
    def normalize_catalog_entry(raw_model: dict[str, Any], defaults: object) -> Model:
        if raw_model["id"] == "skip-me":
            raise CatalogEntrySkipped("not a chat model")
        if raw_model["id"] == "broken":
            raise ValueError("schema mismatch")
        return _STUB_MODELS[raw_model["id"]]


@respx.mock
@pytest.mark.asyncio
async def test_normalized_models_round_trip_through_the_written_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(discovery_module._DISCOVERY_ADAPTER_MAP, "stub", _StubAdapter)
    resources_dir = tmp_path / "resources"
    respx.get(SIMPLE_MODELS_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "skip-me"}, *({"id": key} for key in _STUB_MODELS)]}
        )
    )

    result = await refresh_models(simple_compatible_config(adapter="stub"), API_KEY, resources_dir)

    written = read_models_file(resources_dir, "simple.json")["models"]
    assert result["model_count"] == len(_STUB_MODELS)
    assert "skip-me" not in written
    assert written["voices"]["capabilities"]["supported_voices"] == ["af_aoede", "af_sky"]
    assert written["window-less"]["context_window"] is None
    assert written["shared-wire"]["connection_context_windows"] == {
        "api-key": 1_050_000,
        "subscription": 272_000,
    }
    # Unset reasoning control fields and an unknown family are omitted.
    assert written["bare-reasoning"]["capabilities"]["reasoning"] == {"supported": True}
    assert "family" not in written["bare-reasoning"]
    assert written["levels"]["capabilities"]["reasoning"] == {
        "supported": True,
        "control": "levels",
        "levels": ["low", "medium", "high"],
    }
    assert written["levels"]["family"] == "gpt-5.2"
    assert written["budget"]["capabilities"]["reasoning"] == {
        "supported": True,
        "control": "budget",
        "budget_max": 32000,
    }
    registry = ModelRegistry.load(resources_dir)
    assert {model_id: registry.get("simple", model_id) for model_id in _STUB_MODELS} == (
        _STUB_MODELS
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "response", "expected_calls", "raw_dump_kept"),
    [
        pytest.param(
            "openai_compatible", httpx.Response(200, text="not-json"), 1, False, id="invalid-json"
        ),
        pytest.param(
            "openai_compatible", httpx.Response(200, json={"items": []}), 1, False, id="no-list"
        ),
        pytest.param("openai_compatible", httpx.Response(404), 1, False, id="fatal-status"),
        pytest.param(
            "openai_compatible",
            httpx.Response(500),
            MAX_RETRIES + 1,
            False,
            id="retries-exhausted",
        ),
        pytest.param("unknown_adapter", httpx.Response(200), 0, False, id="unknown-adapter"),
        # The raw dump is written before normalization, so it survives for inspection.
        pytest.param(
            "stub",
            httpx.Response(200, json={"data": [{"id": "broken"}]}),
            1,
            True,
            id="normalizer-error",
        ),
    ],
)
async def test_failed_refresh_raises_and_writes_no_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    adapter: str,
    response: httpx.Response,
    expected_calls: int,
    raw_dump_kept: bool,
) -> None:
    monkeypatch.setitem(discovery_module._DISCOVERY_ADAPTER_MAP, "stub", _StubAdapter)
    route = respx.get(SIMPLE_MODELS_URL).mock(return_value=response)
    resources_dir = tmp_path / "resources"

    with (
        caplog.at_level(logging.WARNING, logger="vbot.models.discovery"),
        pytest.raises(ModelDiscoveryError, match="provider 'simple'"),
    ):
        await refresh_models(simple_compatible_config(adapter=adapter), API_KEY, resources_dir)

    # The error names the Provider; callers log what they catch, discovery does not.
    assert [record for record in caplog.records if record.name == "vbot.models.discovery"] == []
    assert route.call_count == expected_calls
    assert (resources_dir / "models" / "simple.raw.json").exists() is raw_dump_kept
    assert not (resources_dir / "models" / "simple.json").exists()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transient_failure",
    [httpx.Response(503), httpx.ConnectError("connection reset")],
    ids=["status", "transport"],
)
async def test_transient_failure_is_retried_with_fresh_auth_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient_failure: httpx.Response | Exception
) -> None:
    tokens = iter(("first-test-token", "second-test-token"))

    async def getter() -> str:
        return next(tokens)

    route = respx.get(SIMPLE_MODELS_URL).mock(
        side_effect=[transient_failure, httpx.Response(200, json={"data": [{"id": "model-a"}]})]
    )

    result = await refresh_models(simple_compatible_config(), getter, tmp_path / "resources")

    assert result["model_count"] == 1
    assert [call.request.headers["Authorization"] for call in route.calls] == [
        "Bearer first-test-token",
        "Bearer second-test-token",
    ]


def _existing_model(name: str, connections: list[str]) -> dict[str, Any]:
    return model_data(name) | {"connections": connections}


@respx.mock
@pytest.mark.asyncio
async def test_connection_refresh_replaces_only_its_own_catalog_entries(tmp_path: Path) -> None:
    config = openai_subscription_config()
    resources_dir = tmp_path / "resources"
    models_dir = resources_dir / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "openai.json").write_text(
        json.dumps(
            {
                "provider_id": "openai",
                "source": "discovery",
                "fetched_at": "2026-05-08T19:08:00+00:00",
                "models": {
                    "gpt-5.2": _existing_model("GPT-5.2", ["api-key"]),
                    "stale": _existing_model("Stale Subscription Model", ["subscription"]),
                },
            }
        ),
        encoding="utf-8",
    )
    access_token = jwt_with_openai_account("acct_openai")
    mock_openai_codex_package()
    route = respx.get(f"{OPENAI_SUBSCRIPTION_MODELS_URL}?client_version=0.144.6").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "gpt-5-codex", "display_name": "GPT-5 Codex", "visibility": "list"},
                    {"slug": "codex-auto-review", "display_name": "Review", "visibility": "hide"},
                ]
            },
        )
    )

    result = await refresh_models(
        config, access_token, resources_dir, credential_connection=config.connections[0]
    )

    written = read_models_file(resources_dir, "openai.json")["models"]
    raw = read_models_file(resources_dir, "openai.raw.json")["raw_response"]
    assert result["model_count"] == 2
    assert {model_id: data["connections"] for model_id, data in written.items()} == {
        "gpt-5.2": ["api-key"],
        "gpt-5-codex": ["subscription"],
    }
    # A hidden entry stays inspectable in the raw dump only.
    assert {entry["slug"] for entry in raw["models"]} == {"gpt-5-codex", "codex-auto-review"}
    registry = ModelRegistry.load(resources_dir)
    assert registry.get("openai", "gpt-5.2").connections == ("api-key",)
    assert registry.get("openai", "gpt-5-codex").connections == ("subscription",)
    headers = route.calls.last.request.headers
    assert headers["Authorization"] == f"Bearer {access_token}"
    assert headers["chatgpt-account-id"] == "acct_openai"


def _secondary_openrouter() -> tuple[ProviderConfig, ConnectionConfig]:
    secondary = ConnectionConfig(
        id="secondary",
        type="api_key",
        label="Secondary",
        auth=AuthConfig(header="x-api-key", prefix="Token ", credential_key="SECONDARY_KEY"),
    )
    config = openrouter_config()
    return replace(config, connections=[*config.connections, secondary]), secondary


def _keyless_localhost() -> tuple[ProviderConfig, ConnectionConfig]:
    connection = keyless_connection()
    config = simple_compatible_config(
        id="localhost", base_url="http://localhost:9999/v1", connections=[connection]
    )
    return config, connection


def _codex_subscription() -> tuple[ProviderConfig, ConnectionConfig]:
    config = openai_subscription_config()
    return config, config.connections[0]


_CODEX_TOKEN = jwt_with_openai_account("acct_openai")
_CODEX_HEADERS = {
    "Authorization": f"Bearer {_CODEX_TOKEN}",
    "chatgpt-account-id": "acct_openai",
    "OpenAI-Beta": "responses=experimental",
    "originator": "vbot",
}


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "package_version", "credential", "expected_url", "expected_headers"),
    [
        pytest.param(
            _codex_subscription,
            "0.144.6",
            _CODEX_TOKEN,
            f"{OPENAI_SUBSCRIPTION_MODELS_URL}?client_version=0.144.6",
            _CODEX_HEADERS,
            id="connection-endpoint-and-current-codex-version",
        ),
        pytest.param(
            _codex_subscription,
            "next",
            _CODEX_TOKEN,
            f"{OPENAI_SUBSCRIPTION_MODELS_URL}?client_version=0.144.0",
            _CODEX_HEADERS,
            id="bad-package-metadata-falls-back",
        ),
        pytest.param(
            _secondary_openrouter,
            None,
            API_KEY,
            "https://openrouter.ai/api/v1/models",
            {"X-Title": "vBot", "x-api-key": f"Token {API_KEY}"},
            id="selected-connection-auth",
        ),
        pytest.param(
            _keyless_localhost,
            None,
            "",
            "http://localhost:9999/v1/models",
            {},
            id="keyless-connection-sends-no-auth",
        ),
    ],
)
async def test_discovery_request_resolves_the_selected_connection(
    target: Any,
    package_version: str | None,
    credential: str,
    expected_url: str,
    expected_headers: dict[str, str],
) -> None:
    config, connection = target()
    if package_version is not None:
        mock_openai_codex_package(package_version)

    request = await build_discovery_request(config, connection)

    assert request.url == expected_url
    assert request.base_url == (connection.base_url or config.base_url)
    assert request.headers(credential) == expected_headers


@pytest.mark.asyncio
async def test_connection_without_models_endpoint_is_rejected(tmp_path: Path) -> None:
    config = simple_compatible_config(models_endpoint=None)
    connection = config.connections[0]

    with pytest.raises(ValueError, match="does not define a models_endpoint"):
        await build_discovery_request(config, connection)
    with pytest.raises(ValueError, match="does not define a models_endpoint"):
        await refresh_models(
            config, API_KEY, tmp_path / "resources", credential_connection=connection
        )


@respx.mock
@pytest.mark.asyncio
async def test_every_chat_adapter_has_a_discovery_normalizer() -> None:
    """A Provider refreshes its catalog through its own chat Adapter; Ollama Cloud
    chats over OpenAI compatibility but discovers through the Ollama-native API."""

    connection = keyless_connection()
    mock_openai_codex_package()

    bound = {
        adapter: (
            await build_discovery_request(
                simple_compatible_config(adapter=adapter, connections=[connection]), connection
            )
        ).adapter_class
        for adapter in ADAPTER_TYPES
    }

    assert bound == {
        adapter: OllamaAdapter if adapter == "ollama_cloud" else adapter_class
        for adapter, adapter_class in ADAPTER_TYPES.items()
    }
