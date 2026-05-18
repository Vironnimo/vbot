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
    refresh_models,
)
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
GITHUB_COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"
OPENCODE_GO_MODELS_URL = "https://opencode-go.example/v1/models"
API_KEY = "test-openrouter-key"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
        adapter="openrouter",
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


@pytest.fixture()
def github_copilot_config() -> ProviderConfig:
    return ProviderConfig(
        id="github-copilot",
        name="GitHub Copilot",
        adapter="github_copilot",
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
    )


@pytest.fixture()
def opencode_go_config() -> ProviderConfig:
    return ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        adapter="opencode_go",
        base_url="https://opencode-go.example/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENCODE_GO_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
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

    def test_override_validation_tolerates_optional_metadata(self, tmp_path: Path):
        overrides_path = tmp_path / "github-copilot.overrides.json"
        override = model_data("GPT-5.2")
        override["metadata"] = {
            "github_copilot": {
                "vendor": "OpenAI",
                "supported_endpoints": ["/responses"],
            }
        }
        overrides_path.write_text(
            json.dumps({"provider_id": "github-copilot", "models": {"gpt-5.2": override}}),
            encoding="utf-8",
        )

        merged = apply_overrides({}, overrides_path)

        assert merged["gpt-5.2"]["metadata"] == override["metadata"]

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
    async def test_refresh_models_supports_opencode_go_discovery_adapter(
        self,
        tmp_path: Path,
        opencode_go_config: ProviderConfig,
    ):
        resources_dir = tmp_path / "resources"
        route = respx.get(OPENCODE_GO_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        raw_openrouter_model(
                            model_id="deepseek/deepseek-r1",
                            name="DeepSeek R1",
                        )
                    ]
                },
            )
        )

        result = await refresh_models(opencode_go_config, API_KEY, resources_dir)

        registry = ModelRegistry.load(resources_dir)
        model = registry.get("opencode-go", "deepseek/deepseek-r1")
        assert result["provider_id"] == "opencode-go"
        assert result["model_count"] == 1
        assert model.name == "DeepSeek R1"
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"

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
        raw_output_path = resources_dir / "models" / "openrouter.raw.json"
        output_data = json.loads(output_path.read_text(encoding="utf-8"))
        raw_output_data = json.loads(raw_output_path.read_text(encoding="utf-8"))
        registry = ModelRegistry.load(resources_dir)
        model_b = registry.get("openrouter", "model-b")

        assert result["provider_id"] == "openrouter"
        assert result["model_count"] == 2
        assert result["fetched_at"] == output_data["fetched_at"]
        assert output_data["source"] == "discovery"
        assert raw_output_path.exists()
        assert raw_output_data["provider_id"] == "openrouter"
        assert raw_output_data["fetched_at"] == output_data["fetched_at"]
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
        raw_output_path = models_dir / "openrouter.raw.json"
        registry = ModelRegistry.load(resources_dir)
        assert result["model_count"] == 2
        assert output_path.exists()
        assert raw_output_path.exists()
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
        )
        route = respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [raw_openrouter_model(model_id="model-a", name="Model A")]},
            )
        )
        resources_dir = tmp_path / "resources"

        await refresh_models(
            provider_config,
            API_KEY,
            resources_dir,
            credential_connection=selected_connection,
        )

        assert (resources_dir / "models" / "openrouter.json").exists()
        assert (resources_dir / "models" / "openrouter.raw.json").exists()
        assert route.calls.last.request.headers["x-api-key"] == f"Token {API_KEY}"
        assert "Authorization" not in route.calls.last.request.headers

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_uses_tolerant_normalizer_for_github_copilot(
        self,
        tmp_path: Path,
        github_copilot_config: ProviderConfig,
    ):
        raw_fixture = json.loads(
            (FIXTURES_DIR / "github_copilot_models_raw.json").read_text(encoding="utf-8")
        )
        route = respx.get(GITHUB_COPILOT_MODELS_URL).mock(
            return_value=httpx.Response(200, json=raw_fixture)
        )

        result = await refresh_models(github_copilot_config, API_KEY, tmp_path / "resources")

        registry = ModelRegistry.load(tmp_path / "resources")
        gpt_4o = registry.get("github-copilot", "gpt-4o")
        gemini_2_5_pro = registry.get("github-copilot", "gemini-2.5-pro")
        output_data = json.loads(
            (tmp_path / "resources" / "models" / "github-copilot.json").read_text(encoding="utf-8")
        )
        raw_output_path = tmp_path / "resources" / "models" / "github-copilot.raw.json"
        raw_output_data = json.loads(raw_output_path.read_text(encoding="utf-8"))
        gpt_5_mini_data = output_data["models"]["gpt-5-mini"]
        assert result["model_count"] == 5
        assert raw_output_path.exists()
        assert raw_output_data["raw_response"] == raw_fixture
        assert gpt_4o.capabilities.vision is True
        assert gpt_4o.context_window == 128000
        assert gpt_4o.max_output_tokens == 4096
        assert gemini_2_5_pro.capabilities.reasoning.supported is True
        assert gpt_5_mini_data["metadata"]["github_copilot"] == {
            "family": "gpt-5-mini",
            "parallel_tool_calls": True,
            "reasoning_efforts": ["low", "medium", "high"],
            "streaming": True,
            "structured_outputs": True,
            "supported_endpoints": ["/chat/completions", "/responses", "ws:/responses"],
            "tool_calls": True,
            "vendor": "Azure OpenAI",
            "version": "gpt-5-mini",
        }
        assert "policy" not in gpt_5_mini_data["metadata"]["github_copilot"]
        assert registry.get("github-copilot", "gpt-5-mini").metadata["github_copilot"][
            "supported_endpoints"
        ] == ("/chat/completions", "/responses", "ws:/responses")
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert route.calls.last.request.headers["Copilot-Integration-Id"] == "vbot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_writes_raw_file_with_full_provider_response(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        resources_dir = tmp_path / "resources"
        raw_response = {
            "data": [
                {
                    **raw_openrouter_model(model_id="model-a", name="Model A"),
                    "future_field": "value",
                }
            ],
            "extra_key": "preserved",
        }
        respx.get(OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json=raw_response))

        await refresh_models(openrouter_config, API_KEY, resources_dir)

        raw_output_data = json.loads(
            (resources_dir / "models" / "openrouter.raw.json").read_text(encoding="utf-8")
        )
        sanitized_output_data = json.loads(
            (resources_dir / "models" / "openrouter.json").read_text(encoding="utf-8")
        )

        assert raw_output_data["raw_response"]["extra_key"] == "preserved"
        assert raw_output_data["raw_response"]["data"][0]["future_field"] == "value"
        assert "extra_key" not in sanitized_output_data
        assert "future_field" not in sanitized_output_data["models"]["model-a"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_raw_file_contains_unfiltered_data(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        class _DropModelBFilter:
            def accepts(self, raw_model):
                return raw_model.get("id") != "model-b"

        resources_dir = tmp_path / "resources"
        raw_response = {
            "data": [
                raw_openrouter_model(model_id="model-a", name="Model A"),
                raw_openrouter_model(model_id="model-b", name="Model B"),
            ]
        }
        respx.get(OPENROUTER_MODELS_URL).mock(return_value=httpx.Response(200, json=raw_response))

        await refresh_models(
            openrouter_config,
            API_KEY,
            resources_dir,
            raw_filter=_DropModelBFilter(),
        )

        raw_output_data = json.loads(
            (resources_dir / "models" / "openrouter.raw.json").read_text(encoding="utf-8")
        )
        sanitized_output_data = json.loads(
            (resources_dir / "models" / "openrouter.json").read_text(encoding="utf-8")
        )

        assert len(raw_output_data["raw_response"]["data"]) == 2
        assert {model["id"] for model in raw_output_data["raw_response"]["data"]} == {
            "model-a",
            "model-b",
        }
        assert set(sanitized_output_data["models"].keys()) == {"model-a"}

    @pytest.mark.asyncio
    async def test_refresh_models_rejects_unknown_discovery_adapter(self, tmp_path: Path):
        provider_config = ProviderConfig(
            id="unknown-provider",
            name="Unknown Provider",
            adapter="unknown_adapter",
            base_url="https://example.test",
            connections=[],
            defaults={},
            extra_headers={},
            models_endpoint="/models",
        )

        with pytest.raises(ModelDiscoveryError, match="unknown_adapter"):
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
