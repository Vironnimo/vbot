"""Tests for the backend-owned task-model option schemas."""

from __future__ import annotations

from typing import Any

import pytest

from core.model_tasks.constants import (
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_EMBEDDING,
    TASK_TEXT_TO_SPEECH,
)
from core.model_tasks.options import (
    ALLOWED_OPTION_TYPES,
    PROVIDER_DEFAULT_CHOICE_LABEL,
    TaskModelOptionField,
    TaskModelOptionValidationError,
    option_schema_for,
)
from core.models import Capabilities, Model, ReasoningCapabilities


def test_allowed_option_types_includes_json() -> None:
    """The ``json`` field type is part of the supported set so the Settings
    UI can render generic array/object options like the extra-options
    escape hatch or provider passthrough options."""

    assert "json" in ALLOWED_OPTION_TYPES
    # Existing renderable types remain in the whitelist.
    for known in ("text", "textarea", "select", "number", "boolean"):
        assert known in ALLOWED_OPTION_TYPES


def test_task_model_option_field_accepts_json_type() -> None:
    """A field declared as ``json`` is constructed and serialized as ``json``.
    The default value is passed through untouched so the frontend receives
    the raw array/object the provider expects."""

    field = TaskModelOptionField(
        name="text_layout",
        type="json",
        label="Text layout",
        default=[{"text": "hi", "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
        description="Array of {text, bbox} entries (recraft-v3).",
    )

    assert field.type == "json"
    payload = field.to_dict()
    assert payload["type"] == "json"
    assert payload["name"] == "text_layout"
    # Default is preserved as-is — backend does not transform JSON values.
    assert payload["default"] == [{"text": "hi", "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]]}]
    assert payload["description"].startswith("Array of ")


def test_task_model_option_field_rejects_unknown_type() -> None:
    """Unknown field types are rejected up front so the renderer never sees
    them as silent fallbacks. ``json`` is allowed; ``totally-unknown`` is not."""

    with pytest.raises(TaskModelOptionValidationError, match="totally-unknown"):
        TaskModelOptionField(
            name="x",
            type="totally-unknown",
            label="X",
        )

    # Sanity: the constructor's error names the actual offending type.
    with pytest.raises(TaskModelOptionValidationError, match="json-list"):
        TaskModelOptionField(
            name="x",
            type="json-list",  # plausible typo
            label="X",
        )


def test_task_model_option_field_rejects_empty_name() -> None:
    """An empty name would render as an unkeyed option in the binding — reject it."""

    with pytest.raises(TaskModelOptionValidationError, match="name"):
        TaskModelOptionField(name="", type="json", label="X")


def test_existing_field_types_still_validate() -> None:
    """Sanity: the pre-existing field types still construct without error,
    and each reports the correct ``type`` in its serialized form."""

    cases = [
        ("text", "language"),
        ("textarea", "instructions"),
        ("number", "temperature"),
        ("boolean", "enabled"),
    ]
    for field_type, name in cases:
        field = TaskModelOptionField(name=name, type=field_type, label=name.title())
        assert field.type == field_type
        assert field.to_dict()["type"] == field_type


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------


def _make_model(
    model_id: str,
    *,
    supported_voices: tuple[str, ...] = (),
    supported_parameters: tuple[str, ...] = (),
    task_options: dict[str, Any] | None = None,
) -> Model:
    """Build a real ``Model`` instance for the model-aware schema tests."""

    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
            supported_voices=supported_voices,
            supported_parameters=supported_parameters,
            task_options=task_options or {},
        ),
        context_window=32000,
        max_output_tokens=4096,
    )


def _image_schema(model: Model | None, provider_id: str = "openrouter"):
    return option_schema_for(
        TASK_IMAGE_GENERATION,
        provider_id,
        f"{provider_id}/some-model::api-key",
        model=model,
    )


# ---------------------------------------------------------------------------
# Escape hatch — every supported task type carries extra_options
# ---------------------------------------------------------------------------


def test_every_supported_task_schema_ends_with_extra_options() -> None:
    """Every provider target schema carries the ``extra_options`` JSON
    escape hatch as its last field, so options vBot does not surface stay
    usable without a code change."""

    for task_type in (
        TASK_SPEECH_TO_TEXT,
        TASK_TEXT_TO_SPEECH,
        TASK_IMAGE_GENERATION,
        TASK_TEXT_EMBEDDING,
    ):
        schema = option_schema_for(task_type, "openrouter", "openrouter/x::api-key")
        assert schema.fields[-1].name == "extra_options"
        assert schema.fields[-1].type == "json"


def test_option_schema_for_unrecognized_task_type_returns_empty_schema() -> None:
    """Defensive: an unknown task type returns an empty schema without
    raising — and without the escape hatch, because no wire client would
    consume it."""

    schema = option_schema_for(
        "future_task",
        "openrouter",
        "openrouter/x::api-key",
        model=None,
    )

    assert schema.task_type == "future_task"
    assert schema.fields == ()


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


def test_option_schema_for_tts_uses_supported_voices_from_model() -> None:
    """When the model carries ``supported_voices``, the TTS schema
    surfaces exactly those voices and marks the field required."""

    voices = ("af_alloy", "af_aoede", "af_bella", "af_jessica")
    model = _make_model("hexgrad/kokoro-82m", supported_voices=voices)

    schema = option_schema_for(
        TASK_TEXT_TO_SPEECH,
        "openrouter",
        "openrouter/hexgrad/kokoro-82m::api-key",
        model=model,
    )

    voice_field = schema.fields[0]
    assert voice_field.name == "voice"
    assert voice_field.required is True
    assert [choice.value for choice in voice_field.options] == list(voices)
    field_names = {field.name for field in schema.fields}
    assert {"response_format", "speed"} <= field_names
    assert "instructions" not in field_names


def test_option_schema_for_tts_falls_back_to_openai_voices_for_openai_provider() -> None:
    """``provider_id == "openai"`` with no model still uses the OpenAI
    voice list as a select. This is the only provider that gets a hard-
    coded fallback list — every other provider must wait for the model
    to publish ``supported_voices``."""

    schema = option_schema_for(
        TASK_TEXT_TO_SPEECH,
        "openai",
        "openai/tts-1::api-key",
    )

    voice_field = schema.fields[0]
    assert voice_field.type == "select"
    assert voice_field.default == "alloy"
    voice_values = {choice.value for choice in voice_field.options}
    assert voice_values == {
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


def test_option_schema_for_tts_uses_free_text_voice_for_unknown_provider() -> None:
    """For an unknown provider with no model, the voice field is a
    free-text input — the OpenAI voice list is not invented for other
    providers."""

    schema = option_schema_for(
        TASK_TEXT_TO_SPEECH,
        "openrouter",
        "openrouter/unknown-model::api-key",
    )

    voice_field = schema.fields[0]
    assert voice_field.name == "voice"
    assert voice_field.type == "text"
    # No default — the user must provide a model-accepted voice id.
    assert voice_field.default in (None, "")


def test_option_schema_for_openai_tts_instructions_only_when_advertised() -> None:
    """The ``instructions`` field is model-specific: it is exposed only
    for models that advertise support (gpt-4o-mini-tts); tts-1 never
    exposes the field."""

    gpt4o = _make_model(
        "gpt-4o-mini-tts",
        supported_parameters=("voice", "response_format", "speed", "instructions"),
    )
    tts1 = _make_model(
        "tts-1",
        supported_parameters=("voice", "response_format", "speed"),
    )

    gpt4o_schema = option_schema_for(
        TASK_TEXT_TO_SPEECH,
        "openai",
        "openai/gpt-4o-mini-tts::api-key",
        model=gpt4o,
    )
    tts1_schema = option_schema_for(
        TASK_TEXT_TO_SPEECH,
        "openai",
        "openai/tts-1::api-key",
        model=tts1,
    )

    gpt4o_names = {field.name for field in gpt4o_schema.fields}
    tts1_names = {field.name for field in tts1_schema.fields}

    assert "instructions" in gpt4o_names
    assert "instructions" not in tts1_names
    for schema_names in (gpt4o_names, tts1_names):
        assert {"voice", "response_format", "speed"} <= schema_names


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------


def test_option_schema_for_stt_response_format_field_type() -> None:
    """``response_format`` is a select field with the Whisper-style
    format set when the model advertises support for it."""

    model = _make_model("openai/whisper-1", supported_parameters=("response_format",))

    schema = option_schema_for(
        TASK_SPEECH_TO_TEXT,
        "openrouter",
        "openrouter/openai/whisper-1::api-key",
        model=model,
    )

    response_format = next(field for field in schema.fields if field.name == "response_format")
    assert response_format.type == "select"
    assert {choice.value for choice in response_format.options} == {
        "json",
        "text",
        "srt",
        "verbose_json",
        "vtt",
    }
    assert response_format.default == "json"


# ---------------------------------------------------------------------------
# Image — data-driven from capabilities.task_options
# ---------------------------------------------------------------------------


def test_image_enum_parameter_renders_select_with_provider_default_choice() -> None:
    """An ``enum`` parameter renders as a select whose first choice is the
    empty "Provider default" entry, so nothing is sent unless the user
    pins a value (the wire layer drops empty placeholders)."""

    model = _make_model(
        "google/gemini-3.1-flash-image-preview",
        task_options={
            "image_generation": {
                "parameters": {
                    "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9", "9:16"]},
                    "resolution": {"type": "enum", "values": ["512", "1K", "2K", "4K"]},
                }
            }
        },
    )

    schema = _image_schema(model)
    aspect = next(field for field in schema.fields if field.name == "aspect_ratio")
    assert aspect.type == "select"
    assert aspect.default == ""
    assert aspect.options[0].value == ""
    assert aspect.options[0].label == PROVIDER_DEFAULT_CHOICE_LABEL
    assert [choice.value for choice in aspect.options[1:]] == ["1:1", "16:9", "9:16"]

    resolution = next(field for field in schema.fields if field.name == "resolution")
    assert [choice.value for choice in resolution.options[1:]] == ["512", "1K", "2K", "4K"]


def test_image_forced_default_enum_has_no_provider_default_choice() -> None:
    """``response_format`` must default to ``b64_json`` because the wire
    layer only decodes inline Base64 — the provider default (url) would
    break parsing, so there is no empty choice."""

    model = _make_model(
        "dall-e-3",
        task_options={
            "image_generation": {
                "parameters": {
                    "response_format": {"type": "enum", "values": ["b64_json", "url"]},
                }
            }
        },
    )

    schema = _image_schema(model, provider_id="openai")
    response_format = next(field for field in schema.fields if field.name == "response_format")
    assert response_format.default == "b64_json"
    assert all(choice.value for choice in response_format.options)


def test_image_range_parameter_renders_bounded_number() -> None:
    """A ``range`` parameter renders as a number field with min/max; a
    collapsed range (min == max) offers no choice and is skipped."""

    model = _make_model(
        "recraft/recraft-v3",
        task_options={
            "image_generation": {
                "parameters": {
                    "n": {"type": "range", "min": 1, "max": 6},
                    "input_fidelity": {"type": "range", "min": 2, "max": 2},
                }
            }
        },
    )

    schema = _image_schema(model)
    field_names = {field.name for field in schema.fields}
    assert "input_fidelity" not in field_names

    n = next(field for field in schema.fields if field.name == "n")
    assert n.type == "number"
    assert n.min_value == 1
    assert n.max_value == 6
    assert n.default is None


def test_image_boolean_seed_renders_number_field() -> None:
    """The feed types ``seed`` as boolean ("supported"); the value is a
    free integer, so the field renders as a number input."""

    model = _make_model(
        "black-forest-labs/flux.2-pro",
        task_options={
            "image_generation": {
                "parameters": {
                    "seed": {"type": "boolean"},
                }
            }
        },
    )

    schema = _image_schema(model)
    seed = next(field for field in schema.fields if field.name == "seed")
    assert seed.type == "number"
    assert seed.default is None


def test_image_string_parameter_renders_text_field_with_spec_description() -> None:
    """A hand-authored ``string`` spec (open value space, e.g. gpt-image-2
    arbitrary sizes) renders as a free-text field; a per-spec description
    wins over the generic per-name hint."""

    model = _make_model(
        "gpt-image-2",
        task_options={
            "image_generation": {
                "parameters": {
                    "size": {
                        "type": "string",
                        "description": "auto or WIDTHxHEIGHT divisible by 16.",
                    },
                }
            }
        },
    )

    schema = _image_schema(model, provider_id="openai")
    size = next(field for field in schema.fields if field.name == "size")
    assert size.type == "text"
    assert size.description == "auto or WIDTHxHEIGHT divisible by 16."


def test_image_size_shorthand_skipped_when_resolution_or_aspect_present() -> None:
    """OpenRouter's ``size`` is a shorthand that conflicts with
    resolution/aspect_ratio; it is skipped when either is present but
    rendered when it is the only dimension parameter (OpenAI native)."""

    with_conflict = _make_model(
        "bytedance-seed/seedream-4.5",
        task_options={
            "image_generation": {
                "parameters": {
                    "size": {"type": "enum", "values": ["1K", "2K"]},
                    "resolution": {"type": "enum", "values": ["1K", "2K", "4K"]},
                }
            }
        },
    )
    alone = _make_model(
        "gpt-image-1",
        task_options={
            "image_generation": {
                "parameters": {
                    "size": {"type": "enum", "values": ["auto", "1024x1024"]},
                }
            }
        },
    )

    conflict_names = {field.name for field in _image_schema(with_conflict).fields}
    alone_names = {field.name for field in _image_schema(alone, provider_id="openai").fields}
    assert "size" not in conflict_names
    assert "resolution" in conflict_names
    assert "size" in alone_names


def test_image_runtime_parameters_and_unknown_spec_types_are_skipped() -> None:
    """``input_references``/``stream`` are runtime inputs, not settings;
    unknown spec types are skipped fail-soft (the raw catalog still
    carries them for a later renderer upgrade)."""

    model = _make_model(
        "sourceful/riverflow-v2.5-pro",
        task_options={
            "image_generation": {
                "parameters": {
                    "input_references": {"type": "range", "min": 0, "max": 10},
                    "stream": {"type": "boolean"},
                    "novelty": {"type": "matrix", "rows": 3},
                    "resolution": {"type": "enum", "values": ["1K", "2K", "4K"]},
                }
            }
        },
    )

    schema = _image_schema(model)
    field_names = {field.name for field in schema.fields}
    assert "input_references" not in field_names
    assert "stream" not in field_names
    assert "novelty" not in field_names
    assert "resolution" in field_names


def test_image_single_value_enum_is_skipped() -> None:
    """An enum with one published value offers no choice (dall-e-2
    ``quality: ["standard"]``) — the parameter stays unsent so the
    provider's fixed value is authoritative."""

    model = _make_model(
        "dall-e-2",
        task_options={
            "image_generation": {
                "parameters": {
                    "quality": {"type": "enum", "values": ["standard"]},
                    "size": {"type": "enum", "values": ["256x256", "512x512", "1024x1024"]},
                }
            }
        },
    )

    schema = _image_schema(model, provider_id="openai")
    field_names = {field.name for field in schema.fields}
    assert "quality" not in field_names
    assert "size" in field_names


def test_image_parameter_order_is_known_first_then_sorted() -> None:
    """Known parameters render in presentation order; unknown names follow
    alphabetically."""

    model = _make_model(
        "x/experimental",
        task_options={
            "image_generation": {
                "parameters": {
                    "zeta_mode": {"type": "enum", "values": ["a", "b"]},
                    "seed": {"type": "boolean"},
                    "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9"]},
                    "alpha_mode": {"type": "enum", "values": ["x", "y"]},
                }
            }
        },
    )

    schema = _image_schema(model)
    names = [field.name for field in schema.fields if field.name != "extra_options"]
    assert names == ["aspect_ratio", "seed", "alpha_mode", "zeta_mode"]


def test_image_passthrough_renders_provider_options_json_field() -> None:
    """Passthrough keys render as one ``provider_options`` JSON field whose
    description names the provider slug and its allowed keys."""

    model = _make_model(
        "recraft/recraft-v3",
        task_options={
            "image_generation": {
                "parameters": {"n": {"type": "range", "min": 1, "max": 6}},
                "passthrough": {"recraft": ["controls", "style", "text_layout"]},
            }
        },
    )

    schema = _image_schema(model)
    provider_options = next(field for field in schema.fields if field.name == "provider_options")
    assert provider_options.type == "json"
    assert provider_options.default == {}
    assert "recraft: controls, style, text_layout" in provider_options.description


def test_image_openrouter_fallback_without_task_options() -> None:
    """A model without task-options data (unrefreshed catalog) falls back
    to the conservative aspect-ratio/resolution selects; ``seed`` appears
    only when the chat catalog advertises it."""

    with_seed = _make_model("black-forest-labs/flux.2-pro", supported_parameters=("seed",))
    without_seed = _make_model("recraft/recraft-v3")

    with_seed_names = {field.name for field in _image_schema(with_seed).fields}
    without_seed_names = {field.name for field in _image_schema(without_seed).fields}

    assert {"aspect_ratio", "resolution"} <= with_seed_names
    assert "seed" in with_seed_names
    assert "seed" not in without_seed_names


def test_image_openai_fallback_without_model_exposes_union_of_fields() -> None:
    """When the registry has no model yet (e.g. before the first catalog
    refresh) the OpenAI image fallback exposes the union of supported
    fields so the user can still configure the target."""

    schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "openai",
        "openai/gpt-image-1::api-key",
        model=None,
    )

    field_names = {field.name for field in schema.fields}
    assert {
        "size",
        "quality",
        "background",
        "n",
        "output_format",
        "style",
        "response_format",
    } <= field_names


def test_image_openai_fallback_gated_by_supported_parameters() -> None:
    """With a model but no task-options data, the OpenAI fallback exposes
    only the flat ``supported_parameters`` subset."""

    model = _make_model(
        "gpt-image-1",
        supported_parameters=("size", "quality", "background", "n", "output_format"),
    )

    schema = _image_schema(model, provider_id="openai")
    field_names = {field.name for field in schema.fields}
    assert {"size", "quality", "background", "n", "output_format"} <= field_names
    assert "style" not in field_names
    assert "response_format" not in field_names


def test_image_unknown_provider_gets_only_escape_hatch() -> None:
    """Image schemas for providers without an execution profile stay empty
    apart from the escape hatch — the UI must not invent inputs."""

    schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "some-other-provider",
        "some-other-provider/gpt-image-1::api-key",
        model=None,
    )

    assert [field.name for field in schema.fields] == ["extra_options"]


def test_image_openai_task_options_profile_drives_fields() -> None:
    """The override-authored OpenAI profiles render from data: dall-e-3
    exposes size/quality/style/response_format (n collapses), gpt-image-1
    exposes the GPT set — no prefix matching anywhere."""

    dall_e_3 = _make_model(
        "dall-e-3",
        task_options={
            "image_generation": {
                "parameters": {
                    "size": {"type": "enum", "values": ["1024x1024", "1792x1024", "1024x1792"]},
                    "quality": {"type": "enum", "values": ["standard", "hd"]},
                    "style": {"type": "enum", "values": ["vivid", "natural"]},
                    "response_format": {"type": "enum", "values": ["b64_json", "url"]},
                    "n": {"type": "range", "min": 1, "max": 1},
                }
            }
        },
    )

    schema = _image_schema(dall_e_3, provider_id="openai")
    field_names = [field.name for field in schema.fields]
    assert field_names == [
        "size",
        "quality",
        "style",
        "response_format",
        "extra_options",
    ]


# ---------------------------------------------------------------------------
# Text embedding
# ---------------------------------------------------------------------------


def test_option_schema_for_text_embedding_emits_dimensions_field() -> None:
    """A text_embedding schema for an unknown model exposes the
    ``dimensions`` field (union shown before the catalog is loaded)."""

    schema = option_schema_for(
        TASK_TEXT_EMBEDDING,
        "openrouter",
        "openrouter/google/gemini-embedding-2::api-key",
        model=None,
    )

    field_names = {field.name for field in schema.fields}
    assert field_names == {"dimensions", "extra_options"}

    dimensions = schema.fields[0]
    assert dimensions.name == "dimensions"
    assert dimensions.type == "number"
    # The schema emits a None default; the wire layer drops None
    # before sending, so the request omits ``dimensions`` by default.
    assert dimensions.default is None
    assert dimensions.min_value == 1
    assert dimensions.step == 1


def test_option_schema_for_text_embedding_without_supported_dimensions() -> None:
    """A model that does not list ``dimensions`` in
    ``supported_parameters`` has no dimensions knob — the Settings UI
    does not invent one the provider would reject."""

    model = _make_model("some/embedding-model-v1", supported_parameters=())

    schema = option_schema_for(
        TASK_TEXT_EMBEDDING,
        "openrouter",
        "openrouter/some/embedding-model-v1::api-key",
        model=model,
    )

    assert [field.name for field in schema.fields] == ["extra_options"]


def test_option_schema_for_text_embedding_default_options_only_escape_hatch() -> None:
    """The embedding schema's defaults carry only the empty escape hatch —
    the ``dimensions`` field has ``default=None`` and the wire layer drops
    empty placeholders, so a binding with no stored options produces a
    request without ``dimensions``."""

    schema = option_schema_for(
        TASK_TEXT_EMBEDDING,
        "openrouter",
        "openrouter/google/gemini-embedding-2::api-key",
        model=None,
    )

    assert schema.default_options() == {"extra_options": {}}


def test_option_schema_for_text_embedding_to_dict_has_dimensions_field() -> None:
    """The serialized form reaches the Settings UI as a ``number`` field
    with min=1 and an explicit description of the Matryoshka behavior."""

    schema = option_schema_for(
        TASK_TEXT_EMBEDDING,
        "openrouter",
        "openrouter/google/gemini-embedding-2::api-key",
        model=None,
    )

    rendered = schema.to_dict()
    assert rendered["task_type"] == TASK_TEXT_EMBEDDING
    assert rendered["target"] == "openrouter/google/gemini-embedding-2::api-key"
    field = next(item for item in rendered["fields"] if item["name"] == "dimensions")
    assert field["type"] == "number"
    assert field["default"] is None
    assert field["min"] == 1
    assert "Matryoshka" in field["description"]
