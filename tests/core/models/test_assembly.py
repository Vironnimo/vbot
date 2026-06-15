"""Tests for the at-load 3-layer assembly, the canonical join, and the merge.

Two layers of coverage:

* Unit tests over the pure functions in ``core.models.assembly``
  (:func:`resolve_canonical_id`, :func:`merge_layers`, :func:`load_canonical_layer`,
  :func:`assemble_provider_model`).
* End-to-end tests over ``ModelRegistry.load()`` against the worked-example
  fixtures under ``fixtures/assembly/`` — this is the acceptance gate for the
  handoff's ``deepseek-v4-pro`` example (two providers, two effective ladders).
"""

from pathlib import Path

import pytest

from core.models.assembly import (
    assemble_provider_model,
    load_canonical_layer,
    merge_layers,
    resolve_canonical_id,
)
from core.models.models import ModelRegistry

ASSEMBLY_FIXTURES = Path(__file__).parent / "fixtures" / "assembly"


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    ModelRegistry._cache.clear()
    yield
    ModelRegistry._cache.clear()


# ---------------------------------------------------------------------------
# resolve_canonical_id — the deterministic join (no fuzzy)
# ---------------------------------------------------------------------------


class TestResolveCanonicalId:
    def test_explicit_override_pointer_wins(self):
        """A manual pointer in the override layer beats the auto pointer in the
        provider layer."""

        result = resolve_canonical_id(
            "wire-id",
            {"canonical": "lab/auto"},
            {"canonical": "lab/manual"},
            {"lab/auto": {}, "lab/manual": {}},
        )
        assert result == "lab/manual"

    def test_provider_auto_pointer_used_when_no_override_pointer(self):
        result = resolve_canonical_id(
            "wire-id",
            {"canonical": "lab/auto"},
            None,
            {"lab/auto": {}},
        )
        assert result == "lab/auto"

    def test_exact_canonical_id_match_when_no_pointer(self):
        """A wire-id that is itself a canonical-layer key auto-joins by exact
        match (OpenRouter/Mistral-style ``lab/model`` wire-ids)."""

        result = resolve_canonical_id(
            "deepseek/deepseek-v4-pro",
            {},
            None,
            {"deepseek/deepseek-v4-pro": {}},
        )
        assert result == "deepseek/deepseek-v4-pro"

    def test_no_join_when_no_pointer_and_no_exact_match(self):
        """A missed join is not an error — it returns None."""

        result = resolve_canonical_id("opaque-wire-id", {}, None, {"other/model": {}})
        assert result is None

    def test_pointer_at_a_missing_target_is_still_returned(self):
        """The pointer is honored verbatim; a dead target is caught by the
        validator, not silently dropped here."""

        result = resolve_canonical_id("wire-id", {"canonical": "lab/dead"}, None, {})
        assert result == "lab/dead"

    def test_empty_string_pointer_falls_through_to_exact_match(self):
        result = resolve_canonical_id(
            "lab/model",
            {"canonical": ""},
            None,
            {"lab/model": {}},
        )
        assert result == "lab/model"


# ---------------------------------------------------------------------------
# merge_layers — field-level, highest wins, capabilities one level deep
# ---------------------------------------------------------------------------


class TestMergeLayers:
    def test_highest_layer_wins_per_top_level_field(self):
        merged = merge_layers(
            [
                {"name": "canonical", "context_window": 1000, "family": "base"},
                {"name": "provider", "context_window": 2000},
                {"name": "override"},
            ]
        )
        assert merged["name"] == "override"
        assert merged["context_window"] == 2000
        assert merged["family"] == "base"

    def test_capabilities_merged_one_level_deep(self):
        """Each capability sub-field is taken from its highest definer; sub-fields
        a higher layer omits are inherited from below."""

        merged = merge_layers(
            [
                {"capabilities": {"vision": False, "tools": True, "json_mode": True}},
                {"capabilities": {"tools": False}},
            ]
        )
        assert merged["capabilities"] == {
            "vision": False,
            "tools": False,
            "json_mode": True,
        }

    def test_reasoning_object_replaced_wholesale_not_deep_merged(self):
        """A nested ``reasoning`` object is replaced wholesale by the higher
        layer — never key-by-key deep-merged."""

        merged = merge_layers(
            [
                {
                    "capabilities": {
                        "reasoning": {
                            "supported": True,
                            "control": "levels",
                            "levels": ["high", "max"],
                        }
                    }
                },
                {"capabilities": {"reasoning": {"supported": True, "control": "on_off"}}},
            ]
        )
        # ``levels`` from the lower layer does NOT survive — wholesale replace.
        assert merged["capabilities"]["reasoning"] == {
            "supported": True,
            "control": "on_off",
        }

    def test_modality_list_replaced_wholesale(self):
        merged = merge_layers(
            [
                {"capabilities": {"input_modalities": ["text", "image"]}},
                {"capabilities": {"input_modalities": ["text"]}},
            ]
        )
        assert merged["capabilities"]["input_modalities"] == ["text"]

    def test_inputs_are_not_mutated(self):
        low = {"capabilities": {"vision": False}}
        high = {"capabilities": {"vision": True}}
        merge_layers([low, high])
        assert low == {"capabilities": {"vision": False}}
        assert high == {"capabilities": {"vision": True}}


# ---------------------------------------------------------------------------
# load_canonical_layer — defensive, base + canonical overrides
# ---------------------------------------------------------------------------


class TestLoadCanonicalLayer:
    def test_absent_canonical_file_yields_empty_layer(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        assert load_canonical_layer(models_dir) == {}

    def test_loads_base_records(self):
        layer = load_canonical_layer(ASSEMBLY_FIXTURES / "models")
        assert "deepseek/deepseek-v4-pro" in layer
        assert layer["deepseek/deepseek-v4-pro"]["family"] == "deepseek-v4"

    def test_canonical_overrides_win_field_level(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        (models_dir / "models.json").write_text(
            '{"models": {"lab/x": {"name": "Base", "family": "base"}}}',
            encoding="utf-8",
        )
        (models_dir / "models.overrides.json").write_text(
            '{"models": {"lab/x": {"name": "Corrected"}}}',
            encoding="utf-8",
        )

        layer = load_canonical_layer(models_dir)

        assert layer["lab/x"]["name"] == "Corrected"
        assert layer["lab/x"]["family"] == "base"

    def test_canonical_override_can_add_a_new_record(self, tmp_path: Path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        (models_dir / "models.json").write_text(
            '{"models": {"lab/x": {"name": "X"}}}', encoding="utf-8"
        )
        (models_dir / "models.overrides.json").write_text(
            '{"models": {"lab/y": {"name": "Y"}}}', encoding="utf-8"
        )

        layer = load_canonical_layer(models_dir)

        assert set(layer) == {"lab/x", "lab/y"}


# ---------------------------------------------------------------------------
# assemble_provider_model — strips the join key, applies the merge
# ---------------------------------------------------------------------------


class TestAssembleProviderModel:
    def test_canonical_pointer_is_stripped_from_result(self):
        record = assemble_provider_model(
            "deepseek-v4-pro",
            {"name": "P", "canonical": "deepseek/deepseek-v4-pro"},
            None,
            {"deepseek/deepseek-v4-pro": {"name": "Canon", "family": "deepseek-v4"}},
        )
        assert "canonical" not in record
        assert record["family"] == "deepseek-v4"

    def test_no_join_runs_on_provider_data_only(self):
        record = assemble_provider_model(
            "opaque",
            {"name": "Provider Only", "context_window": 8000},
            None,
            {},
        )
        assert record == {"name": "Provider Only", "context_window": 8000}


# ---------------------------------------------------------------------------
# Worked example — ModelRegistry.load() over the fixtures (acceptance gate)
# ---------------------------------------------------------------------------


class TestWorkedExampleDeepseekV4Pro:
    """The handoff's ``deepseek-v4-pro`` worked example, end-to-end.

    Canonical ladder ``[high, max]``. OpenRouter deviates to ``[high, xhigh]``
    (joins by exact canonical-id match). opencode-go omits reasoning and carries
    a ``canonical`` pointer, so it inherits ``[high, max]``.
    """

    def test_openrouter_deviating_ladder_is_effective(self):
        registry = ModelRegistry.load(ASSEMBLY_FIXTURES)
        model = registry.get("openrouter", "deepseek/deepseek-v4-pro")

        assert model.capabilities.reasoning.levels == ("high", "xhigh")
        # Provider name wins over canonical; family is inherited from canonical.
        assert model.name == "DeepSeek V4 Pro (OpenRouter)"
        assert model.family == "deepseek-v4"

    def test_opencode_go_inherits_canonical_ladder(self):
        registry = ModelRegistry.load(ASSEMBLY_FIXTURES)
        model = registry.get("opencode-go", "deepseek-v4-pro")

        assert model.capabilities.reasoning.levels == ("high", "max")
        assert model.capabilities.reasoning.control == "levels"
        # The wire-id stays the provider wire-id; the canonical id never lands
        # on the model.
        assert model.model_id == "deepseek-v4-pro"
        assert model.family == "deepseek-v4"

    def test_both_effective_ladders_asserted_together(self):
        """The two providers, side by side — the explicit acceptance assertion."""

        registry = ModelRegistry.load(ASSEMBLY_FIXTURES)

        openrouter = registry.get("openrouter", "deepseek/deepseek-v4-pro")
        opencode_go = registry.get("opencode-go", "deepseek-v4-pro")

        assert openrouter.capabilities.reasoning.levels == ("high", "xhigh")
        assert opencode_go.capabilities.reasoning.levels == ("high", "max")


class TestAssemblyRegressionCases:
    """Provider-only, override-wins, and wholesale-nested-replace, end-to-end."""

    def test_provider_only_model_with_no_canonical_loads(self):
        """A model whose wire-id joins nothing loads on provider data alone — a
        missed join is not an error."""

        registry = ModelRegistry.load(ASSEMBLY_FIXTURES)
        model = registry.get("openrouter", "vendor-x/standalone-model")

        assert model.name == "Standalone Model"
        assert model.family == ""
        assert model.capabilities.reasoning.supported is False
        assert model.context_window == 64000

    def test_override_wins_and_omits_provider_id(self):
        """The mistral override omits ``provider_id`` (derived from filename),
        carries a manual ``canonical`` pointer, and its fields win."""

        registry = ModelRegistry.load(ASSEMBLY_FIXTURES)
        model = registry.get("mistral", "thin-deepseek")

        assert model.name == "Thin DeepSeek (hand-corrected)"
        # Manual pointer forced the join → canonical family inherited.
        assert model.family == "deepseek-v4"

    def test_override_reasoning_replaces_provider_and_canonical_wholesale(self):
        """The override's ``reasoning`` ladder wins wholesale over both the
        provider ladder ``[low, medium]`` and the canonical ladder ``[high, max]``."""

        registry = ModelRegistry.load(ASSEMBLY_FIXTURES)
        model = registry.get("mistral", "thin-deepseek")

        assert model.capabilities.reasoning.levels == ("medium", "high", "max")
        # Provider-layer facts the override doesn't touch survive.
        assert model.context_window == 128000
        assert model.max_output_tokens == 16000
        assert model.capabilities.tools is True


# ---------------------------------------------------------------------------
# Cache / invalidation — canonical data participates in refresh-then-reload
# ---------------------------------------------------------------------------


class TestCanonicalCacheInvalidation:
    def test_canonical_edit_picked_up_after_invalidate_and_reload(self, tmp_path: Path):
        """A ``model.refresh_db``-style reload (invalidate then load) picks up new
        canonical data, since the canonical files live under the same
        ``resources_dir`` the registry caches by."""

        models_dir = tmp_path / "models"
        models_dir.mkdir()
        (models_dir / "openrouter.json").write_text(
            """
            {
              "provider_id": "openrouter",
              "models": {
                "lab/model": {
                  "name": "Wire Name",
                  "capabilities": {
                    "vision": false, "tools": true, "json_mode": true,
                    "input_modalities": ["text"], "output_modalities": ["text"]
                  },
                  "context_window": 100000,
                  "max_output_tokens": 8000
                }
              }
            }
            """,
            encoding="utf-8",
        )
        # No canonical file yet → the provider model is missing ``reasoning``.
        # Assembly must still place it via... actually it needs reasoning; so the
        # first load supplies reasoning through the canonical file below. Write
        # the canonical file before the first load.
        (models_dir / "models.json").write_text(
            """
            {
              "models": {
                "lab/model": {
                  "capabilities": {
                    "reasoning": {"supported": true, "control": "levels", "levels": ["low"]}
                  }
                }
              }
            }
            """,
            encoding="utf-8",
        )

        first = ModelRegistry.load(tmp_path)
        assert first.get("openrouter", "lab/model").capabilities.reasoning.levels == ("low",)

        # Refresh rewrites the canonical ladder; without invalidation the cached
        # registry still serves the old data.
        (models_dir / "models.json").write_text(
            """
            {
              "models": {
                "lab/model": {
                  "capabilities": {
                    "reasoning": {"supported": true, "control": "levels", "levels": ["low", "high"]}
                  }
                }
              }
            }
            """,
            encoding="utf-8",
        )
        assert ModelRegistry.load(tmp_path) is first

        ModelRegistry.invalidate(tmp_path)
        second = ModelRegistry.load(tmp_path)

        assert second is not first
        assert second.get("openrouter", "lab/model").capabilities.reasoning.levels == (
            "low",
            "high",
        )
