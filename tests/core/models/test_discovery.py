"""Tests for dynamic model discovery."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.models.discovery import (
    ModelDiscoveryError,
    PassthroughModelFilter,
    PassthroughRawFilter,
    apply_overrides,
    normalize_openai_compatible,
    normalize_openrouter,
    refresh_models,
)
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
GITHUB_COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"
GENERIC_OPENAI_MODELS_URL = "https://generic.example/v1/models"
API_KEY = "test-openrouter-key"


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    ModelRegistry._cache.clear()
    yield
    ModelRegistry._cache.clear()


@pytest.fixture()
def openrouter_config() -> ProviderConfig:
    return ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openai_compatible",
        base_url="https://openrouter.ai/api/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENROUTER_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={"X-Title": "vBot"},
        models_endpoint="/models",
        model_discovery="openrouter",
    )


@pytest.fixture()
def github_copilot_config() -> ProviderConfig:
    return ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="openai_compatible",
        base_url="https://api.githubcopilot.com",
        connections=[
            ConnectionConfig(
                id="oauth",
                type="oauth",
                label="GitHub OAuth",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="GITHUB_COPILOT_TOKEN",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={"Copilot-Integration-Id": "vbot"},
        models_endpoint="/models",
        model_discovery="openai_compatible",
    )


@pytest.fixture()
def generic_openai_config() -> ProviderConfig:
    return ProviderConfig(
        id="generic-openai",
        name="Generic OpenAI-Compatible",
        adapter="openai_compatible",
        base_url="https://generic.example/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="GENERIC_OPENAI_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={"X-Test": "generic"},
        models_endpoint="/models",
    )


def raw_openrouter_model(
    *,
    model_id: str = "anthropic/claude-sonnet-4",
    name: str = "Anthropic: Claude Sonnet 4",
    input_modalities: list[str] | None = None,
    supported_parameters: list[str] | None = None,
    context_length: int = 128000,
    max_completion_tokens: int | None = 64000,
) -> dict:
    return {
        "id": model_id,
        "name": name,
        "architecture": {"input_modalities": input_modalities or ["text", "image"]},
        "supported_parameters": (
            supported_parameters
            if supported_parameters is not None
            else ["tools", "response_format", "reasoning"]
        ),
        "context_length": context_length,
        "top_provider": {"max_completion_tokens": max_completion_tokens},
    }


def model_data(name: str = "Model Name") -> dict:
    return {
        "name": name,
        "capabilities": {
            "vision": False,
            "tools": True,
            "json_mode": True,
            "reasoning": {"supported": False},
        },
        "context_window": 32000,
        "max_output_tokens": 4096,
    }


class TestNormalizeOpenRouter:
    def test_happy_path_maps_all_fields(self):
        raw_model = raw_openrouter_model()

        model = normalize_openrouter(raw_model, {"max_tokens": 8192})

        assert model == Model(
            model_id="anthropic/claude-sonnet-4",
            name="Anthropic: Claude Sonnet 4",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=128000,
            max_output_tokens=64000,
        )

    def test_null_max_completion_tokens_uses_provider_default(self):
        raw_model = raw_openrouter_model(max_completion_tokens=None)

        model = normalize_openrouter(raw_model, {"max_tokens": 8192})

        assert model.max_output_tokens == 8192

    def test_null_max_completion_tokens_uses_hard_fallback_without_default(self):
        raw_model = raw_openrouter_model(max_completion_tokens=None)

        model = normalize_openrouter(raw_model, {})

        assert model.max_output_tokens == 4096

    @pytest.mark.parametrize(
        ("supported_parameters", "tools", "json_mode", "reasoning"),
        [
            (["tools"], True, False, False),
            (["response_format"], False, True, False),
            (["structured_outputs"], False, True, False),
            (["reasoning"], False, False, True),
            (["include_reasoning"], False, False, True),
            ([], False, False, False),
        ],
    )
    def test_supported_parameters_derive_capabilities(
        self,
        supported_parameters: list[str],
        tools: bool,
        json_mode: bool,
        reasoning: bool,
    ):
        raw_model = raw_openrouter_model(supported_parameters=supported_parameters)

        model = normalize_openrouter(raw_model, {})
        assert model.capabilities.tools is tools
        assert model.capabilities.json_mode is json_mode
        assert model.capabilities.reasoning.supported is reasoning

    @pytest.mark.parametrize(
        ("input_modalities", "vision"), [(["text", "image"], True), (["text"], False)]
    )
    def test_input_modalities_derive_vision(self, input_modalities: list[str], vision: bool):
        raw_model = raw_openrouter_model(input_modalities=input_modalities)

        model = normalize_openrouter(raw_model, {})
        assert model.capabilities.vision is vision


class TestNormalizeOpenAICompatible:
    def test_missing_openrouter_only_fields_uses_top_level_fields_and_defaults_tools(self):
        raw_model = {
            "id": "gpt-4.1",
            "context_window": 1047576,
            "max_output_tokens": 32768,
        }

        model = normalize_openai_compatible(raw_model, {"max_tokens": 8192})

        assert model == Model(
            model_id="gpt-4.1",
            name="gpt-4.1",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=1047576,
            max_output_tokens=32768,
        )

    def test_openrouter_only_fields_are_ignored_for_generic_discovery(self):
        raw_model = {
            "id": "claude-sonnet-4",
            "name": "Claude Sonnet 4",
            "architecture": {"input_modalities": ["text", "image"], "context_length": 200000},
            "supported_parameters": ["tools", "response_format", "reasoning"],
            "top_provider": {"max_completion_tokens": 64000, "tools": False},
        }

        model = normalize_openai_compatible(raw_model, {"max_tokens": 8192})

        assert model.name == "Claude Sonnet 4"
        assert model.context_window == 0
        assert model.max_output_tokens == 8192
        assert model.capabilities.vision is False
        assert model.capabilities.tools is True
        assert model.capabilities.json_mode is False
        assert model.capabilities.reasoning.supported is False

    def test_non_object_architecture_is_ignored_for_copilot_payloads(self):
        raw_model = {
            "id": "claude-sonnet-4",
            "name": "Claude Sonnet 4",
            "architecture": "unknown",
            "contextLength": "ignored",
            "contextWindow": 200000,
            "maxOutputTokens": 64000,
            "input_modalities": ["text", {"type": "image"}],
        }

        model = normalize_openai_compatible(raw_model, {"max_tokens": 8192})

        assert model.name == "Claude Sonnet 4"
        assert model.context_window == 200000
        assert model.max_output_tokens == 64000
        assert model.capabilities.vision is True

    def test_generic_normalization_uses_explicit_top_level_metadata_for_capabilities(self):
        raw_model = {
            "id": "o4-mini",
            "name": "o4-mini",
            "context_length": 128000,
            "tools": False,
            "supports_json_mode": False,
            "reasoning": {"supported": True},
        }

        model = normalize_openai_compatible(raw_model, {"max_tokens": 8192})

        assert model.capabilities.tools is False
        assert model.capabilities.json_mode is False
        assert model.capabilities.reasoning.supported is True
        assert model.max_output_tokens == 8192


class TestApplyOverrides:
    def test_override_corrects_one_field_only(self, tmp_path: Path):
        overrides_path = tmp_path / "openrouter.overrides.json"
        overrides_path.write_text(
            json.dumps({"provider_id": "openrouter", "models": {"model-a": {"name": "Corrected"}}}),
            encoding="utf-8",
        )
        models = {"model-a": model_data("Original")}

        merged = apply_overrides(models, overrides_path)

        assert merged["model-a"]["name"] == "Corrected"
        assert merged["model-a"]["context_window"] == 32000

    def test_override_adds_model_not_in_fetch(self, tmp_path: Path):
        overrides_path = tmp_path / "openrouter.overrides.json"
        overrides_path.write_text(
            json.dumps(
                {"provider_id": "openrouter", "models": {"model-b": model_data("Override Only")}}
            ),
            encoding="utf-8",
        )

        merged = apply_overrides({}, overrides_path)
        assert merged["model-b"]["name"] == "Override Only"

    def test_no_override_file_returns_models_unchanged(self, tmp_path: Path):
        models = {"model-a": model_data("Original")}

        merged = apply_overrides(models, tmp_path / "missing.overrides.json")
        assert merged == models

    def test_override_with_full_model_definition_works(self, tmp_path: Path):
        overrides_path = tmp_path / "openrouter.overrides.json"
        full_override = model_data("Full Override")
        overrides_path.write_text(
            json.dumps({"provider_id": "openrouter", "models": {"model-c": full_override}}),
            encoding="utf-8",
        )

        merged = apply_overrides({"model-c": model_data("Original")}, overrides_path)
        assert merged["model-c"] == full_override

    def test_existing_model_override_is_validated_after_nested_replacement(self, tmp_path: Path):
        overrides_path = tmp_path / "openrouter.overrides.json"
        overrides_path.write_text(
            json.dumps(
                {
                    "provider_id": "openrouter",
                    "models": {"model-a": {"capabilities": {"vision": True}}},
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="Invalid override for model 'model-a'"):
            apply_overrides({"model-a": model_data("Original")}, overrides_path)


class TestPassthroughFilters:
    def test_raw_filter_accepts_everything(self):
        assert PassthroughRawFilter().accepts({"anything": object()}) is True

    def test_model_filter_accepts_everything(self):
        model = Model(
            model_id="model-a",
            name="Model A",
            capabilities=Capabilities(
                vision=False,
                tools=False,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=1000,
            max_output_tokens=100,
        )

        assert PassthroughModelFilter().accepts(model) is True


class TestRefreshModels:
    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_writes_json_and_registry_reads_it(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        resources_dir = tmp_path / "resources"
        route = respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        raw_openrouter_model(model_id="model-a", name="Model A"),
                        raw_openrouter_model(
                            model_id="model-b",
                            name="Model B",
                            max_completion_tokens=None,
                        ),
                    ]
                },
            )
        )

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        output_path = resources_dir / "models" / "openrouter.json"
        output_data = json.loads(output_path.read_text(encoding="utf-8"))
        registry = ModelRegistry.load(resources_dir)
        model_b = registry.get("openrouter", "model-b")

        assert result["provider_id"] == "openrouter"
        assert result["model_count"] == 2
        assert result["fetched_at"] == output_data["fetched_at"]
        assert output_data["source"] == "discovery"
        assert model_b.name == "Model B"
        assert model_b.max_output_tokens == 8192
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert route.calls.last.request.headers["X-Title"] == "vBot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_reads_colocated_overrides_without_writing_override_catalog(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        resources_dir = tmp_path / "resources"
        models_dir = resources_dir / "models"
        models_dir.mkdir(parents=True)
        overrides_path = models_dir / "openrouter.overrides.json"
        overrides_path.write_text(
            json.dumps(
                {
                    "provider_id": "openrouter",
                    "models": {
                        "model-a": {"name": "Corrected Model A"},
                        "override-only": model_data("Override Only"),
                    },
                }
            ),
            encoding="utf-8",
        )
        respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [raw_openrouter_model(model_id="model-a", name="Model A")]},
            )
        )

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        output_path = models_dir / "openrouter.json"
        registry = ModelRegistry.load(resources_dir)
        assert result["model_count"] == 2
        assert output_path.exists()
        assert overrides_path.exists()
        assert not (resources_dir / "model-overrides" / "openrouter.json").exists()
        assert registry.get("openrouter", "model-a").name == "Corrected Model A"
        assert registry.get("openrouter", "override-only").name == "Override Only"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_uses_selected_connection_auth_headers(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        selected_connection = ConnectionConfig(
            id="secondary",
            type="api_key",
            label="Secondary",
            auth=AuthConfig(
                header="x-api-key",
                prefix="Token ",
                credential_key="SECONDARY_KEY",
            ),
        )
        provider_config = ProviderConfig(
            id=openrouter_config.id,
            name=openrouter_config.name,
            adapter=openrouter_config.adapter,
            base_url=openrouter_config.base_url,
            connections=[openrouter_config.connections[0], selected_connection],
            defaults=openrouter_config.defaults,
            extra_headers=openrouter_config.extra_headers,
            models_endpoint=openrouter_config.models_endpoint,
            model_discovery=openrouter_config.model_discovery,
        )
        route = respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [raw_openrouter_model(model_id="model-a", name="Model A")]},
            )
        )

        await refresh_models(
            provider_config,
            API_KEY,
            tmp_path / "resources",
            credential_connection=selected_connection,
        )

        assert route.calls.last.request.headers["x-api-key"] == f"Token {API_KEY}"
        assert "Authorization" not in route.calls.last.request.headers

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_uses_tolerant_normalizer_for_github_copilot(
        self,
        tmp_path: Path,
        github_copilot_config: ProviderConfig,
    ):
        route = respx.get(GITHUB_COPILOT_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "gpt-4.1",
                            "context_window": 1047576,
                            "max_output_tokens": 32768,
                        },
                        {
                            "id": "claude-sonnet-4",
                            "name": "Claude Sonnet 4",
                            "architecture": "not-an-object",
                            "contextWindow": 200000,
                            "maxOutputTokens": 64000,
                        },
                    ]
                },
            )
        )

        result = await refresh_models(github_copilot_config, API_KEY, tmp_path / "resources")

        registry = ModelRegistry.load(tmp_path / "resources")
        model_without_name = registry.get("github-copilot", "gpt-4.1")
        model_with_non_object_architecture = registry.get(
            "github-copilot",
            "claude-sonnet-4",
        )
        assert result["model_count"] == 2
        assert model_without_name.name == "gpt-4.1"
        assert model_with_non_object_architecture.context_window == 200000
        assert model_with_non_object_architecture.max_output_tokens == 64000
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert route.calls.last.request.headers["Copilot-Integration-Id"] == "vbot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_defaults_discovery_strategy_from_adapter(
        self,
        tmp_path: Path,
        generic_openai_config: ProviderConfig,
    ):
        route = respx.get(GENERIC_OPENAI_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "gpt-4.1-mini",
                            "context_window": 128000,
                            "max_output_tokens": 16000,
                        }
                    ]
                },
            )
        )

        result = await refresh_models(generic_openai_config, API_KEY, tmp_path / "resources")

        registry = ModelRegistry.load(tmp_path / "resources")
        model = registry.get("generic-openai", "gpt-4.1-mini")
        assert result["provider_id"] == "generic-openai"
        assert result["model_count"] == 1
        assert model.name == "gpt-4.1-mini"
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert route.calls.last.request.headers["X-Test"] == "generic"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_uses_explicit_openrouter_discovery_strategy(
        self,
        tmp_path: Path,
        generic_openai_config: ProviderConfig,
    ):
        provider_config = ProviderConfig(
            id=generic_openai_config.id,
            name=generic_openai_config.name,
            adapter=generic_openai_config.adapter,
            base_url=generic_openai_config.base_url,
            connections=generic_openai_config.connections,
            defaults=generic_openai_config.defaults,
            extra_headers=generic_openai_config.extra_headers,
            models_endpoint=generic_openai_config.models_endpoint,
            model_discovery="openrouter",
        )
        respx.get(GENERIC_OPENAI_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "gpt-4.1-mini",
                            "context_window": 128000,
                            "max_output_tokens": 16000,
                        }
                    ]
                },
            )
        )

        with pytest.raises(ModelDiscoveryError, match="Expected 'architecture' to be an object"):
            await refresh_models(provider_config, API_KEY, tmp_path / "resources")

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_maps_invalid_json_response_to_discovery_error(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        respx.get(OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, text="not-json"))

        with pytest.raises(ModelDiscoveryError, match="Model discovery failed"):
            await refresh_models(openrouter_config, API_KEY, tmp_path / "resources")
