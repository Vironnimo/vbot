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

    def test_typed_control_fields_default_to_absent(self):
        """The minimal ``supported``-only form leaves the control fields unset,
        so a ``{"supported": true}`` model with no projected ladder is valid."""

        caps = ReasoningCapabilities(supported=True)

        assert caps.control is None
        assert caps.levels == ()
        assert caps.budget_max is None

    def test_levels_control_carries_ladder(self):
        caps = ReasoningCapabilities(
            supported=True,
            control="levels",
            levels=("low", "medium", "high"),
        )

        assert caps.control == "levels"
        assert caps.levels == ("low", "medium", "high")
        assert caps.budget_max is None

    def test_budget_control_carries_max(self):
        caps = ReasoningCapabilities(supported=True, control="budget", budget_max=32000)

        assert caps.control == "budget"
        assert caps.budget_max == 32000
        assert caps.levels == ()

    def test_on_off_control(self):
        caps = ReasoningCapabilities(supported=True, control="on_off")

        assert caps.control == "on_off"
        assert caps.levels == ()
        assert caps.budget_max is None

    def test_frozen(self):
        caps = ReasoningCapabilities(supported=True)
        with pytest.raises(FrozenInstanceError):
            caps.supported = False  # type: ignore[misc]

    def test_typed_control_fields_frozen(self):
        caps = ReasoningCapabilities(supported=True, control="levels", levels=("high",))
        with pytest.raises(FrozenInstanceError):
            caps.control = "on_off"  # type: ignore[misc]


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
        assert caps.supported_voices == ()
        assert caps.task_types == (
            "chat",
            "text_output",
            "image_input",
            "image_understanding",
            "audio_generation",
        )

    def test_supported_voices_default_to_empty_tuple(self):
        caps = Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        )

        assert caps.supported_voices == ()

    def test_supported_voices_normalizes_dedupes_and_sorts(self):
        caps = Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
            supported_voices=(" af_sky ", "af_aoede", "af_sky", ""),
        )

        assert caps.supported_voices == ("af_aoede", "af_sky")

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
        # Dedicated embedding models have "embeddings" in output_modalities.
        # They are NOT tagged chat/text_output — their output is a vector,
        # not text. Mirror of the "speech" → text_to_speech alias.
        assert derive_model_task_types(("text",), ("embeddings",)) == ("text_embedding",)

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

    def test_family_defaults_to_empty_string(self):
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=128000,
            max_output_tokens=16384,
        )

        assert model.family == ""

    def test_family_is_first_class_field(self):
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=128000,
            max_output_tokens=16384,
            family="gpt-5.2",
        )

        assert model.family == "gpt-5.2"

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

    def test_embedding_model_derives_text_embedding_task_type(self):
        """A Model with output_modalities=("embeddings",) and no explicit
        task_types derives task_types=("text_embedding",). Mirrors the
        speech → text_to_speech alias.
        """

        capabilities = Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
            output_modalities=("embeddings",),
        )
        model = Model(
            model_id="text-embedding-3-small",
            name="Text Embedding 3 Small",
            capabilities=capabilities,
            context_window=8192,
            max_output_tokens=None,
        )

        assert model.capabilities.task_types == ("text_embedding",)


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

    def test_load_reads_supported_voices_from_capabilities(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("speech-provider.json").write_text(
            """
            {
              "provider_id": "speech-provider",
              "models": {
                "kokoro-tts": {
                  "name": "Kokoro TTS",
                  "capabilities": {
                    "vision": false,
                    "tools": false,
                    "json_mode": true,
                    "reasoning": {"supported": false},
                    "input_modalities": ["text"],
                    "output_modalities": ["speech"],
                    "supported_parameters": ["response_format", "seed"],
                    "supported_voices": ["af_sky", "af_aoede", "af_bella"],
                    "task_types": ["text_to_speech", "audio_generation"]
                  },
                  "context_window": 4096,
                  "max_output_tokens": null
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)
        model = registry.get("speech-provider", "kokoro-tts")

        assert model.capabilities.supported_voices == ("af_aoede", "af_bella", "af_sky")

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
# ModelRegistry — typed reasoning + family on the load path
# ---------------------------------------------------------------------------


class TestModelRegistryTypedReasoning:
    def test_loads_levels_control_model(self, tmp_path: Path):
        """A model with ``control: levels`` and a ladder loads with the typed
        fields populated."""

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("typed.json").write_text(
            """
            {
              "provider_id": "typed",
              "models": {
                "levels-model": {
                  "name": "Levels Model",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {
                      "supported": true,
                      "control": "levels",
                      "levels": ["low", "medium", "high"]
                    }
                  },
                  "context_window": 128000,
                  "max_output_tokens": 16000
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)
        reasoning = registry.get("typed", "levels-model").capabilities.reasoning

        assert reasoning.supported is True
        assert reasoning.control == "levels"
        assert reasoning.levels == ("low", "medium", "high")
        assert reasoning.budget_max is None

    def test_loads_on_off_control_model(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("typed.json").write_text(
            """
            {
              "provider_id": "typed",
              "models": {
                "on-off-model": {
                  "name": "On Off Model",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": true, "control": "on_off"}
                  },
                  "context_window": 64000,
                  "max_output_tokens": 8000
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)
        reasoning = registry.get("typed", "on-off-model").capabilities.reasoning

        assert reasoning.supported is True
        assert reasoning.control == "on_off"
        assert reasoning.levels == ()
        assert reasoning.budget_max is None

    def test_loads_budget_control_model(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("typed.json").write_text(
            """
            {
              "provider_id": "typed",
              "models": {
                "budget-model": {
                  "name": "Budget Model",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": true, "control": "budget", "budget_max": 32000}
                  },
                  "context_window": 200000,
                  "max_output_tokens": 64000
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)
        reasoning = registry.get("typed", "budget-model").capabilities.reasoning

        assert reasoning.supported is True
        assert reasoning.control == "budget"
        assert reasoning.budget_max == 32000
        assert reasoning.levels == ()

    def test_loads_unsupported_reasoning_model(self, tmp_path: Path):
        """``{"supported": false}`` loads with no control fields set."""

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("typed.json").write_text(
            """
            {
              "provider_id": "typed",
              "models": {
                "plain-model": {
                  "name": "Plain Model",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": false}
                  },
                  "context_window": 32000,
                  "max_output_tokens": 4096
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)
        reasoning = registry.get("typed", "plain-model").capabilities.reasoning

        assert reasoning.supported is False
        assert reasoning.control is None
        assert reasoning.levels == ()
        assert reasoning.budget_max is None

    def test_loads_minimal_supported_reasoning_without_control(self, tmp_path: Path):
        """A supported model with no projected ladder yet loads as the bare
        ``{"supported": true}`` form — Phase 1 has no ladder data."""

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("typed.json").write_text(
            """
            {
              "provider_id": "typed",
              "models": {
                "minimal-reasoning": {
                  "name": "Minimal Reasoning",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": true}
                  },
                  "context_window": 32000,
                  "max_output_tokens": 4096
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)
        reasoning = registry.get("typed", "minimal-reasoning").capabilities.reasoning

        assert reasoning.supported is True
        assert reasoning.control is None

    def test_loads_family_field(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("typed.json").write_text(
            """
            {
              "provider_id": "typed",
              "models": {
                "with-family": {
                  "name": "With Family",
                  "family": "gpt-5.2",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": false}
                  },
                  "context_window": 32000,
                  "max_output_tokens": 4096
                },
                "without-family": {
                  "name": "Without Family",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": false}
                  },
                  "context_window": 32000,
                  "max_output_tokens": 4096
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.get("typed", "with-family").family == "gpt-5.2"
        assert registry.get("typed", "without-family").family == ""


# ---------------------------------------------------------------------------
# Model.connections parsing
# ---------------------------------------------------------------------------


class TestModelConnectionsParsing:
    def test_connections_defaults_to_empty_tuple_when_field_missing(self):
        """A model entry without a ``connections`` key in the catalog loads
        with ``connections == ()`` — valid for every connection of the
        provider."""

        capabilities = Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        )
        model = Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=capabilities,
            context_window=128000,
            max_output_tokens=16000,
        )

        assert model.connections == ()

    def test_registry_loads_connections_allowlist_from_json(self, tmp_path: Path):
        """A catalog entry that declares ``connections`` is loaded with the
        tuple preserved exactly — this is the field that downstream
        target-expansion and ``model.list`` consume."""

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("openai.json").write_text(
            """
            {
              "provider_id": "openai",
              "models": {
                "gpt-5.2": {
                  "name": "GPT-5.2",
                  "capabilities": {
                    "vision": true,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": true}
                  },
                  "context_window": 128000,
                  "max_output_tokens": 16000,
                  "connections": ["api-key"]
                },
                "gpt-5.5": {
                  "name": "GPT-5.5",
                  "capabilities": {
                    "vision": true,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": true}
                  },
                  "context_window": 256000,
                  "max_output_tokens": 32000,
                  "connections": ["subscription"]
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.get("openai", "gpt-5.2").connections == ("api-key",)
        assert registry.get("openai", "gpt-5.5").connections == ("subscription",)

    def test_registry_loads_empty_connections_for_models_without_field(self, tmp_path: Path):
        """A model entry that omits the ``connections`` key loads with an
        empty tuple, preserving the "valid for every connection" semantic."""

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("openai.json").write_text(
            """
            {
              "provider_id": "openai",
              "models": {
                "gpt-5.2": {
                  "name": "GPT-5.2",
                  "capabilities": {
                    "vision": true,
                    "tools": true,
                    "json_mode": true,
                    "reasoning": {"supported": true}
                  },
                  "context_window": 128000,
                  "max_output_tokens": 16000
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.get("openai", "gpt-5.2").connections == ()


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

    def test_every_committed_catalog_file_loads(self):
        """Every ``resources/models/<provider>.json`` loads under the new typed
        shape without error — the binding requirement for Phase 1's all-seeds
        conversion. ``*.raw.json`` and ``*.overrides.json`` are skipped by the
        loader, so the registry must end up non-empty across all real catalogs.
        """

        registry = ModelRegistry.load(RESOURCES_DIR)

        # Sanity: at least the hand-maintained anthropic seed and the
        # refresh-backed openrouter catalog are present, and every loaded
        # model's reasoning carries a boolean ``supported`` flag.
        assert registry.list_for_provider("anthropic")
        assert registry.list_for_provider("opencode-go")
        for _, model in registry._models.items():
            assert isinstance(model.capabilities.reasoning.supported, bool)
            assert isinstance(model.capabilities.reasoning.levels, tuple)
            assert model.capabilities.reasoning.control in (None, "levels", "on_off", "budget")
            assert isinstance(model.family, str)

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
