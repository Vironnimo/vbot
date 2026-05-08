"""Tests for Model dataclass and ModelRegistry."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities

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
        )
        assert caps.vision is True
        assert caps.tools is False
        assert caps.json_mode is True
        assert caps.reasoning is reasoning

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
    """Verify the shipped JSON data files load correctly."""

    @pytest.fixture(autouse=True)
    def _reset_cache_for_real_resources(self):
        """Extra cache clear to ensure isolation from fixture-based tests."""
        ModelRegistry._cache.clear()
        yield
        ModelRegistry._cache.clear()

    def test_load_openai(self):
        registry = ModelRegistry.load(RESOURCES_DIR)

        model = registry.get("openai", "gpt-5.2")
        assert model.name == "GPT-5.2"
        assert model.context_window == 128000
        assert model.max_output_tokens == 16384

    def test_load_openrouter(self):
        registry = ModelRegistry.load(RESOURCES_DIR)

        haiku = registry.get("openrouter", "anthropic/claude-haiku-4.5")
        assert haiku.name == "Claude Haiku 4.5"
        assert haiku.context_window == 200000
        assert haiku.max_output_tokens == 64000
        assert haiku.capabilities.vision is True
        assert haiku.capabilities.tools is True
        assert haiku.capabilities.json_mode is True
        assert haiku.capabilities.reasoning.supported is True

        claude = registry.get("openrouter", "anthropic/claude-sonnet-4")
        assert claude.name == "Claude Sonnet 4"
        assert claude.context_window == 128000
        assert claude.max_output_tokens == 64000

        gpt = registry.get("openrouter", "openai/gpt-5.2")
        assert gpt.name == "GPT-5.2"
        assert gpt.max_output_tokens == 16384

    def test_load_anthropic(self):
        registry = ModelRegistry.load(RESOURCES_DIR)

        sonnet = registry.get("anthropic", "claude-sonnet-4-20250219")
        assert sonnet.name == "Claude Sonnet 4"
        assert sonnet.context_window == 200000
        assert sonnet.max_output_tokens == 64000

        opus = registry.get("anthropic", "claude-opus-4-20250219")
        assert opus.name == "Claude Opus 4"
        assert opus.context_window == 200000
        assert opus.max_output_tokens == 32000

    def test_list_all_providers(self):
        registry = ModelRegistry.load(RESOURCES_DIR)

        openai_models = registry.list_for_provider("openai")
        assert len(openai_models) == 1

        openrouter_models = registry.list_for_provider("openrouter")
        assert len(openrouter_models) == 3

        anthropic_models = registry.list_for_provider("anthropic")
        assert len(anthropic_models) == 2
