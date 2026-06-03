"""Tests for Model dataclass and ModelRegistry."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.models.models import (
    Capabilities,
    Model,
    ModelRegistry,
    ReasoningCapabilities,
    derive_model_task_types,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
RESOURCES_DIR = PROJECT_ROOT / "resources"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Clear the registry cache before and after each test for independence."""
    ModelRegistry._cache.clear()
    yield
    ModelRegistry._cache.clear()


# ---------------------------------------------------------------------------
# ReasoningCapabilities
# ---------------------------------------------------------------------------


class TestReasoningCapabilities:
    def test_fields(self):
        caps = ReasoningCapabilities(supported=True)
        assert caps.supported is True

    def test_frozen(self):
        caps = ReasoningCapabilities(supported=True)
        with pytest.raises(FrozenInstanceError):
            caps.supported = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_fields(self):
        reasoning = ReasoningCapabilities(supported=True)
        caps = Capabilities(
            vision=True,
            tools=False,
            json_mode=True,
            reasoning=reasoning,
            input_modalities=("Text", "Image", "image"),
            output_modalities=("Text", "Audio"),
            supported_parameters=("response_format", "tools", "tools"),
        )
        assert caps.vision is True
        assert caps.tools is False
        assert caps.json_mode is True
        assert caps.reasoning is reasoning
        assert caps.input_modalities == ("text", "image")
        assert caps.output_modalities == ("text", "audio")
        assert caps.supported_parameters == ("response_format", "tools")
        assert caps.task_types == (
            "chat",
            "text_output",
            "image_input",
            "image_understanding",
            "audio_generation",
        )

    def test_legacy_vision_derives_text_image_input(self):
        caps = Capabilities(
            vision=True,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        )

        assert caps.input_modalities == ("text", "image")
        assert caps.output_modalities == ("text",)
        assert "image_understanding" in caps.task_types

    def test_derives_generation_task_types(self):
        assert derive_model_task_types(("text", "image"), ("text", "image")) == (
            "chat",
            "text_output",
            "image_input",
            "image_understanding",
            "image_generation",
        )
        # Generic "audio" output does NOT imply text_to_speech —
        # only "speech" output does.
        assert derive_model_task_types(("text",), ("audio",)) == ("audio_generation",)
        # Dedicated TTS models have "speech" in output_modalities.
        assert derive_model_task_types(("text",), ("speech",)) == (
            "audio_generation",
            "text_to_speech",
        )
        # Dedicated STT models have "transcription" in output_modalities.
        # They also get audio_input since they accept audio.
        assert derive_model_task_types(("audio",), ("transcription",)) == (
            "text_output",
            "audio_input",
            "speech_to_text",
        )
        # Multimodal models with audio input and text output get speech_to_text.
        assert derive_model_task_types(("text", "audio"), ("text",)) == (
            "chat",
            "text_output",
            "audio_input",
            "speech_to_text",
        )

    def test_frozen(self):
        reasoning = ReasoningCapabilities(supported=False)
        caps = Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=reasoning,
        )
        with pytest.raises(FrozenInstanceError):
            caps.vision = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestModel:
    def test_fields(self):
        reasoning = ReasoningCapabilities(supported=True)
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=reasoning,
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
        )
        assert model.model_id == "gpt-5.2"
        assert model.name == "GPT-5.2"
        assert model.capabilities is capabilities
        assert model.context_window == 128000
        assert model.max_output_tokens == 16384
        assert model.metadata == {}

    def test_metadata_field_is_optional_and_immutable(self):
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
            metadata={"github_copilot": {"supported_endpoints": ["/responses"]}},
        )

        assert model.metadata["github_copilot"]["supported_endpoints"] == ("/responses",)
        with pytest.raises(TypeError):
            model.metadata["github_copilot"] = {}  # type: ignore[index]

    def test_frozen(self):
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
        )
        with pytest.raises(FrozenInstanceError):
            model.model_id = "changed"  # type: ignore[misc]

    def test_nested_capabilities_frozen(self):
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
        )
        with pytest.raises(FrozenInstanceError):
            model.capabilities.vision = False  # type: ignore[misc]

    def test_nested_reasoning_frozen(self):
        reasoning = ReasoningCapabilities(supported=True)
        capabilities = Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=reasoning,
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16384,
        )
        with pytest.raises(FrozenInstanceError):
            model.capabilities.reasoning.supported = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelRegistry — loading and lookup
# ---------------------------------------------------------------------------


class TestModelRegistryLoad:
    def test_load_from_json_fixtures(self):
        registry = ModelRegistry.load(FIXTURES_DIR)

        alpha = registry.get("test_provider_a", "model-alpha")
        assert alpha.model_id == "model-alpha"
        assert alpha.name == "Model Alpha"
        assert alpha.capabilities.vision is True
        assert alpha.capabilities.tools is False
        assert alpha.capabilities.json_mode is True
        assert alpha.capabilities.reasoning.supported is False
        assert alpha.capabilities.input_modalities == ("text", "image")
        assert alpha.capabilities.output_modalities == ("text",)
        assert "image_understanding" in alpha.capabilities.task_types
        assert alpha.context_window == 32000
        assert alpha.max_output_tokens == 4096

    def test_load_multiple_providers(self):
        registry = ModelRegistry.load(FIXTURES_DIR)

        beta = registry.get("test_provider_b", "model-beta")
        assert beta.model_id == "model-beta"
        assert beta.name == "Model Beta"
        assert beta.capabilities.reasoning.supported is True
        assert beta.context_window == 128000

        gamma = registry.get("test_provider_b", "model-gamma")
        assert gamma.model_id == "model-gamma"
        assert gamma.name == "Model Gamma"
        assert gamma.capabilities.vision is False
        assert gamma.capabilities.reasoning.supported is False

    def test_load_reads_optional_metadata(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("github-copilot.json").write_text(
            """
            {
              "provider_id": "github-copilot",
              "models": {
                "gpt-5.2": {
                  "name": "GPT-5.2",
                  "capabilities": {
                    "vision": true,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": true}
                  },
                  "context_window": 264000,
                  "max_output_tokens": 64000,
                  "metadata": {
                    "github_copilot": {
                      "vendor": "OpenAI",
                      "family": "gpt-5.2",
                      "supported_endpoints": ["/responses", "/chat/completions"]
                    }
                  }
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)
        model = registry.get("github-copilot", "gpt-5.2")

        assert model.metadata["github_copilot"]["vendor"] == "OpenAI"
        assert model.metadata["github_copilot"]["supported_endpoints"] == (
            "/responses",
            "/chat/completions",
        )

    def test_load_preserves_unknown_max_output_tokens(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("test-provider.json").write_text(
            """
            {
              "provider_id": "test-provider",
              "models": {
                "minimal-model": {
                  "name": "Minimal Model",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": false,
                    "reasoning": {"supported": false}
                  },
                  "context_window": 0,
                  "max_output_tokens": null
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)
        model = registry.get("test-provider", "minimal-model")

        assert model.max_output_tokens is None

    def test_load_catalog_without_metadata_keeps_empty_mapping(self):
        registry = ModelRegistry.load(FIXTURES_DIR)

        model = registry.get("test_provider_a", "model-alpha")

        assert model.metadata == {}

    def test_cache_returns_same_instance(self):
        registry_first = ModelRegistry.load(FIXTURES_DIR)
        registry_second = ModelRegistry.load(FIXTURES_DIR)
        assert registry_first is registry_second

    def test_invalidate_removes_cache_entry_and_next_load_reads_disk(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        model_file = models_dir / "test_provider.json"
        model_file.write_text(
            """
            {
              "provider_id": "test_provider",
              "models": {
                "model-a": {
                  "name": "Original",
                  "capabilities": {
                    "vision": false,
                    "tools": false,
                    "json_mode": false,
                    "reasoning": {"supported": false}
                  },
                  "context_window": 1000,
                  "max_output_tokens": 100
                }
              }
            }
            """,
            encoding="utf-8",
        )
        registry_first = ModelRegistry.load(tmp_path)
        model_file.write_text(
            """
            {
              "provider_id": "test_provider",
              "models": {
                "model-a": {
                  "name": "Updated",
                  "capabilities": {
                    "vision": false,
                    "tools": false,
                    "json_mode": false,
                    "reasoning": {"supported": false}
                  },
                  "context_window": 1000,
                  "max_output_tokens": 100
                }
              }
            }
            """,
            encoding="utf-8",
        )

        ModelRegistry.invalidate(tmp_path)
        registry_second = ModelRegistry.load(tmp_path)

        assert registry_second is not registry_first
        assert registry_second.get("test_provider", "model-a").name == "Updated"

    def test_load_ignores_colocated_override_files(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("openrouter.json").write_text(
            """
                        {
                            "provider_id": "openrouter",
                            "models": {
                                "model-a": {
                                    "name": "Model A",
                                    "capabilities": {
                                        "vision": false,
                                        "tools": false,
                                        "json_mode": false,
                                        "reasoning": {"supported": false}
                                    },
                                    "context_window": 1000,
                                    "max_output_tokens": 100
                                }
                            }
                        }
                        """,
            encoding="utf-8",
        )
        models_dir.joinpath("openrouter.overrides.json").write_text(
            """
                        {
                            "provider_id": "openrouter",
                            "models": {
                                "model-a": {"name": "Corrected Model A"},
                                "override-only": {"name": "Override Only"}
                            }
                        }
                        """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.get("openrouter", "model-a").name == "Model A"
        with pytest.raises(KeyError):
            registry.get("openrouter", "override-only")

    def test_load_ignores_colocated_raw_files(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("test_provider.json").write_text(
            """
                        {
                            "provider_id": "test_provider",
                            "models": {
                                "model-a": {
                                    "name": "Model A",
                                    "capabilities": {
                                        "vision": false,
                                        "tools": false,
                                        "json_mode": false,
                                        "reasoning": {"supported": false}
                                    },
                                    "context_window": 1000,
                                    "max_output_tokens": 100
                                }
                            }
                        }
                        """,
            encoding="utf-8",
        )
        models_dir.joinpath("test_provider.raw.json").write_text(
            """
                        {
                            "provider_id": "test_provider",
                            "fetched_at": "2026-01-01T00:00:00+00:00",
                            "raw_response": {
                                "data": []
                            }
                        }
                        """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        models = registry.list_for_provider("test_provider")
        assert len(models) == 1
        assert models[0].model_id == "model-a"


# ---------------------------------------------------------------------------
# ModelRegistry — get()
# ---------------------------------------------------------------------------


class TestModelRegistryGet:
    def test_get_existing_model(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        model = registry.get("test_provider_a", "model-alpha")
        assert model.model_id == "model-alpha"

    def test_get_missing_provider_raises_key_error(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        with pytest.raises(KeyError, match="nonexistent_provider"):
            registry.get("nonexistent_provider", "some-model")

    def test_get_missing_model_raises_key_error(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        with pytest.raises(KeyError, match="nonexistent-model"):
            registry.get("test_provider_a", "nonexistent-model")

    def test_get_model_wrong_provider_raises_key_error(self):
        """A model that exists under one provider is not found under another."""
        registry = ModelRegistry.load(FIXTURES_DIR)
        with pytest.raises(KeyError):
            registry.get("test_provider_b", "model-alpha")


# ---------------------------------------------------------------------------
# ModelRegistry — list_for_provider()
# ---------------------------------------------------------------------------


class TestModelRegistryListForProvider:
    def test_list_single_provider(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        models = registry.list_for_provider("test_provider_a")
        assert len(models) == 1
        assert models[0].model_id == "model-alpha"

    def test_list_multi_model_provider_sorted(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        models = registry.list_for_provider("test_provider_b")
        assert len(models) == 2
        # Sorted by model_id: model-beta comes before model-gamma
        assert models[0].model_id == "model-beta"
        assert models[1].model_id == "model-gamma"

    def test_list_nonexistent_provider_returns_empty(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        models = registry.list_for_provider("nonexistent_provider")
        assert models == []


# ---------------------------------------------------------------------------
# ModelRegistry — real resource files
# ---------------------------------------------------------------------------


class TestModelRegistryRealResources:
    """Smoke-check: shipped sanitized JSON files load without error."""

    @pytest.fixture(autouse=True)
    def _reset_cache_for_real_resources(self):
        ModelRegistry._cache.clear()
        yield
        ModelRegistry._cache.clear()

    @pytest.mark.parametrize(
        "provider_id",
        ["openai", "openrouter", "anthropic", "github-copilot", "mistral"],
    )
    def test_provider_loads_and_has_models(self, provider_id: str):
        registry = ModelRegistry.load(RESOURCES_DIR)
        models = registry.list_for_provider(provider_id)

        assert len(models) > 0
        for model in models:
            assert model.model_id
            assert model.name
            assert isinstance(model.capabilities.vision, bool)
            assert isinstance(model.capabilities.tools, bool)
            assert isinstance(model.capabilities.json_mode, bool)
            assert isinstance(model.capabilities.reasoning.supported, bool)
            assert isinstance(model.capabilities.input_modalities, tuple)
            assert isinstance(model.capabilities.output_modalities, tuple)
            assert isinstance(model.capabilities.supported_parameters, tuple)
            assert isinstance(model.capabilities.task_types, tuple)
            assert isinstance(model.context_window, int)
            assert model.context_window >= 0
            if model.max_output_tokens is not None:
                assert isinstance(model.max_output_tokens, int)
                assert model.max_output_tokens >= 0
