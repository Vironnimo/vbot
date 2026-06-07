"""Tests for the core model query (``core.models.query.ModelQuery``)."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from core.models import (
    Capabilities,
    Model,
    ModelQuery,
    ModelRegistry,
    ReasoningCapabilities,
)
from core.models.query import (
    _BOOLEAN_MODEL_CAPABILITIES,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Clear the registry cache before and after each test for independence."""
    ModelRegistry._cache.clear()
    yield
    ModelRegistry._cache.clear()


def _make_capabilities(
    *,
    vision: bool = False,
    tools: bool = False,
    json_mode: bool = False,
    reasoning: bool = False,
    input_modalities: tuple[str, ...] = (),
    output_modalities: tuple[str, ...] = (),
    task_types: tuple[str, ...] = (),
) -> Capabilities:
    return Capabilities(
        vision=vision,
        tools=tools,
        json_mode=json_mode,
        reasoning=ReasoningCapabilities(supported=reasoning),
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        task_types=task_types,
    )


def _make_model(
    model_id: str,
    *,
    context_window: int = 32000,
    max_output_tokens: int | None = 4096,
    **capability_kwargs: Any,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=_make_capabilities(**capability_kwargs),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )


# ---------------------------------------------------------------------------
# ModelQuery — value normalization
# ---------------------------------------------------------------------------


class TestFromFiltersNormalization:
    def test_empty_params_yields_empty_query(self):
        query = ModelQuery.from_filters({})
        assert query == ModelQuery()

    def test_unknown_keys_are_ignored(self):
        query = ModelQuery.from_filters({"unknown_field": "x", "another": 42})
        assert query == ModelQuery()

    def test_capability_aliases_collapse(self):
        query = ModelQuery.from_filters(
            {"capability": "tools", "capabilities": ["vision", "tools"]}
        )
        assert query.capabilities == ("tools", "vision")

    def test_task_aliases_collapse(self):
        query = ModelQuery.from_filters(
            {
                "task": "image_generation",
                "tasks": ["text_to_speech"],
                "task_type": "image_generation",
                "task_types": ["speech_to_text"],
            }
        )
        assert query.tasks == ("image_generation", "text_to_speech", "speech_to_text")

    def test_input_modality_aliases_collapse(self):
        query = ModelQuery.from_filters(
            {"input_modality": "image", "input_modalities": ["audio", "image"]}
        )
        assert query.input_modalities == ("image", "audio")

    def test_output_modality_aliases_collapse(self):
        query = ModelQuery.from_filters(
            {"output_modality": "speech", "output_modalities": ["text", "speech"]}
        )
        assert query.output_modalities == ("speech", "text")

    def test_list_or_string_values_both_accepted(self):
        string_query = ModelQuery.from_filters({"tasks": "chat"})
        list_query = ModelQuery.from_filters({"tasks": ["chat"]})
        assert string_query.tasks == ("chat",)
        assert list_query.tasks == ("chat",)
        assert string_query == list_query

    def test_values_are_lowercased_and_trimmed(self):
        query = ModelQuery.from_filters({"tasks": ["  IMAGE_GENERATION  ", "chat", "CHAT"]})
        assert query.tasks == ("image_generation", "chat")

    def test_empty_and_whitespace_values_are_dropped(self):
        query = ModelQuery.from_filters({"tasks": ["", "  ", "chat", "\t"]})
        assert query.tasks == ("chat",)

    def test_duplicates_are_removed(self):
        query = ModelQuery.from_filters(
            {
                "capabilities": ["tools", "vision", "tools", "vision"],
                "tasks": ["chat", "chat", "image_generation"],
            }
        )
        assert query.capabilities == ("tools", "vision")
        assert query.tasks == ("chat", "image_generation")

    def test_provider_id_is_lowercased_and_trimmed(self):
        query = ModelQuery.from_filters({"provider_id": "  OpenAI  "})
        assert query.provider_id == "openai"

    def test_empty_provider_id_becomes_none(self):
        query = ModelQuery.from_filters({"provider_id": "   "})
        assert query.provider_id is None

    def test_missing_provider_id_becomes_none(self):
        query = ModelQuery.from_filters({})
        assert query.provider_id is None

    def test_min_context_window_zero_becomes_none(self):
        query = ModelQuery.from_filters({"min_context_window": 0})
        assert query.min_context_window is None

    def test_min_context_window_missing_becomes_none(self):
        query = ModelQuery.from_filters({})
        assert query.min_context_window is None

    def test_min_context_window_positive_is_kept(self):
        query = ModelQuery.from_filters({"min_context_window": 128000})
        assert query.min_context_window == 128000


class TestFromFiltersValidation:
    def test_non_string_provider_id_raises(self):
        with pytest.raises(ValueError, match="provider_id must be a string"):
            ModelQuery.from_filters({"provider_id": 42})

    def test_capability_list_with_non_string_raises(self):
        with pytest.raises(ValueError, match="capability must be a string or list of strings"):
            ModelQuery.from_filters({"capability": [1, 2, 3]})

    def test_capability_int_raises(self):
        with pytest.raises(ValueError, match="capability must be a string or list of strings"):
            ModelQuery.from_filters({"capability": 7})

    def test_task_list_with_non_string_raises(self):
        with pytest.raises(ValueError, match="task must be a string or list of strings"):
            ModelQuery.from_filters({"task": [None]})

    def test_input_modality_list_with_non_string_raises(self):
        with pytest.raises(ValueError, match="input_modality must be a string or list of strings"):
            ModelQuery.from_filters({"input_modality": ["text", 99]})

    def test_output_modality_list_with_non_string_raises(self):
        with pytest.raises(ValueError, match="output_modality must be a string or list of strings"):
            ModelQuery.from_filters({"output_modality": ["text", {}]})

    def test_min_context_window_negative_raises(self):
        with pytest.raises(ValueError, match="min_context_window must be a non-negative integer"):
            ModelQuery.from_filters({"min_context_window": -1})

    def test_min_context_window_bool_raises(self):
        with pytest.raises(ValueError, match="min_context_window must be a non-negative integer"):
            ModelQuery.from_filters({"min_context_window": True})

    def test_min_context_window_string_raises(self):
        with pytest.raises(ValueError, match="min_context_window must be a non-negative integer"):
            ModelQuery.from_filters({"min_context_window": "1000"})


# ---------------------------------------------------------------------------
# ModelQuery — frozen and shape
# ---------------------------------------------------------------------------


class TestModelQueryShape:
    def test_default_construction_is_empty(self):
        query = ModelQuery()
        assert query.provider_id is None
        assert query.tasks == ()
        assert query.capabilities == ()
        assert query.input_modalities == ()
        assert query.output_modalities == ()
        assert query.min_context_window is None

    def test_is_frozen(self):
        query = ModelQuery(provider_id="openai", tasks=("chat",))
        with pytest.raises(FrozenInstanceError):
            query.provider_id = "anthropic"  # type: ignore[misc]

    def test_equality(self):
        a = ModelQuery(provider_id="openai", tasks=("chat",))
        b = ModelQuery(provider_id="openai", tasks=("chat",))
        assert a == b

    def test_boolean_capabilities_constant_matches_legacy(self):
        assert (
            frozenset(("vision", "tools", "json_mode", "reasoning")) == _BOOLEAN_MODEL_CAPABILITIES
        )


# ---------------------------------------------------------------------------
# ModelQuery.matches — task filter
# ---------------------------------------------------------------------------


class TestMatchesTaskFilter:
    def test_task_match(self):
        query = ModelQuery(tasks=("image_generation",))
        model = _make_model(
            "img",
            task_types=("chat", "text_output", "image_generation"),
        )
        assert query.matches(model) is True

    def test_task_miss_excludes_model(self):
        query = ModelQuery(tasks=("image_generation",))
        model = _make_model("chatty", task_types=("chat", "text_output"))
        assert query.matches(model) is False

    def test_multiple_tasks_all_required(self):
        query = ModelQuery(tasks=("chat", "image_understanding"))
        full = _make_model("full", task_types=("chat", "text_output", "image_understanding"))
        partial = _make_model("partial", task_types=("chat", "text_output"))
        assert query.matches(full) is True
        assert query.matches(partial) is False

    def test_empty_task_filter_matches_everything(self):
        query = ModelQuery(tasks=())
        any_model = _make_model("any", task_types=())
        assert query.matches(any_model) is True


# ---------------------------------------------------------------------------
# ModelQuery.matches — boolean capabilities
# ---------------------------------------------------------------------------


class TestMatchesBooleanCapabilities:
    def test_vision_capability_passes_when_true(self):
        query = ModelQuery(capabilities=("vision",))
        model = _make_model("vision-yes", vision=True)
        assert query.matches(model) is True

    def test_vision_capability_fails_when_false(self):
        query = ModelQuery(capabilities=("vision",))
        model = _make_model("vision-no", vision=False)
        assert query.matches(model) is False

    def test_tools_capability(self):
        query = ModelQuery(capabilities=("tools",))
        assert query.matches(_make_model("with-tools", tools=True)) is True
        assert query.matches(_make_model("no-tools", tools=False)) is False

    def test_json_mode_capability(self):
        query = ModelQuery(capabilities=("json_mode",))
        assert query.matches(_make_model("with-json", json_mode=True)) is True
        assert query.matches(_make_model("no-json", json_mode=False)) is False

    def test_reasoning_capability_uses_nested_supported(self):
        query = ModelQuery(capabilities=("reasoning",))
        assert query.matches(_make_model("reasoning-yes", reasoning=True)) is True
        assert query.matches(_make_model("reasoning-no", reasoning=False)) is False

    def test_non_boolean_capability_falls_back_to_task_types(self):
        """A capability name that is not a boolean cap is treated as a task_type."""
        query = ModelQuery(capabilities=("image_generation",))
        model = _make_model("img", task_types=("image_generation",))
        assert query.matches(model) is True

        not_model = _make_model("no-img", task_types=("chat",))
        assert query.matches(not_model) is False

    def test_multiple_capabilities_all_required(self):
        query = ModelQuery(capabilities=("vision", "tools"))
        full = _make_model("full", vision=True, tools=True)
        partial = _make_model("partial", vision=True, tools=False)
        assert query.matches(full) is True
        assert query.matches(partial) is False

    def test_capability_name_with_no_boolean_or_task_match_fails(self):
        query = ModelQuery(capabilities=("does_not_exist",))
        model = _make_model("m", task_types=("chat",))
        assert query.matches(model) is False


# ---------------------------------------------------------------------------
# ModelQuery.matches — modalities
# ---------------------------------------------------------------------------


class TestMatchesModalities:
    def test_input_modality_match(self):
        query = ModelQuery(input_modalities=("image",))
        model = _make_model("img", input_modalities=("text", "image"))
        assert query.matches(model) is True

    def test_input_modality_miss_excludes_model(self):
        query = ModelQuery(input_modalities=("image",))
        model = _make_model("text-only", input_modalities=("text",))
        assert query.matches(model) is False

    def test_output_modality_match(self):
        query = ModelQuery(output_modalities=("speech",))
        model = _make_model("tts", output_modalities=("text", "speech"))
        assert query.matches(model) is True

    def test_output_modality_miss_excludes_model(self):
        query = ModelQuery(output_modalities=("speech",))
        model = _make_model("text-out", output_modalities=("text",))
        assert query.matches(model) is False

    def test_multiple_modalities_all_required(self):
        query = ModelQuery(input_modalities=("text", "image"))
        full = _make_model("full", input_modalities=("text", "image", "audio"))
        partial = _make_model("partial", input_modalities=("text",))
        assert query.matches(full) is True
        assert query.matches(partial) is False


# ---------------------------------------------------------------------------
# ModelQuery.matches — min_context_window
# ---------------------------------------------------------------------------


class TestMatchesMinContextWindow:
    def test_meets_minimum_passes(self):
        query = ModelQuery(min_context_window=1000)
        model = _make_model("big", context_window=1000)
        assert query.matches(model) is True

    def test_exceeds_minimum_passes(self):
        query = ModelQuery(min_context_window=1000)
        model = _make_model("bigger", context_window=128000)
        assert query.matches(model) is True

    def test_below_minimum_fails(self):
        query = ModelQuery(min_context_window=128000)
        model = _make_model("small", context_window=64000)
        assert query.matches(model) is False

    def test_none_minimum_does_not_filter(self):
        query = ModelQuery(min_context_window=None)
        model = _make_model("zero", context_window=0)
        assert query.matches(model) is True


# ---------------------------------------------------------------------------
# ModelQuery.matches — combined / AND semantics
# ---------------------------------------------------------------------------


class TestMatchesCombined:
    def test_all_filters_must_pass(self):
        query = ModelQuery(
            capabilities=("vision", "tools"),
            tasks=("chat",),
            input_modalities=("text", "image"),
            min_context_window=32000,
        )
        good = _make_model(
            "good",
            vision=True,
            tools=True,
            input_modalities=("text", "image"),
            context_window=64000,
            task_types=("chat", "image_understanding"),
        )
        assert query.matches(good) is True

    def test_any_filter_failing_excludes(self):
        # All filters present, but tools is False — must be excluded.
        query = ModelQuery(
            capabilities=("vision", "tools"),
            tasks=("chat",),
            input_modalities=("image",),
            min_context_window=32000,
        )
        bad = _make_model(
            "bad",
            vision=True,
            tools=False,
            input_modalities=("text", "image"),
            context_window=64000,
            task_types=("chat",),
        )
        assert query.matches(bad) is False

    def test_empty_query_matches_any_model(self):
        query = ModelQuery()
        any_model = _make_model("any")
        assert query.matches(any_model) is True


# ---------------------------------------------------------------------------
# ModelRegistry.query
# ---------------------------------------------------------------------------


class TestModelRegistryQuery:
    def test_empty_query_returns_all_models_sorted(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(ModelQuery())

        assert len(results) == 3
        # Sorted by (provider_id, model_id)
        assert [pid for pid, _ in results] == [
            "test_provider_a",
            "test_provider_b",
            "test_provider_b",
        ]
        assert [m.model_id for _, m in results] == [
            "model-alpha",
            "model-beta",
            "model-gamma",
        ]

    def test_provider_id_filter_narrows_results(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(ModelQuery(provider_id="test_provider_b"))

        assert [m.model_id for _, m in results] == ["model-beta", "model-gamma"]
        assert all(pid == "test_provider_b" for pid, _ in results)

    def test_unknown_provider_id_returns_empty(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(ModelQuery(provider_id="nonexistent_provider"))
        assert results == []

    def test_task_filter(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(ModelQuery(tasks=("image_understanding",)))

        # Both model-alpha and model-beta derive image_understanding from
        # their vision input modalities. model-gamma does not.
        assert sorted(m.model_id for _, m in results) == ["model-alpha", "model-beta"]

    def test_capability_filter(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(ModelQuery(capabilities=("vision", "tools")))

        # model-beta has vision=True and tools=True; model-alpha has
        # vision=True but tools=False; model-gamma has tools=True but
        # vision=False.
        assert [m.model_id for _, m in results] == ["model-beta"]

    def test_capability_filter_reasoning(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(ModelQuery(capabilities=("reasoning",)))

        # Only model-beta has reasoning.supported=True.
        assert [m.model_id for _, m in results] == ["model-beta"]

    def test_input_modality_filter(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(ModelQuery(input_modalities=("image",)))

        # Only model-alpha and model-beta have image input.
        assert sorted(m.model_id for _, m in results) == ["model-alpha", "model-beta"]

    def test_min_context_window_filter(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(ModelQuery(min_context_window=100000))

        # Only model-beta has context_window >= 100000.
        assert [m.model_id for _, m in results] == ["model-beta"]

    def test_combined_provider_and_task(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(
            ModelQuery(
                provider_id="test_provider_b",
                tasks=("image_understanding",),
            )
        )

        # model-beta has image_understanding derived from its vision
        # input; model-gamma does not.
        assert [m.model_id for _, m in results] == ["model-beta"]

    def test_returns_provider_id_with_each_model(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        results = registry.query(ModelQuery(tasks=("image_understanding",)))

        assert len(results) == 2
        for provider_id, model in results:
            assert isinstance(provider_id, str)
            assert isinstance(model, Model)
        # The first result is from the lexicographically-first provider.
        assert results[0][0] == "test_provider_a"

    def test_registry_with_no_models_returns_empty(self):
        empty = ModelRegistry({})
        assert empty.query(ModelQuery()) == []
        assert empty.query(ModelQuery(provider_id="any")) == []


# ---------------------------------------------------------------------------
# ModelQuery + ModelRegistry end-to-end with builder
# ---------------------------------------------------------------------------


class TestFromFiltersEndToEnd:
    def test_builder_produces_query_usable_with_registry(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        query = ModelQuery.from_filters({"task": "image_understanding", "min_context_window": 1000})
        results = registry.query(query)

        # Both model-alpha (32k) and model-beta (128k) clear 1k minimum
        # and have image_understanding in their derived task_types.
        assert sorted(m.model_id for _, m in results) == [
            "model-alpha",
            "model-beta",
        ]

    def test_builder_aliases_for_same_field_unioned(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        query = ModelQuery.from_filters(
            {
                "task": "image_understanding",
                "task_type": "chat",
            }
        )
        # model-alpha and model-beta have both; model-gamma has chat
        # only — fails the image_understanding check.
        results = registry.query(query)
        assert sorted(m.model_id for _, m in results) == [
            "model-alpha",
            "model-beta",
        ]

    def test_builder_capability_list_with_booleans_and_task_types(self):
        registry = ModelRegistry.load(FIXTURES_DIR)
        query = ModelQuery.from_filters({"capability": ["tools", "image_understanding"]})
        # "tools" is a boolean check; "image_understanding" is treated
        # as a task_type. Only model-beta has both: tools=True and
        # image_understanding in task_types.
        results = registry.query(query)
        assert [m.model_id for _, m in results] == ["model-beta"]
