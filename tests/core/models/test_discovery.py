"""Tests for dynamic model discovery."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from core.models.discovery import (
    PassthroughModelFilter,
    PassthroughRawFilter,
    apply_overrides,
    normalize_openrouter,
    refresh_models,
)
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
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
