"""Tests for dynamic model discovery."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

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
_SIMPLE_MODELS_URL = "https://simple.example/v1/models"
API_KEY = "test-openrouter-key"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _simple_compatible_config() -> ProviderConfig:
    """Minimal OpenAI-compatible provider: one fetch per refresh, no supplementary calls."""
    return ProviderConfig(
        id="simple",
        name="Simple",
        adapter="openai_compatible",
        base_url="https://simple.example/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="SIMPLE_KEY",
                ),
            )
        ],
        defaults={},
        models_endpoint="/models",
    )


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
    """Provider with one OAuth connection (subscription) for Codex discovery.

    After the openai-provider merge there is a single ``openai`` provider
    with two connections; for unit-testing the connection-aware discovery
    pipeline we model that state with a connection-level
    ``base_url``/``models_endpoint`` and the Codex adapter.
    """

    return ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="ChatGPT Plus/Pro",
                base_url="https://chatgpt.com/backend-api",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                ),
                mode="codex_responses",
                models_endpoint="/codex/models",
            )
        ],
        defaults={"max_tokens": 8192},
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


class TestTypedReasoningSerialization:
    """The discovery serializer + validator round-trip the typed reasoning
    shape and ``family``, and reject malformed reasoning blocks."""

    def test_model_to_data_omits_unset_reasoning_control_fields(self) -> None:
        """A supported model with no projected ladder serializes back to the
        bare ``{"supported": true}`` form — control/levels/budget_max are
        omitted when unset, matching how connections/metadata are omitted."""

        from core.models.discovery import _model_to_data

        model = Model(
            model_id="minimal",
            name="Minimal",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=32000,
            max_output_tokens=4096,
        )

        data = _model_to_data(model)

        assert data["capabilities"]["reasoning"] == {"supported": True}
        assert "family" not in data

    def test_model_to_data_emits_typed_reasoning_and_family(self) -> None:
        from core.models.discovery import _model_to_data

        model = Model(
            model_id="levels-model",
            name="Levels Model",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control="levels",
                    levels=("low", "medium", "high"),
                ),
            ),
            context_window=128000,
            max_output_tokens=16000,
            family="gpt-5.2",
        )

        data = _model_to_data(model)

        assert data["capabilities"]["reasoning"] == {
            "supported": True,
            "control": "levels",
            "levels": ["low", "medium", "high"],
        }
        assert data["family"] == "gpt-5.2"

    def test_model_to_data_emits_budget_max(self) -> None:
        from core.models.discovery import _model_to_data

        model = Model(
            model_id="budget-model",
            name="Budget Model",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control="budget",
                    budget_max=32000,
                ),
            ),
            context_window=200000,
            max_output_tokens=64000,
        )

        data = _model_to_data(model)

        assert data["capabilities"]["reasoning"] == {
            "supported": True,
            "control": "budget",
            "budget_max": 32000,
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_round_trips_typed_reasoning_shape(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A normalized model carrying the typed reasoning ladder survives
        serialize → write → ``ModelRegistry.load`` with the control fields
        intact. The adapter is stubbed so the test is pure (no network)."""

        class _TypedReasoningAdapter:
            @staticmethod
            def normalize_catalog_entry(raw_model: dict, defaults: dict | None) -> Model:
                return Model(
                    model_id=str(raw_model["id"]),
                    name=str(raw_model.get("name", "Typed")),
                    capabilities=Capabilities(
                        vision=False,
                        tools=True,
                        json_mode=True,
                        reasoning=ReasoningCapabilities(
                            supported=True,
                            control="levels",
                            levels=("low", "medium", "high"),
                        ),
                    ),
                    context_window=128000,
                    max_output_tokens=16000,
                    family="deepseek-v4",
                )

        provider_config = ProviderConfig(
            id="stub-provider",
            name="Stub Provider",
            adapter="stub_typed_adapter",
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
            defaults={},
            models_endpoint="/models",
        )
        monkeypatch.setitem(
            discovery_module._DISCOVERY_ADAPTER_MAP,
            "stub_typed_adapter",
            _TypedReasoningAdapter,
        )
        respx.get(STUB_DISCOVERY_MODELS_URL).mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "typed-model", "name": "Typed Model"}]}
            )
        )
        resources_dir = tmp_path / "resources"

        await refresh_models(provider_config, API_KEY, resources_dir)

        registry = ModelRegistry.load(resources_dir)
        model = registry.get("stub-provider", "typed-model")
        assert model.capabilities.reasoning.control == "levels"
        assert model.capabilities.reasoning.levels == ("low", "medium", "high")
        assert model.family == "deepseek-v4"

    def test_override_rejects_bad_reasoning_control(self, tmp_path: Path) -> None:
        overrides_path = tmp_path / "openrouter.overrides.json"
        override = model_data("Bad Control")
        override["capabilities"]["reasoning"] = {"supported": True, "control": "bogus"}
        overrides_path.write_text(
            json.dumps({"provider_id": "openrouter", "models": {"model-x": override}}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="control"):
            apply_overrides({}, overrides_path)

    def test_override_rejects_bad_reasoning_level_value(self, tmp_path: Path) -> None:
        overrides_path = tmp_path / "openrouter.overrides.json"
        override = model_data("Bad Levels")
        override["capabilities"]["reasoning"] = {
            "supported": True,
            "control": "levels",
            "levels": ["low", "ultra"],
        }
        overrides_path.write_text(
            json.dumps({"provider_id": "openrouter", "models": {"model-y": override}}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="levels"):
            apply_overrides({}, overrides_path)

    def test_override_accepts_minimal_supported_reasoning(self, tmp_path: Path) -> None:
        """The validator keeps accepting the bare ``{"supported": true}`` form
        — Phase 1 has no ladder data, so this must stay valid."""

        overrides_path = tmp_path / "openrouter.overrides.json"
        override = model_data("Minimal Reasoning")
        override["capabilities"]["reasoning"] = {"supported": True}
        overrides_path.write_text(
            json.dumps({"provider_id": "openrouter", "models": {"model-z": override}}),
            encoding="utf-8",
        )

        merged = apply_overrides({}, overrides_path)

        assert merged["model-z"]["capabilities"]["reasoning"] == {"supported": True}

    def test_override_rejects_non_string_family(self, tmp_path: Path) -> None:
        overrides_path = tmp_path / "openrouter.overrides.json"
        override = model_data("Bad Family")
        override["family"] = 123
        overrides_path.write_text(
            json.dumps({"provider_id": "openrouter", "models": {"model-f": override}}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="family"):
            apply_overrides({}, overrides_path)


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
    async def test_refresh_models_tags_models_with_selected_connection_id(
        self,
        tmp_path: Path,
        openai_subscription_config: ProviderConfig,
    ):
        """Refresh of a connection stamps every catalog entry with its local id.

        The merged catalog is loaded through :class:`ModelRegistry` and the
        per-model ``connections`` tuple must contain the connection that
        produced the fetch. Other models on disk from a different
        connection (if any) would be preserved — this is the no-existing
        baseline.
        """

        resources_dir = tmp_path / "resources"
        access_token = jwt_with_openai_account("acct_openai")
        respx.get(OPENAI_SUBSCRIPTION_MODELS_URL).mock(
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
        model = registry.get("openai", "gpt-5-codex")
        catalog_data = json.loads(
            (resources_dir / "models" / "openai.json").read_text(encoding="utf-8")
        )
        assert result["provider_id"] == "openai"
        assert result["model_count"] == 1
        assert model.connections == ("subscription",)
        assert model.name == "GPT-5 Codex"
        assert catalog_data["models"]["gpt-5-codex"]["connections"] == ["subscription"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_merges_models_from_other_connection(
        self,
        tmp_path: Path,
        openai_subscription_config: ProviderConfig,
    ):
        """A second refresh of a different connection leaves earlier entries alone.

        Existing entries tagged with the *other* connection are preserved
        in the shared catalog; entries tagged with the refreshed
        connection are replaced. The catalog is loaded end-to-end through
        :class:`ModelRegistry` to confirm the per-model
        ``connections`` tuple round-trips.
        """

        resources_dir = tmp_path / "resources"
        catalog_path = resources_dir / "models" / "openai.json"
        existing_data = {
            "provider_id": "openai",
            "source": "discovery",
            "fetched_at": "2026-05-08T19:08:00+00:00",
            "models": {
                "gpt-5.2": {
                    "name": "GPT-5.2",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": ["tools", "response_format", "reasoning"],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 256000,
                    "max_output_tokens": 32000,
                    "connections": ["api-key"],
                },
                "stale-subscription-model": {
                    "name": "Stale Subscription Model",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True},
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": ["tools"],
                        "task_types": ["chat"],
                    },
                    "context_window": 128000,
                    "max_output_tokens": 16000,
                    "connections": ["subscription"],
                },
            },
        }
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(json.dumps(existing_data, indent=2), encoding="utf-8")

        access_token = jwt_with_openai_account("acct_openai")
        respx.get(OPENAI_SUBSCRIPTION_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "gpt-5.4",
                            "display_name": "GPT-5.4",
                            "input_modalities": ["text", "image"],
                            "context_window": 256000,
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
        merged_data = json.loads(catalog_path.read_text(encoding="utf-8"))
        assert result["provider_id"] == "openai"
        assert result["model_count"] == 2

        # The api-key entry is preserved untouched.
        assert "gpt-5.2" in merged_data["models"]
        assert merged_data["models"]["gpt-5.2"]["connections"] == ["api-key"]
        api_key_model = registry.get("openai", "gpt-5.2")
        assert api_key_model.connections == ("api-key",)

        # The stale subscription entry is replaced by the fresh fetch.
        assert "stale-subscription-model" not in merged_data["models"]
        assert "gpt-5.4" in merged_data["models"]
        assert merged_data["models"]["gpt-5.4"]["connections"] == ["subscription"]
        fresh_model = registry.get("openai", "gpt-5.4")
        assert fresh_model.connections == ("subscription",)

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_uses_connection_endpoint_and_base_url(
        self,
        tmp_path: Path,
        openai_subscription_config: ProviderConfig,
    ):
        """The connection's ``base_url`` + ``models_endpoint`` drive the fetch URL.

        The provider-level defaults would point at the platform endpoint
        (a totally different host); refresh must combine the connection
        values into the request URL and target Codex's ``/codex/models``.
        """

        resources_dir = tmp_path / "resources"
        access_token = jwt_with_openai_account("acct_openai")
        expected_url = f"{OPENAI_SUBSCRIPTION_MODELS_URL}?client_version=0.136.0"
        route = respx.get(expected_url).mock(
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

        await refresh_models(
            openai_subscription_config,
            access_token,
            resources_dir,
            credential_connection=openai_subscription_config.connections[0],
        )

        request = route.calls.last.request
        assert str(request.url).split("?")[0] == OPENAI_SUBSCRIPTION_MODELS_URL
        assert request.url.params["client_version"] == "0.136.0"
        assert request.headers["Authorization"] == f"Bearer {access_token}"
        assert request.headers["chatgpt-account-id"] == "acct_openai"
        assert request.headers["OpenAI-Beta"] == "responses=experimental"
        assert request.headers["originator"] == "vbot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_without_endpoint_raises_value_error(
        self,
        tmp_path: Path,
    ):
        """A connection with no effective ``models_endpoint`` is rejected loudly."""

        config = ProviderConfig(
            id="openai",
            name="OpenAI",
            adapter="openai",
            base_url="https://api.openai.com/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="OPENAI_API_KEY",
                    ),
                )
            ],
            defaults={"max_tokens": 8192},
        )

        with pytest.raises(ValueError, match="does not define a models_endpoint"):
            await refresh_models(
                config,
                "sk-test",
                tmp_path / "resources",
                credential_connection=config.connections[0],
            )

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
    async def test_refresh_models_logs_warning_on_catalog_refresh_failure(
        self,
        tmp_path: Path,
        openrouter_config: ProviderConfig,
        caplog: Any,
    ):
        """A primary catalog-refresh failure logs a warning (no traceback) before raising."""

        respx.get(OPENROUTER_MODELS_URL).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        with (
            caplog.at_level(logging.WARNING, logger="vbot.models.discovery"),
            pytest.raises(ModelDiscoveryError),
        ):
            await refresh_models(openrouter_config, API_KEY, tmp_path / "resources")

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        failure_record = next(
            r for r in warning_records if "Model catalog refresh failed" in r.getMessage()
        )
        assert openrouter_config.id in failure_record.getMessage()
        assert failure_record.exc_info is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_retries_transient_status_then_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A retryable status (503) is re-issued with backoff before succeeding."""

        # Skip the real backoff sleep so the retry path stays fast.
        async def _no_sleep(_delay: float) -> None:
            return None

        monkeypatch.setattr("core.utils.retry.asyncio.sleep", _no_sleep)

        responses = [
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(200, json={"data": [{"id": "model-a", "name": "Model A"}]}),
        ]
        route = respx.get(_SIMPLE_MODELS_URL).mock(side_effect=responses)

        result = await refresh_models(_simple_compatible_config(), API_KEY, tmp_path / "resources")

        assert route.call_count == 2
        assert result["model_count"] == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_does_not_retry_fatal_status(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A fatal status (404) aborts immediately without retrying."""

        async def _fail_if_called(_delay: float) -> None:
            raise AssertionError("fatal status must not trigger a retry sleep")

        monkeypatch.setattr("core.utils.retry.asyncio.sleep", _fail_if_called)
        route = respx.get(_SIMPLE_MODELS_URL).mock(return_value=httpx.Response(404, text="Not Found"))

        with pytest.raises(ModelDiscoveryError, match="Model discovery failed"):
            await refresh_models(_simple_compatible_config(), API_KEY, tmp_path / "resources")

        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_retries_transport_error_then_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A transient transport failure is re-issued before succeeding."""

        async def _no_sleep(_delay: float) -> None:
            return None

        monkeypatch.setattr("core.utils.retry.asyncio.sleep", _no_sleep)

        responses = [
            httpx.ConnectError("connection reset"),
            httpx.Response(200, json={"data": [{"id": "model-a", "name": "Model A"}]}),
        ]
        route = respx.get(_SIMPLE_MODELS_URL).mock(side_effect=responses)

        result = await refresh_models(_simple_compatible_config(), API_KEY, tmp_path / "resources")

        assert route.call_count == 2
        assert result["model_count"] == 1

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
