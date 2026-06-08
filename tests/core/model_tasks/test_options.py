"""Tests for the backend-owned task-model option schemas."""

from __future__ import annotations

import pytest

from core.model_tasks.constants import (
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_EMBEDDING,
    TASK_TEXT_TO_SPEECH,
)
from core.model_tasks.options import (
    ALLOWED_OPTION_TYPES,
    TaskModelOptionField,
    TaskModelOptionValidationError,
    option_schema_for,
)
from core.models import Capabilities, Model, ReasoningCapabilities


def test_allowed_option_types_includes_json() -> None:
    """The new ``json`` field type is part of the supported set so the
    Settings UI can render generic array/object options like Recraft's
    ``text_layout`` or Sourceful's ``scoring_rubric``."""

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
    """Sanity: the four pre-existing field types still construct without
    error, and each reports the correct ``type`` in its serialized form."""

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


def test_option_schema_for_unchanged_for_existing_tasks() -> None:
    """The existing task branches (TTS, STT, image) still produce the
    pre-M2 schemas — adding the ``json`` type does not silently alter the
    recognized task types."""

    tts_schema = option_schema_for(TASK_TEXT_TO_SPEECH, "openai", "openai/tts-1::api-key")
    assert [field.name for field in tts_schema.fields[:1]] == ["voice"]

    stt_schema = option_schema_for(TASK_SPEECH_TO_TEXT, "openai", "openai/whisper-1::api-key")
    assert "language" in [field.name for field in stt_schema.fields]

    image_schema = option_schema_for(
        TASK_IMAGE_GENERATION, "openrouter", "openrouter/flux.2-pro::api-key"
    )
    assert "aspect_ratio" in [field.name for field in image_schema.fields]


def test_task_model_option_field_choices_serialize_with_json_default() -> None:
    """A ``select`` field with a JSON-serializable default serializes
    correctly. This guards the to_dict path used by the renderer."""

    field = TaskModelOptionField(
        name="voice",
        type="select",
        label="Voice",
        default="alloy",
    )

    payload = field.to_dict()
    assert payload["type"] == "select"
    assert payload["default"] == "alloy"


def test_task_model_option_field_json_default_passthrough_complex_types() -> None:
    """JSON fields can carry nested arrays, objects, and primitives. The
    backend must not transform the value — the frontend stringifies it for
    display and parses it back on input."""

    items: list[dict[str, object]] = [
        {"key": "clarity", "weight": 0.6, "passing_score": 0.5},
        {"key": "style", "weight": 0.4},
    ]
    nested: dict[str, object] = {"items": items, "background": None}
    field = TaskModelOptionField(
        name="scoring_rubric",
        type="json",
        label="Scoring rubric",
        default=nested,
    )

    payload = field.to_dict()
    assert payload["default"] is nested
    nested_default = payload["default"]
    assert nested_default is not None
    assert isinstance(nested_default, dict)
    nested_items = nested_default["items"]
    assert isinstance(nested_items, list)
    first_item = nested_items[0]
    assert isinstance(first_item, dict)
    assert first_item["passing_score"] == 0.5


# ---------------------------------------------------------------------------
# Phase 3 — model-aware schema builders
#
# These tests exercise ``option_schema_for`` directly with a real ``Model``
# instance, so they cover the schema-side logic independently of
# ``TaskModelService.options`` (which is covered by test_model_tasks.py).
# ---------------------------------------------------------------------------


def _make_model(
    model_id: str,
    *,
    supported_voices: tuple[str, ...] = (),
    supported_parameters: tuple[str, ...] = (),
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
        ),
        context_window=32000,
        max_output_tokens=4096,
    )


def test_option_schema_for_tts_uses_supported_voices_from_model() -> None:
    """When the model carries ``supported_voices``, the TTS schema
    surfaces exactly those voices and marks the field required. The
    test mirrors the live kokoro / gemini-tts / voxtral / grok case where
    the per-model list is the only authoritative source."""

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
    # The TTS schema also includes response_format and speed regardless of
    # the voice list source. ``instructions`` is model-specific (Phase 5)
    # and only appears when the model advertises support for it.
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
    free-text input. The OpenAI voice list is not invented for other
    providers — that was the bug the phase fixed."""

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


def test_option_schema_for_image_gemini_flash_image_preview_exposes_half_k() -> None:
    """The Gemini 3.1 Flash Image preview model is the only one whose
    image_size choices include ``0.5K``. The schema reflects that."""

    model = _make_model("google/gemini-3.1-flash-image-preview")

    schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "openrouter",
        "openrouter/google/gemini-3.1-flash-image-preview::api-key",
        model=model,
    )

    size_field = next(field for field in schema.fields if field.name == "image_size")
    size_values = {choice.value for choice in size_field.options}
    assert size_values == {"0.5K", "1K", "2K", "4K"}


def test_option_schema_for_image_mai_image_2_5_exposes_reduced_ratios() -> None:
    """MAI Image 2.5 advertises a reduced aspect-ratio set; the schema
    must match. The other base ratios are absent."""

    model = _make_model("microsoft/mai-image-2.5")

    schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "openrouter",
        "openrouter/microsoft/mai-image-2.5::api-key",
        model=model,
    )

    aspect_field = next(field for field in schema.fields if field.name == "aspect_ratio")
    aspect_values = {choice.value for choice in aspect_field.options}
    assert aspect_values == {"1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3"}


def test_option_schema_for_image_seed_only_present_when_in_supported_parameters() -> None:
    """``seed`` is added only when ``supported_parameters`` includes it —
    the field is provider-level top-level, so it must not appear for
    models that do not list it (recraft, sourceful, bytedance-seedream)."""

    flux_model = _make_model("black-forest-labs/flux.2-pro", supported_parameters=("seed",))
    recraft_model = _make_model("recraft/recraft-v3", supported_parameters=())

    flux_schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "openrouter",
        "openrouter/black-forest-labs/flux.2-pro::api-key",
        model=flux_model,
    )
    recraft_schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "openrouter",
        "openrouter/recraft/recraft-v3::api-key",
        model=recraft_model,
    )

    flux_names = {field.name for field in flux_schema.fields}
    recraft_names = {field.name for field in recraft_schema.fields}
    assert "seed" in flux_names
    assert "seed" not in recraft_names


def test_option_schema_for_image_recraft_v3_emits_expected_json_fields() -> None:
    """Recraft-v3 json-typed fields are typed as ``json`` so the
    Settings UI uses the multiline JSON textarea renderer. Strength is
    a number, style is a text input."""

    model = _make_model("recraft/recraft-v3")

    schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "openrouter",
        "openrouter/recraft/recraft-v3::api-key",
        model=model,
    )

    fields_by_name = {field.name: field for field in schema.fields}

    assert fields_by_name["text_layout"].type == "json"
    assert fields_by_name["rgb_colors"].type == "json"
    assert fields_by_name["background_rgb_color"].type == "json"
    assert fields_by_name["strength"].type == "number"
    assert fields_by_name["style"].type == "text"


def test_option_schema_for_image_sourceful_v25_emits_expected_json_fields() -> None:
    """Sourceful v2.5 json fields use the ``json`` type so the Settings
    UI renders them as the multiline JSON textarea. Scoring prompt is a
    textarea, background controls are select / text."""

    model = _make_model("sourceful/riverflow-v2.5-pro")

    schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "openrouter",
        "openrouter/sourceful/riverflow-v2.5-pro::api-key",
        model=model,
    )

    fields_by_name = {field.name: field for field in schema.fields}

    assert fields_by_name["font_inputs"].type == "json"
    assert fields_by_name["scoring_rubric"].type == "json"
    assert fields_by_name["scoring_prompt"].type == "textarea"
    assert fields_by_name["background_mode"].type == "select"
    assert fields_by_name["background_hex_color"].type == "text"
    # v2-only super_resolution_references is not in the v2.5 schema.
    assert "super_resolution_references" not in fields_by_name


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


def test_option_schema_for_non_openai_non_openrouter_provider_returns_empty_image_schema() -> None:
    """Image option schemas for unknown providers (neither OpenAI nor
    OpenRouter) stay empty. The OpenAI branch is the second native
    profile; everything else is out of scope for the model-aware schema
    builders."""

    schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "some-other-provider",
        "some-other-provider/gpt-image-1::api-key",
        model=None,
    )

    assert schema.fields == ()


def test_option_schema_for_openai_image_gpt_image_1_exposes_expected_fields() -> None:
    """The OpenAI gpt-image-1 native profile exposes the size, quality,
    background, n, and output_format fields. ``style`` and
    ``response_format`` are dall-e-3-only and must NOT appear here."""

    model = _make_model(
        "gpt-image-1",
        supported_parameters=("size", "quality", "background", "n", "output_format"),
    )

    schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "openai",
        "openai/gpt-image-1::api-key",
        model=model,
    )

    field_names = {field.name for field in schema.fields}
    assert {"size", "quality", "background", "n", "output_format"} <= field_names
    assert "style" not in field_names
    assert "response_format" not in field_names

    # gpt-image-1 quality defaults to "auto"; n is 1-10.
    quality = next(field for field in schema.fields if field.name == "quality")
    assert quality.default == "auto"
    n = next(field for field in schema.fields if field.name == "n")
    assert n.max_value == 10
    assert n.min_value == 1


def test_option_schema_for_openai_image_dall_e_3_exposes_expected_fields() -> None:
    """The OpenAI dall-e-3 native profile exposes size, quality, style,
    n, and response_format. ``background`` and ``output_format`` are
    gpt-image-1-only and must NOT appear here. ``n`` is restricted to 1."""

    model = _make_model(
        "dall-e-3",
        supported_parameters=("size", "quality", "style", "n", "response_format"),
    )

    schema = option_schema_for(
        TASK_IMAGE_GENERATION,
        "openai",
        "openai/dall-e-3::api-key",
        model=model,
    )

    field_names = {field.name for field in schema.fields}
    assert {"size", "quality", "style", "n", "response_format"} <= field_names
    assert "background" not in field_names
    assert "output_format" not in field_names

    n = next(field for field in schema.fields if field.name == "n")
    assert n.max_value == 1
    quality = next(field for field in schema.fields if field.name == "quality")
    assert quality.default == "standard"
    style = next(field for field in schema.fields if field.name == "style")
    assert style.default == "vivid"


def test_option_schema_for_openai_image_without_model_exposes_union_of_fields() -> None:
    """When the registry has no model yet (e.g. before the first catalog
    refresh) the OpenAI image profile exposes the union of supported
    fields so the user can still configure the target. The wire layer
    decides which subset to send to the selected model."""

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


def test_option_schema_for_openai_tts_instructions_only_for_gpt4o_mini() -> None:
    """The ``instructions`` field is model-specific: it is exposed only
    for OpenAI models that advertise support (gpt-4o-mini-tts). tts-1 and
    tts-1-hd never expose the field. The OpenAI TTS voice list is the
    canonical fallback when the model is unknown."""

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
    # The voice/format/speed trio is always present.
    for schema_names in (gpt4o_names, tts1_names):
        assert {"voice", "response_format", "speed"} <= schema_names


def test_option_schema_for_unrecognized_task_type_returns_empty_schema() -> None:
    """Defensive: an unknown task type returns an empty schema without
    raising. The dispatch is task-type-driven; future task types can
    add their own builders."""

    schema = option_schema_for(
        "future_task",
        "openrouter",
        "openrouter/x::api-key",
        model=None,
    )

    assert schema.task_type == "future_task"
    assert schema.fields == ()


# ---------------------------------------------------------------------------
# Phase 3 — text_embedding option schema
#
# The embedding task exposes the Matryoshka ``dimensions`` knob when
# the resolved model advertises it in ``supported_parameters``. The
# schema stays empty for models that do not list it (so the Settings
# UI does not invent a knob the provider would reject), and the
# ``dimensions`` field defaults to ``None`` — the wire layer drops
# ``None`` so the request omits the field for non-Matryoshka models.
# ---------------------------------------------------------------------------


def test_option_schema_for_text_embedding_emits_dimensions_field() -> None:
    """A text_embedding schema for an unknown model exposes the
    ``dimensions`` field. The catalog may not be loaded yet — the
    union of fields is shown so the user can still configure the
    target, mirroring the OpenAI image profile behavior.
    """

    schema = option_schema_for(
        TASK_TEXT_EMBEDDING,
        "openrouter",
        "openrouter/google/gemini-embedding-2::api-key",
        model=None,
    )

    field_names = {field.name for field in schema.fields}
    assert field_names == {"dimensions"}

    dimensions = schema.fields[0]
    assert dimensions.name == "dimensions"
    assert dimensions.type == "number"
    # The schema emits a None default; the wire layer drops None
    # before sending, so the request omits ``dimensions`` by default.
    assert dimensions.default is None
    assert dimensions.min_value == 1
    assert dimensions.step == 1


def test_option_schema_for_text_embedding_with_model_advertising_dimensions() -> None:
    """When the resolved model lists ``dimensions`` in
    ``supported_parameters``, the schema still emits the field — the
    wire layer is the source of truth for whether to forward it, and
    the Settings UI surfaces the knob whenever the model could
    accept it.
    """

    model = _make_model("google/gemini-embedding-2", supported_parameters=("dimensions",))

    schema = option_schema_for(
        TASK_TEXT_EMBEDDING,
        "openrouter",
        "openrouter/google/gemini-embedding-2::api-key",
        model=model,
    )

    field_names = {field.name for field in schema.fields}
    assert field_names == {"dimensions"}


def test_option_schema_for_text_embedding_without_supported_dimensions() -> None:
    """A model that does not list ``dimensions`` in
    ``supported_parameters`` has an empty embedding schema — the
    Settings UI does not invent a knob the provider would reject.
    """

    model = _make_model("some/embedding-model-v1", supported_parameters=())

    schema = option_schema_for(
        TASK_TEXT_EMBEDDING,
        "openrouter",
        "openrouter/some/embedding-model-v1::api-key",
        model=model,
    )

    assert schema.fields == ()


def test_option_schema_for_text_embedding_schema_default_options_is_empty() -> None:
    """The embedding schema's default options are empty — the
    ``dimensions`` field carries ``default=None`` and the schema
    default-options helper only includes fields whose default is not
    ``None``. The wire layer drops ``None`` anyway, so a binding with
    no stored options produces a request without ``dimensions``.
    """

    schema = option_schema_for(
        TASK_TEXT_EMBEDDING,
        "openrouter",
        "openrouter/google/gemini-embedding-2::api-key",
        model=None,
    )

    assert schema.default_options() == {}


def test_option_schema_for_text_embedding_to_dict_has_dimensions_field() -> None:
    """The serialized form reaches the Settings UI as a ``number`` field
    with min=1 and an explicit description of the Matryoshka
    behavior.
    """

    schema = option_schema_for(
        TASK_TEXT_EMBEDDING,
        "openrouter",
        "openrouter/google/gemini-embedding-2::api-key",
        model=None,
    )

    rendered = schema.to_dict()
    assert rendered["task_type"] == TASK_TEXT_EMBEDDING
    assert rendered["target"] == "openrouter/google/gemini-embedding-2::api-key"
    assert len(rendered["fields"]) == 1
    field = rendered["fields"][0]
    assert field["name"] == "dimensions"
    assert field["type"] == "number"
    assert field["default"] is None
    assert field["min"] == 1
    assert "Matryoshka" in field["description"]
