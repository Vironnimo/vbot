"""Models: ``ModelQuery`` filters and ``ModelRegistry.query``."""

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

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _make_model(
    model_id: str,
    *,
    context_window: int | None = 32000,
    reasoning: bool = False,
    **capabilities: Any,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            **{"vision": False, "tools": False, "json_mode": False} | capabilities,
            reasoning=ReasoningCapabilities(supported=reasoning),
        ),
        context_window=context_window,
        max_output_tokens=4096,
    )


_MODELS = [
    _make_model(
        "vision-tools",
        vision=True,
        tools=True,
        input_modalities=("text", "image"),
        task_types=("chat", "image_understanding"),
        context_window=64000,
    ),
    _make_model("json-reasoning", json_mode=True, reasoning=True, context_window=1000),
    _make_model("image-gen", task_types=("chat", "text_output", "image_generation")),
    _make_model("tts", output_modalities=("text", "speech"), context_window=128000),
    _make_model("window-less", context_window=None),
]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({"unknown_field": "x", "another": 42}, ModelQuery(), id="unknown-keys"),
        pytest.param(
            {
                "capability": "tools",
                "capabilities": ["vision", "tools"],
                "task": "image_generation",
                "tasks": ["text_to_speech"],
                "task_type": "image_generation",
                "task_types": ["speech_to_text"],
                "input_modality": "image",
                "input_modalities": ["audio", "image"],
                "output_modality": "speech",
                "output_modalities": ["text", "speech"],
            },
            ModelQuery(
                capabilities=("tools", "vision"),
                tasks=("image_generation", "text_to_speech", "speech_to_text"),
                input_modalities=("image", "audio"),
                output_modalities=("speech", "text"),
            ),
            id="aliases-collapse-per-field",
        ),
        pytest.param(
            {
                "provider_id": "  OpenAI  ",
                "tasks": ["  IMAGE_GENERATION  ", "chat", "CHAT", "", "  ", "\t"],
                "min_context_window": 128000,
            },
            ModelQuery(
                provider_id="openai", tasks=("image_generation", "chat"), min_context_window=128000
            ),
            id="values-are-trimmed-lowercased-and-deduplicated",
        ),
        pytest.param(
            {"provider_id": "   ", "min_context_window": 0, "tasks": "chat"},
            ModelQuery(tasks=("chat",)),
            id="blank-provider-and-zero-window-mean-no-filter",
        ),
    ],
)
def test_from_filters_normalizes_raw_filter_values(
    params: dict[str, Any], expected: ModelQuery
) -> None:
    assert ModelQuery.from_filters(params) == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_id", 42),
        ("input_modality", ["text", 99]),
        ("min_context_window", -1),
        ("min_context_window", True),
    ],
)
def test_from_filters_rejects_mistyped_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        ModelQuery.from_filters({field: value})


def test_query_is_frozen() -> None:
    query = ModelQuery(provider_id="openai", tasks=("chat",))

    with pytest.raises(FrozenInstanceError):
        query.provider_id = "anthropic"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("query", "matching"),
    [
        pytest.param(ModelQuery(), [model.model_id for model in _MODELS], id="empty-query"),
        pytest.param(ModelQuery(tasks=("image_generation",)), ["image-gen"], id="task"),
        pytest.param(
            ModelQuery(tasks=("chat", "image_understanding")),
            ["vision-tools"],
            id="every-task-required",
        ),
        pytest.param(ModelQuery(capabilities=("vision",)), ["vision-tools"], id="vision"),
        pytest.param(ModelQuery(capabilities=("tools",)), ["vision-tools"], id="tools"),
        pytest.param(ModelQuery(capabilities=("json_mode",)), ["json-reasoning"], id="json-mode"),
        pytest.param(
            ModelQuery(capabilities=("reasoning",)),
            ["json-reasoning"],
            id="reasoning-reads-supported",
        ),
        pytest.param(
            ModelQuery(capabilities=("image_generation",)),
            ["image-gen"],
            id="other-capability-is-a-task-type",
        ),
        pytest.param(
            ModelQuery(input_modalities=("text", "image")),
            ["vision-tools"],
            id="every-input-modality-required",
        ),
        pytest.param(ModelQuery(output_modalities=("speech",)), ["tts"], id="output-modality"),
        # The minimum is inclusive; an unknown window cannot prove it and is excluded.
        pytest.param(
            ModelQuery(min_context_window=64000), ["vision-tools", "tts"], id="min-context-window"
        ),
        pytest.param(
            ModelQuery(
                capabilities=("vision", "tools"),
                tasks=("chat",),
                input_modalities=("text", "image"),
                min_context_window=32000,
            ),
            ["vision-tools"],
            id="all-filters-combined",
        ),
    ],
)
def test_matches_requires_every_filter(query: ModelQuery, matching: list[str]) -> None:
    assert [model.model_id for model in _MODELS if query.matches(model)] == matching


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param(
            {},
            [
                ("test_provider_a", "model-alpha"),
                ("test_provider_b", "model-beta"),
                ("test_provider_b", "model-gamma"),
            ],
            id="all-sorted-by-provider-and-model",
        ),
        pytest.param(
            {"provider_id": "test_provider_b"},
            [("test_provider_b", "model-beta"), ("test_provider_b", "model-gamma")],
            id="provider",
        ),
        pytest.param({"provider_id": "nonexistent_provider"}, [], id="unknown-provider"),
        # Both vision models derive image_understanding from their image input.
        pytest.param(
            {"task": "image_understanding", "min_context_window": 1000},
            [("test_provider_a", "model-alpha"), ("test_provider_b", "model-beta")],
            id="derived-task",
        ),
        pytest.param(
            {"provider_id": "test_provider_b", "task": "image_understanding"},
            [("test_provider_b", "model-beta")],
            id="provider-and-task",
        ),
        pytest.param(
            {"capability": ["tools", "image_understanding"]},
            [("test_provider_b", "model-beta")],
            id="boolean-and-task-capabilities",
        ),
    ],
)
def test_registry_query_filters_the_loaded_catalog(
    params: dict[str, Any], expected: list[tuple[str, str]]
) -> None:
    registry = ModelRegistry.load(FIXTURES_DIR)

    results = registry.query(ModelQuery.from_filters(params))

    assert [(provider_id, model.model_id) for provider_id, model in results] == expected
