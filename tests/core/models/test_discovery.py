"""Tests for dynamic model discovery."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

import core.models.discovery as discovery_module
from core.models.discovery import (
    ModelDiscoveryError,
    PassthroughModelFilter,
    PassthroughRawFilter,
    apply_overrides,
    refresh_models,
)
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
GITHUB_COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"
OPENAI_SUBSCRIPTION_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
OPENCODE_GO_MODELS_URL = "https://opencode-go.example/v1/models"
STUB_DISCOVERY_MODELS_URL = "https://stub-provider.example/v1/models"
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
    output_modalities: list[str] | None = None,
    supported_parameters: list[str] | None = None,
    context_length: int = 128000,
    max_completion_tokens: int | None = 64000,
) -> dict:
    return {
        "id": model_id,
        "name": name,
        "architecture": {
            "input_modalities": input_modalities or ["text", "image"],
            "output_modalities": output_modalities or ["text"],
            "modality": "text+image->text",
        },
        "supported_parameters": (
            supported_parameters
            if supported_parameters is not None
            else ["tools", "response_format", "reasoning"]
        ),
        "context_length": context_length,
        "top_provider": {"max_completion_tokens": max_completion_tokens},
    }


@pytest.fixture()
def openai_subscription_config() -> ProviderConfig:
    return ProviderConfig(
        id="openai-subscription",
        name="OpenAI Subscription",
        adapter="openai_subscription",
        base_url="https://chatgpt.com/backend-api",
        connections=[
            ConnectionConfig(
                id="oauth",
                type="oauth",
                label="ChatGPT Plus/Pro",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={
            "OpenAI-Beta": "responses=experimental",
            "originator": "vbot",
        },
        models_endpoint="/codex/models",
    )


def model_data(name: str = "Model Name") -> dict:
    return {
        "name": name,
        "capabilities": {
            "vision": False,
            "tools": True,
            "json_mode": True,
            "reasoning": {"supported": False},
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supported_parameters": ["response_format", "tools"],
            "task_types": ["chat", "text_output"],
        },
        "context_window": 32000,
        "max_output_tokens": 4096,
    }


def jwt_with_openai_account(account_id: str = "acct_vbot") -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
        }
    }
    encoded_payload = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    )
    return f"header.{encoded_payload}.signature"


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

    def test_override_full_model_definition_allows_unknown_max_output_tokens(
        self,
        tmp_path: Path,
    ):
        overrides_path = tmp_path / "openrouter.overrides.json"
        full_override = model_data("Full Override")
        full_override["max_output_tokens"] = None
        overrides_path.write_text(
            json.dumps({"provider_id": "openrouter", "models": {"model-c": full_override}}),
            encoding="utf-8",
        )

        merged = apply_overrides({}, overrides_path)

        assert merged["model-c"]["max_output_tokens"] is None

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

    def test_override_merges_supported_voices_into_existing_model(self, tmp_path: Path):
        """An authored full ``capabilities`` block with ``supported_voices`` round-trips
        through ``apply_overrides`` and reaches the catalog JSON unchanged. OpenAI's
        Phase-5 TTS models will land this way."""
        overrides_path = tmp_path / "openai.overrides.json"
        override = model_data("GPT-4o Mini TTS")
        override["capabilities"]["supported_voices"] = [
            "alloy",
            "ash",
            "ballad",
            "coral",
        ]
        overrides_path.write_text(
            json.dumps({"provider_id": "openai", "models": {"gpt-4o-mini-tts": override}}),
            encoding="utf-8",
        )

        merged = apply_overrides({}, overrides_path)

        assert merged["gpt-4o-mini-tts"]["capabilities"]["supported_voices"] == [
            "alloy",
            "ash",
            "ballad",
            "coral",
        ]

    def test_openai_overrides_file_validates_and_exposes_expected_models(
        self, tmp_path: Path
    ) -> None:
        """The shipped ``resources/models/openai.overrides.json`` is well-formed,
        registers exactly the Phase-5 image/TTS/STT models, and is consumable by
        ``apply_overrides`` end-to-end. The file lives in the resources tree so
        we resolve it from the repo root rather than relying on a relative
        path that may break when the test runner changes working directory."""

        overrides_path = (
            Path(__file__).resolve().parents[3] / "resources" / "models" / "openai.overrides.json"
        )
        assert overrides_path.exists(), f"Expected Phase-5 overrides at {overrides_path}"

        merged = apply_overrides({}, overrides_path)

        # TTS models carry supported_voices so the per-model TTS schema can
        # populate a select widget. The list matches the live OpenAI docs.
        expected_voices = {
            "alloy",
            "ash",
            "ballad",
            "coral",
            "echo",
            "fable",
            "nova",
            "onyx",
            "sage",
            "shimmer",
            "verse",
        }
        for tts_model in ("tts-1", "tts-1-hd", "gpt-4o-mini-tts"):
            assert tts_model in merged, f"Missing OpenAI TTS model '{tts_model}'"
            capabilities = merged[tts_model]["capabilities"]
            assert capabilities["supported_voices"] == sorted(expected_voices)
            assert "text_to_speech" in capabilities["task_types"]

        # GPT-4o Mini TTS is the only one advertising ``instructions``.
        gpt4o = merged["gpt-4o-mini-tts"]["capabilities"]
        assert "instructions" in gpt4o["supported_parameters"]
        assert "instructions" not in merged["tts-1"]["capabilities"]["supported_parameters"]
        assert "instructions" not in merged["tts-1-hd"]["capabilities"]["supported_parameters"]

        # STT models are tagged with ``speech_to_text`` and ``transcription``
        # output so ``ModelQuery`` finds them.
        for stt_model in ("whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"):
            assert stt_model in merged, f"Missing OpenAI STT model '{stt_model}'"
            capabilities = merged[stt_model]["capabilities"]
            assert "speech_to_text" in capabilities["task_types"]
            assert "transcription" in capabilities["output_modalities"]
            assert "response_format" in capabilities["supported_parameters"]

        # Image models cover both generations and the right supported
        # parameters.
        gpt_image = merged["gpt-image-1"]["capabilities"]
        assert gpt_image["output_modalities"] == ["image"]
        assert "image_generation" in gpt_image["task_types"]
        assert {"size", "quality", "background", "n", "output_format"} <= set(
            gpt_image["supported_parameters"]
        )

        dall_e = merged["dall-e-3"]["capabilities"]
        assert "image_generation" in dall_e["task_types"]
        assert "style" in dall_e["supported_parameters"]
        assert "size" in dall_e["supported_parameters"]

    def test_model_to_data_round_trips_supported_voices(self) -> None:
        """The ``_model_to_data`` write path keeps ``supported_voices`` stable so a
        normalized OpenRouter TTS entry survives serialize → ``ModelRegistry.load``."""

        from core.models.discovery import _model_to_data

        model = Model(
            model_id="hexgrad/kokoro-82m",
            name="hexgrad: Kokoro 82M",
            capabilities=Capabilities(
                vision=False,
                tools=False,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=False),
                input_modalities=("text",),
                output_modalities=("speech",),
                supported_parameters=("response_format", "seed"),
                supported_voices=("af_aoede", "af_sky", "am_adam"),
                task_types=("audio_generation", "text_to_speech"),
            ),
            context_window=4096,
            max_output_tokens=None,
        )

        data = _model_to_data(model)

        assert data["capabilities"]["supported_voices"] == [
            "af_aoede",
            "af_sky",
            "am_adam",
        ]


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
    async def test_refresh_models_supports_openai_subscription_discovery_headers(
        self,
        tmp_path: Path,
        openai_subscription_config: ProviderConfig,
    ):
        resources_dir = tmp_path / "resources"
        access_token = jwt_with_openai_account("acct_openai")
        route = respx.get(f"{OPENAI_SUBSCRIPTION_MODELS_URL}?client_version=0.136.0").mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "gpt-5-codex",
                            "display_name": "GPT-5 Codex",
                            "input_modalities": ["text", "image"],
                            "context_window": 272000,
                            "supports_parallel_tool_calls": True,
                        }
                    ]
                },
            )
        )

        result = await refresh_models(
            openai_subscription_config,
            access_token,
            resources_dir,
            credential_connection=openai_subscription_config.connections[0],
        )

        registry = ModelRegistry.load(resources_dir)
        model = registry.get("openai-subscription", "gpt-5-codex")
        request_headers = route.calls.last.request.headers
        assert result["provider_id"] == "openai-subscription"
        assert result["model_count"] == 1
        assert model.name == "GPT-5 Codex"
        assert model.capabilities.tools is True
        assert model.capabilities.json_mode is True
        assert model.capabilities.reasoning.supported is True
        assert set(model.capabilities.supported_parameters) == {
            "tools",
            "response_format",
            "reasoning",
            "parallel_tool_calls",
        }
        assert request_headers["Authorization"] == f"Bearer {access_token}"
        assert request_headers["chatgpt-account-id"] == "acct_openai"
        assert request_headers["OpenAI-Beta"] == "responses=experimental"
        assert request_headers["originator"] == "vbot"
        assert route.calls.last.request.url.params["client_version"] == "0.136.0"

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
        assert model_b.max_output_tokens is None
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

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_skips_catalog_entry_skipped_and_continues(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        class _SkipEntryAdapter:
            @staticmethod
            def normalize_catalog_entry(raw_model: dict, defaults: dict | None) -> Model:
                if raw_model.get("id") == "skip-me":
                    raise CatalogEntrySkipped("skip expected non-chat model")
                return Model(
                    model_id=str(raw_model["id"]),
                    name=str(raw_model.get("name", "Kept Model")),
                    capabilities=Capabilities(
                        vision=False,
                        tools=True,
                        json_mode=True,
                        reasoning=ReasoningCapabilities(supported=False),
                    ),
                    context_window=8192,
                    max_output_tokens=2048,
                )

        provider_config = ProviderConfig(
            id="stub-provider",
            name="Stub Provider",
            adapter="stub_skip_adapter",
            base_url="https://stub-provider.example/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="STUB_PROVIDER_KEY",
                    ),
                )
            ],
            defaults={"max_tokens": 2048},
            models_endpoint="/models",
        )

        monkeypatch.setitem(
            discovery_module._DISCOVERY_ADAPTER_MAP,
            "stub_skip_adapter",
            _SkipEntryAdapter,
        )
        respx.get(STUB_DISCOVERY_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "skip-me", "name": "Skipped Model"},
                        {"id": "keep-me", "name": "Kept Model"},
                    ]
                },
            )
        )
        resources_dir = tmp_path / "resources"

        result = await refresh_models(provider_config, API_KEY, resources_dir)

        registry = ModelRegistry.load(resources_dir)
        assert result["provider_id"] == "stub-provider"
        assert result["model_count"] == 1
        assert registry.get("stub-provider", "keep-me").name == "Kept Model"
        with pytest.raises(KeyError):
            registry.get("stub-provider", "skip-me")

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_wraps_normalizer_value_error_as_discovery_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        class _ErroringAdapter:
            @staticmethod
            def normalize_catalog_entry(raw_model: dict, defaults: dict | None) -> Model:
                raise ValueError("schema mismatch")

        provider_config = ProviderConfig(
            id="stub-provider",
            name="Stub Provider",
            adapter="stub_error_adapter",
            base_url="https://stub-provider.example/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="STUB_PROVIDER_KEY",
                    ),
                )
            ],
            defaults={"max_tokens": 2048},
            models_endpoint="/models",
        )

        monkeypatch.setitem(
            discovery_module._DISCOVERY_ADAPTER_MAP,
            "stub_error_adapter",
            _ErroringAdapter,
        )
        respx.get(STUB_DISCOVERY_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "broken-model", "name": "Broken Model"}]},
            )
        )
        resources_dir = tmp_path / "resources"

        with pytest.raises(ModelDiscoveryError, match="schema mismatch"):
            await refresh_models(provider_config, API_KEY, resources_dir)

        assert (resources_dir / "models" / "stub-provider.raw.json").exists()
        assert not (resources_dir / "models" / "stub-provider.json").exists()

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_fetches_supplementary_openrouter_models(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """OpenRouter discovery fetches STT/TTS models via supplementary API calls."""
        resources_dir = tmp_path / "resources"

        # Main catalog returns a chat model and a multimodal audio model.
        main_models = {
            "data": [
                raw_openrouter_model(model_id="openai/gpt-4o", name="GPT-4o"),
                raw_openrouter_model(
                    model_id="openai/gpt-audio",
                    name="GPT Audio",
                    output_modalities=["text", "audio"],
                ),
            ]
        }
        # Supplementary STT fetch returns a whisper model.
        stt_models = {
            "data": [
                raw_openrouter_model(
                    model_id="openai/whisper-1",
                    name="Whisper 1",
                    input_modalities=["audio"],
                    output_modalities=["transcription"],
                ),
            ]
        }
        # Supplementary TTS fetch returns a TTS model.
        tts_models = {
            "data": [
                raw_openrouter_model(
                    model_id="openai/gpt-4o-mini-tts",
                    name="GPT-4o Mini TTS",
                    input_modalities=["text"],
                    output_modalities=["speech"],
                ),
            ]
        }

        # respx does not distinguish URLs by query params, so use side_effect.
        def openrouter_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "output_modalities=transcription" in url:
                return httpx.Response(200, json=stt_models)
            if "output_modalities=speech" in url:
                return httpx.Response(200, json=tts_models)
            return httpx.Response(200, json=main_models)

        respx.get(OPENROUTER_MODELS_URL).mock(side_effect=openrouter_handler)

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)
        registry = ModelRegistry.load(resources_dir)

        assert result["model_count"] == 4
        assert registry.get("openrouter", "openai/gpt-4o") is not None
        assert registry.get("openrouter", "openai/gpt-audio") is not None
        assert registry.get("openrouter", "openai/whisper-1") is not None
        assert registry.get("openrouter", "openai/gpt-4o-mini-tts") is not None

        # Verify task types are derived correctly
        whisper = registry.get("openrouter", "openai/whisper-1")
        assert "speech_to_text" in whisper.capabilities.task_types

        tts = registry.get("openrouter", "openai/gpt-4o-mini-tts")
        assert "text_to_speech" in tts.capabilities.task_types

        # GPT Audio should have audio_generation but NOT text_to_speech
        gpt_audio = registry.get("openrouter", "openai/gpt-audio")
        assert "audio_generation" in gpt_audio.capabilities.task_types
        assert "text_to_speech" not in gpt_audio.capabilities.task_types

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_deduplicates_supplementary_models(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """Supplementary fetches that return already-known models are deduplicated."""
        resources_dir = tmp_path / "resources"

        # Main catalog includes gpt-audio; supplementary also returns it.
        main_models = {
            "data": [
                raw_openrouter_model(
                    model_id="openai/gpt-audio",
                    name="GPT Audio",
                    output_modalities=["text", "audio"],
                ),
            ]
        }
        duplicate_stt = {
            "data": [
                # Duplicate of gpt-audio from main catalog.
                raw_openrouter_model(
                    model_id="openai/gpt-audio",
                    name="GPT Audio",
                    output_modalities=["text", "audio"],
                ),
                raw_openrouter_model(
                    model_id="openai/whisper-1",
                    name="Whisper 1",
                    input_modalities=["audio"],
                    output_modalities=["transcription"],
                ),
            ]
        }
        empty_speech: dict[str, object] = {"data": []}

        # respx does not distinguish URLs by query params, so use side_effect.
        def openrouter_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "output_modalities=transcription" in url:
                return httpx.Response(200, json=duplicate_stt)
            if "output_modalities=speech" in url:
                return httpx.Response(200, json=empty_speech)
            return httpx.Response(200, json=main_models)

        respx.get(OPENROUTER_MODELS_URL).mock(side_effect=openrouter_handler)

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        # gpt-audio should appear only once.
        assert result["model_count"] == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_supplementary_fetch_failure_does_not_block(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """If a supplementary fetch fails, discovery still completes with main models."""
        resources_dir = tmp_path / "resources"

        main_models = {
            "data": [
                raw_openrouter_model(model_id="openai/gpt-4o", name="GPT-4o"),
            ]
        }

        # respx does not distinguish URLs by query params, so use side_effect.
        def openrouter_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "output_modalities=transcription" in url:
                return httpx.Response(500, text="Internal Server Error")
            if "output_modalities=speech" in url:
                return httpx.Response(500, text="Internal Server Error")
            return httpx.Response(200, json=main_models)

        respx.get(OPENROUTER_MODELS_URL).mock(side_effect=openrouter_handler)

        result = await refresh_models(openrouter_config, API_KEY, resources_dir)

        # Main models are still available.
        assert result["model_count"] == 1
        registry = ModelRegistry.load(resources_dir)
        assert registry.get("openrouter", "openai/gpt-4o") is not None

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_raw_file_records_supplementary_models_once(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
    ):
        """Supplementary models appear exactly once in the persisted raw payload."""
        resources_dir = tmp_path / "resources"

        main_models = {
            "data": [raw_openrouter_model(model_id="openai/gpt-4o", name="GPT-4o")],
        }
        stt_models = {
            "data": [
                raw_openrouter_model(
                    model_id="openai/whisper-1",
                    name="Whisper 1",
                    input_modalities=["audio"],
                    output_modalities=["transcription"],
                ),
            ]
        }

        def openrouter_handler(request: httpx.Request) -> httpx.Response:
            if "output_modalities=transcription" in str(request.url):
                return httpx.Response(200, json=stt_models)
            return httpx.Response(200, json=main_models)

        respx.get(OPENROUTER_MODELS_URL).mock(side_effect=openrouter_handler)

        await refresh_models(openrouter_config, API_KEY, resources_dir)

        raw_output_data = json.loads(
            (resources_dir / "models" / "openrouter.raw.json").read_text(encoding="utf-8")
        )
        raw_ids = [model["id"] for model in raw_output_data["raw_response"]["data"]]

        # The supplementary STT model must not be duplicated in the raw payload.
        assert raw_ids.count("openai/whisper-1") == 1
        assert sorted(raw_ids) == ["openai/gpt-4o", "openai/whisper-1"]
