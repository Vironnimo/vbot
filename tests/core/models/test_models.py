"""Tests for models."""

import json
import logging
from pathlib import Path

import pytest

from core.models.models import (
    ModelRegistry,
)
from tests.core.models.models_test_support import (
    FIXTURES_DIR,
)
from tests.core.models.models_test_support import (
    _clear_registry_cache as _clear_registry_cache,
)


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
