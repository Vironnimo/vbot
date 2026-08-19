"""Tests for Model dataclass and ModelRegistry."""

import json
import logging
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
from core.providers.reasoning import resolve_reasoning_intent

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
RESOURCES_DIR = PROJECT_ROOT / "resources"


def _model_record(name: str) -> dict[str, object]:
    return {
        "name": name,
        "capabilities": {
            "vision": False,
            "tools": True,
            "json_mode": False,
            "reasoning": {"supported": False},
        },
        "context_window": 32000,
        "max_output_tokens": 4096,
    }


def _write_provider_catalog(
    models_dir: Path,
    provider_id: str,
    models: dict[str, object],
) -> Path:
    path = models_dir / f"{provider_id}.json"
    path.write_text(
        json.dumps({"provider_id": provider_id, "models": models}),
        encoding="utf-8",
    )
    return path


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

    def test_context_window_is_optional(self):
        # A missing context window stays missing in the data rather than being
        # faked with a constant; the read-side default chain fills it at use time.
        model = Model(
            model_id="custom-model",
            name="Custom Model",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=None,
            max_output_tokens=None,
        )

        assert model.context_window is None

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

    def test_corrupt_provider_file_does_not_block_valid_providers(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _write_provider_catalog(models_dir, "healthy", {"model-a": _model_record("Healthy")})
        corrupt_path = models_dir / "corrupt.json"
        corrupt_path.write_text('{"provider_id": "corrupt", "models":', encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="vbot.models"):
            registry = ModelRegistry.load(tmp_path)

        assert registry.get("healthy", "model-a").name == "Healthy"
        assert registry.list_for_provider("corrupt") == []
        assert str(corrupt_path) in caplog.text

    def test_structurally_invalid_provider_file_does_not_block_load(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _write_provider_catalog(models_dir, "healthy", {"model-a": _model_record("Healthy")})
        invalid_path = models_dir / "invalid.json"
        invalid_path.write_text(
            json.dumps({"provider_id": "invalid", "models": []}),
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="vbot.models"):
            registry = ModelRegistry.load(tmp_path)

        assert registry.get("healthy", "model-a").name == "Healthy"
        assert registry.list_for_provider("invalid") == []
        assert str(invalid_path) in caplog.text

    def test_invalid_model_entry_does_not_hide_valid_sibling(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        provider_path = _write_provider_catalog(
            models_dir,
            "mixed",
            {
                "healthy": _model_record("Healthy"),
                "broken": ["not", "an", "object"],
            },
        )

        with caplog.at_level(logging.WARNING, logger="vbot.models"):
            registry = ModelRegistry.load(tmp_path)

        assert registry.get("mixed", "healthy").name == "Healthy"
        assert registry.list_for_provider("mixed") == [registry.get("mixed", "healthy")]
        assert str(provider_path) in caplog.text
        assert "mixed/broken" in caplog.text

    def test_corrupt_override_is_ignored_without_hiding_generated_model(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _write_provider_catalog(models_dir, "healthy", {"model-a": _model_record("Generated")})
        override_path = models_dir / "healthy.overrides.json"
        override_path.write_text('{"models": {"model-a":', encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="vbot.models"):
            registry = ModelRegistry.load(tmp_path)

        assert registry.get("healthy", "model-a").name == "Generated"
        assert str(override_path) in caplog.text

    def test_corrupt_canonical_file_is_ignored_without_hiding_provider_model(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _write_provider_catalog(models_dir, "healthy", {"model-a": _model_record("Provider")})
        canonical_path = models_dir / "models.json"
        canonical_path.write_text('{"models":', encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="vbot.models"):
            registry = ModelRegistry.load(tmp_path)

        assert registry.get("healthy", "model-a").name == "Provider"
        assert str(canonical_path) in caplog.text

    def test_empty_provider_record_can_inherit_complete_canonical_model(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _write_provider_catalog(models_dir, "thin", {"model-a": {}})
        models_dir.joinpath("models.json").write_text(
            json.dumps({"models": {"model-a": _model_record("Canonical")}}),
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.get("thin", "model-a").name == "Canonical"

    def test_model_directory_scan_failure_does_not_raise(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()

        def fail_scan(*_args: object, **_kwargs: object) -> list[Path]:
            raise OSError("scan failed")

        monkeypatch.setattr(Path, "glob", fail_scan)

        with caplog.at_level(logging.WARNING, logger="vbot.models"):
            registry = ModelRegistry.load(tmp_path)

        assert registry.list_for_provider("unavailable") == []
        assert str(models_dir) in caplog.text

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

    def test_load_preserves_null_context_window(self, tmp_path: Path):
        # Arrange: a model whose catalog carries an explicit null context window
        # (an honestly missing fact, e.g. a thin/window-less endpoint).
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("test-provider.json").write_text(
            """
            {
              "provider_id": "test-provider",
              "models": {
                "window-less": {
                  "name": "Window-less Model",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": false,
                    "reasoning": {"supported": false}
                  },
                  "context_window": null,
                  "max_output_tokens": null
                }
              }
            }
            """,
            encoding="utf-8",
        )

        # Act
        registry = ModelRegistry.load(tmp_path)
        model = registry.get("test-provider", "window-less")

        # Assert: the gap stays a gap — not faked with a constant.
        assert model.context_window is None

    def test_load_preserves_absent_context_window(self, tmp_path: Path):
        # Arrange: a catalog entry that omits context_window entirely.
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("test-provider.json").write_text(
            """
            {
              "provider_id": "test-provider",
              "models": {
                "no-window": {
                  "name": "No Window Model",
                  "capabilities": {
                    "vision": false,
                    "tools": true,
                    "json_mode": false,
                    "reasoning": {"supported": false}
                  },
                  "max_output_tokens": null
                }
              }
            }
            """,
            encoding="utf-8",
        )

        # Act
        registry = ModelRegistry.load(tmp_path)
        model = registry.get("test-provider", "no-window")

        # Assert
        assert model.context_window is None

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

    def test_reload_swaps_contents_in_place_keeping_identity(self, tmp_path: Path):
        """``reload`` re-reads disk into the same instance so holders that captured
        the registry at construction see the new catalog without re-wiring."""

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        model_file = models_dir / "test_provider.json"

        def write_model(name: str) -> None:
            model_file.write_text(
                json.dumps(
                    {
                        "provider_id": "test_provider",
                        "models": {
                            "model-a": {
                                "name": name,
                                "capabilities": {
                                    "vision": False,
                                    "tools": False,
                                    "json_mode": False,
                                    "reasoning": {"supported": False},
                                },
                                "context_window": 1000,
                                "max_output_tokens": 100,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

        write_model("Original")
        registry = ModelRegistry.load(tmp_path)
        # A holder that captured the instance at construction.
        held_reference = registry

        write_model("Updated")
        registry.reload(tmp_path)

        # Same object, new contents: the captured reference sees the update.
        assert held_reference is registry
        assert held_reference.get("test_provider", "model-a").name == "Updated"
        # The cache is repointed at this same instance, not a fresh one.
        assert ModelRegistry.load(tmp_path) is registry

    def test_override_file_is_not_loaded_as_its_own_provider(self, tmp_path: Path):
        """``<provider>.overrides.json`` is a hand layer, not a provider file: it
        is excluded from the provider-file glob and applied during assembly. It
        does not spawn a phantom ``openrouter.overrides`` provider."""

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
                            "models": {
                                "model-a": {"name": "Corrected Model A"}
                            }
                        }
                        """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        # No phantom provider derived from the override filename.
        assert registry.list_for_provider("openrouter.overrides") == []

    def test_provider_override_applies_field_level_at_load(self, tmp_path: Path):
        """Overrides are merged at LOAD now (they used to only apply at refresh):
        the override's ``name`` wins, the provider's other fields survive."""

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
                    "vision": false, "tools": false, "json_mode": false,
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
              "models": {
                "model-a": {"name": "Corrected Model A"}
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)
        model = registry.get("openrouter", "model-a")

        assert model.name == "Corrected Model A"
        assert model.context_window == 1000

    def test_reasoning_replay_provider_and_model_overrides_load(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("provider.json").write_text(
            json.dumps(
                {
                    "provider_id": "provider",
                    "models": {
                        model_id: {
                            "name": model_id,
                            "capabilities": {
                                "vision": False,
                                "tools": False,
                                "json_mode": False,
                                "reasoning": {"supported": True},
                            },
                        }
                        for model_id in ("inherited", "overridden")
                    },
                }
            ),
            encoding="utf-8",
        )
        models_dir.joinpath("provider.overrides.json").write_text(
            json.dumps(
                {
                    "reasoning_replay": "current_run",
                    "models": {"overridden": {"reasoning_replay": "full_history"}},
                }
            ),
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.provider_reasoning_replay("provider") == "current_run"
        assert registry.get("provider", "inherited").reasoning_replay is None
        assert registry.get("provider", "overridden").reasoning_replay == "full_history"

    def test_reload_updates_reasoning_replay_overrides_in_place(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("provider.json").write_text(
            json.dumps(
                {
                    "provider_id": "provider",
                    "models": {
                        "model": {
                            "name": "Model",
                            "capabilities": {
                                "vision": False,
                                "tools": False,
                                "json_mode": False,
                                "reasoning": {"supported": True},
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        override_path = models_dir / "provider.overrides.json"
        override_path.write_text(
            json.dumps(
                {
                    "reasoning_replay": "current_run",
                    "models": {"model": {"reasoning_replay": "none"}},
                }
            ),
            encoding="utf-8",
        )
        registry = ModelRegistry.load(tmp_path)

        override_path.write_text(
            json.dumps(
                {
                    "reasoning_replay": "full_history",
                    "models": {"model": {"reasoning_replay": "current_run"}},
                }
            ),
            encoding="utf-8",
        )
        registry.reload(tmp_path)

        assert registry.provider_reasoning_replay("provider") == "full_history"
        assert registry.get("provider", "model").reasoning_replay == "current_run"

    def test_invalid_provider_reasoning_replay_rejects_override_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("provider.json").write_text(
            json.dumps(
                {
                    "provider_id": "provider",
                    "models": {
                        "model": {
                            "name": "Model",
                            "capabilities": {
                                "vision": False,
                                "tools": False,
                                "json_mode": False,
                                "reasoning": {"supported": True},
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        models_dir.joinpath("provider.overrides.json").write_text(
            json.dumps({"reasoning_replay": "conservative", "models": {}}),
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.provider_reasoning_replay("provider") is None
        assert registry.get("provider", "model").name == "Model"
        assert "reasoning_replay must be one of" in caplog.text

    def test_invalid_model_reasoning_replay_isolates_model(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("provider.json").write_text(
            json.dumps(
                {
                    "provider_id": "provider",
                    "models": {
                        model_id: {
                            "name": model_id,
                            "capabilities": {
                                "vision": False,
                                "tools": False,
                                "json_mode": False,
                                "reasoning": {"supported": True},
                            },
                        }
                        for model_id in ("healthy", "invalid")
                    },
                }
            ),
            encoding="utf-8",
        )
        models_dir.joinpath("provider.overrides.json").write_text(
            json.dumps(
                {
                    "models": {"invalid": {"reasoning_replay": "conservative"}},
                }
            ),
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.get("provider", "healthy").name == "healthy"
        with pytest.raises(KeyError):
            registry.get("provider", "invalid")
        assert "reasoning_replay must be one of" in caplog.text

    def test_override_only_model_loads_at_load(self, tmp_path: Path):
        """A wire-id present only in the override file (a manual override-only
        model with the full shape) is assembled and loaded."""

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
                    "vision": false, "tools": false, "json_mode": false,
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
              "models": {
                "override-only": {
                  "name": "Override Only",
                  "capabilities": {
                    "vision": false, "tools": false, "json_mode": false,
                    "reasoning": {"supported": false}
                  },
                  "context_window": 2000,
                  "max_output_tokens": 200
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.get("openrouter", "override-only").name == "Override Only"

    def test_override_only_provider_loads_without_generated_catalog(self, tmp_path: Path):
        """A hand-only provider needs no empty generated provider file."""

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("hand-only.overrides.json").write_text(
            """
            {
              "provider_id": "hand-only",
              "models": {
                "model-a": {
                  "name": "Hand Only Model",
                  "capabilities": {
                    "vision": false, "tools": true, "json_mode": false,
                    "reasoning": {"supported": true}
                  },
                  "context_window": 2000,
                  "max_output_tokens": 200
                }
              }
            }
            """,
            encoding="utf-8",
        )

        registry = ModelRegistry.load(tmp_path)

        assert registry.get("hand-only", "model-a").name == "Hand Only Model"

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


class TestModelAllowsConnection:
    """The single source of the per-model connection rule."""

    @staticmethod
    def _model(connections: tuple[str, ...]) -> Model:
        return Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=128000,
            max_output_tokens=16000,
            connections=connections,
        )

    def test_empty_allowlist_permits_every_connection(self):
        model = self._model(())
        assert model.allows_connection("api-key") is True
        assert model.allows_connection("subscription") is True

    def test_non_empty_allowlist_permits_listed_connection(self):
        assert self._model(("subscription",)).allows_connection("subscription") is True

    def test_non_empty_allowlist_rejects_unlisted_connection(self):
        assert self._model(("subscription",)).allows_connection("api-key") is False


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

    def test_real_resources_load_with_generated_canonical_layer(self):
        """Phase 3 generated the canonical ``models.json``; the assembly load
        path must still load a provider model whose wire-id does NOT join the
        canonical layer (opencode-go keys ``deepseek-v4-pro`` bare, while the
        canonical id is ``deepseek/deepseek-v4-pro`` — no auto join), on
        provider + override data alone."""

        assert (RESOURCES_DIR / "models" / "models.json").exists()

        registry = ModelRegistry.load(RESOURCES_DIR)

        # A provider-only model with no canonical join still loads fine.
        deepseek = registry.get("opencode-go", "deepseek-v4-pro")
        assert deepseek.capabilities.reasoning.supported is True

    def test_ollama_cloud_catalog_is_separate_and_entirely_remote(self):
        registry = ModelRegistry.load(RESOURCES_DIR)

        models = registry.list_for_provider("ollama-cloud")

        assert models
        assert registry.get("ollama-cloud", "gpt-oss:120b").model_id == "gpt-oss:120b"
        assert all(model.connections == ("api-key",) for model in models)
        assert all(model.metadata.get("ollama", {}).get("remote") is True for model in models)
        assert all(model.metadata.get("ollama", {}).get("local") is not True for model in models)

    def test_ollama_cloud_deepseek_output_override_is_effective(self):
        registry = ModelRegistry.load(RESOURCES_DIR)

        deepseek = registry.get("ollama-cloud", "deepseek-v4-flash:0731")

        assert deepseek.max_output_tokens == 65_536

    @pytest.mark.parametrize(
        ("provider_id", "model_id", "reasoning_field"),
        [
            ("ollama-cloud", "glm-5.2", "reasoning"),
            ("opencode-go", "glm-5.2", "reasoning_content"),
            ("opencode-go", "glm-5.3", "reasoning_content"),
        ],
    )
    def test_glm_targets_load_explicit_full_history_profiles(
        self,
        provider_id: str,
        model_id: str,
        reasoning_field: str,
    ) -> None:
        registry = ModelRegistry.load(RESOURCES_DIR)

        model = registry.get(provider_id, model_id)
        metadata = model.metadata[provider_id.replace("-", "_")]

        assert registry.provider_reasoning_replay(provider_id) == "full_history"
        assert model.reasoning_replay == "full_history"
        assert metadata["reasoning_response_field"] == reasoning_field
        if provider_id == "opencode-go":
            assert metadata["protocol"] == "openai"
        if (provider_id, model_id) == ("opencode-go", "glm-5.3"):
            assert "reasoning_request_format" not in metadata

    def test_overrides_are_applied_at_load(self):
        """``<provider>.overrides.json`` is now merged at LOAD (it used to only
        apply at refresh). The openai overrides add override-only task models —
        they must be present in the loaded registry."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        # ``tts-1`` lives only in ``openai.overrides.json`` (the provider file
        # carries the chat models). It loads because overrides apply at load.
        tts = registry.get("openai", "tts-1")
        assert tts.capabilities.task_types == ("text_to_speech", "audio_generation")

    def test_deepseek_flash_top_p_override_applies_at_load(self):
        """``ollama-cloud.overrides.json`` pins recommended_top_p 0.95 for both
        DeepSeek V4 Flash ids — the generated ``:0731`` entry and the
        override-only bare id."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        pinned = registry.get("ollama-cloud", "deepseek-v4-flash:0731")
        assert pinned.recommended_top_p == 0.95
        assert pinned.recommended_temperature == 1.0

        bare = registry.get("ollama-cloud", "deepseek-v4-flash")
        assert bare.recommended_top_p == 0.95
        assert bare.recommended_temperature == 1.0

    def test_deepseek_v4_pro_has_no_top_p_override(self):
        """DeepSeek V4 Pro has different official guidance — no top_p pin."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        pro = registry.get("ollama-cloud", "deepseek-v4-pro:preview")
        assert pro.recommended_top_p is None

    def test_openai_task_model_overrides_are_limited_to_working_connections(self):
        """OpenAI task models without a subscription wire are api-key only, while
        ``gpt-image-2`` stays valid for both connections."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        api_key_only_models = (
            "tts-1",
            "tts-1-hd",
            "gpt-4o-mini-tts",
            "whisper-1",
            "gpt-4o-transcribe",
            "gpt-4o-mini-transcribe",
            "dall-e-2",
            "dall-e-3",
            "gpt-image-1",
            "gpt-image-1-mini",
            "gpt-image-1.5",
        )
        for model_id in api_key_only_models:
            model = registry.get("openai", model_id)
            assert model.connections == ("api-key",)
            assert model.allows_connection("api-key") is True
            assert model.allows_connection("subscription") is False

        gpt_image_2 = registry.get("openai", "gpt-image-2")
        assert gpt_image_2.connections == ()
        assert gpt_image_2.allows_connection("api-key") is True
        assert gpt_image_2.allows_connection("subscription") is True

        for model_id in ("gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5", "gpt-image-2"):
            model = registry.get("openai", model_id)
            assert model.capabilities.input_modalities == ("image", "text")

    def test_anthropic_opus_4_5_override_pins_budget_control(self):
        """``anthropic.overrides.json`` pins Opus 4.5 to ``budget`` control.

        Opus 4.5 exposes an effort ladder (so the canonical layer labels it
        ``levels``) but does not support adaptive thinking — the ``levels`` render
        (``thinking: {type: adaptive}``) 400s there. The override forces native
        ``budget`` rendering, which the model accepts. With no ``budget_max`` to
        seed, a ``high`` effort derives the absolute fallback budget (16384)."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        opus45 = registry.get("anthropic", "claude-opus-4-5-20251101")
        assert opus45.capabilities.reasoning.supported is True
        assert opus45.capabilities.reasoning.control == "budget"
        assert opus45.capabilities.reasoning.budget_max is None

        intent = resolve_reasoning_intent(
            supported=opus45.capabilities.reasoning.supported,
            control=opus45.capabilities.reasoning.control,
            levels=opus45.capabilities.reasoning.levels,
            effort="high",
            budget_max=opus45.capabilities.reasoning.budget_max,
        )
        assert intent.kind == "budget"
        assert intent.budget_tokens == 16384

    @pytest.mark.parametrize(
        "provider_id",
        ["openai", "openrouter", "anthropic", "github-copilot", "mistral", "ollama-cloud"],
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
            if model.context_window is not None:
                assert isinstance(model.context_window, int)
                assert model.context_window >= 0
            if model.max_output_tokens is not None:
                assert isinstance(model.max_output_tokens, int)
                assert model.max_output_tokens >= 0


class TestCustomModelOverlay:
    @staticmethod
    def _custom_provider(model_name: str) -> dict[str, dict[str, object]]:
        return {
            model_name: {
                "models": {
                    "chat-model": {
                        "name": model_name,
                        "capabilities": {
                            "vision": False,
                            "tools": True,
                            "json_mode": False,
                            "reasoning": False,
                            "input_modalities": ["text"],
                            "output_modalities": ["text"],
                            "supported_parameters": [],
                            "supported_voices": [],
                            "task_types": ["chat"],
                            "task_options": {},
                        },
                    }
                }
            }
        }

    def test_custom_provider_registries_do_not_share_the_path_cache(self, tmp_path: Path) -> None:
        first = ModelRegistry.load(
            tmp_path,
            custom_providers=self._custom_provider("first"),
        )
        second = ModelRegistry.load(
            tmp_path,
            custom_providers=self._custom_provider("second"),
        )
        bundled_only = ModelRegistry.load(tmp_path)

        assert first is not second
        assert first.get("first", "chat-model").name == "first"
        assert second.get("second", "chat-model").name == "second"
        assert bundled_only.list_for_provider("first") == []
        assert bundled_only.list_for_provider("second") == []

    def test_manual_custom_model_overlays_discovered_record(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("local-ai.json").write_text(
            json.dumps(
                {
                    "provider_id": "local-ai",
                    "models": {
                        "chat-model": {
                            "name": "Discovered",
                            "capabilities": {
                                "vision": False,
                                "tools": False,
                                "json_mode": False,
                                "reasoning": {"supported": False},
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        custom = {
            "local-ai": {
                "models": {
                    "chat-model": {
                        "name": "Manual",
                        "context_window": 65_536,
                        "max_output_tokens": 2_048,
                        "capabilities": {
                            "vision": True,
                            "tools": True,
                            "json_mode": True,
                            "reasoning": True,
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                            "supported_parameters": [],
                            "supported_voices": [],
                            "task_types": ["chat", "image_understanding"],
                            "task_options": {},
                        },
                    }
                }
            }
        }

        registry = ModelRegistry.load(tmp_path, custom_providers=custom)
        held_reference = registry
        model = registry.get("local-ai", "chat-model")

        assert model.name == "Manual"
        assert model.context_window == 65_536
        assert model.capabilities.reasoning.supported is True
        assert model.connections == ("default",)

        custom["local-ai"]["models"]["chat-model"]["name"] = "Updated"
        registry.reload(tmp_path, custom_providers=custom)

        assert held_reference is registry
        assert held_reference.get("local-ai", "chat-model").name == "Updated"
