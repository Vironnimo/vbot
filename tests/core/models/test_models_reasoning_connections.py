"""Tests for models reasoning connections."""




from pathlib import Path

import pytest

from core.models.models import (
    Capabilities,
    Model,
    ModelRegistry,
    ReasoningCapabilities,
)
from tests.core.models.models_test_support import (
    FIXTURES_DIR,
)
from tests.core.models.models_test_support import (
    _clear_registry_cache as _clear_registry_cache,
)


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
