"""Tests for the backend-owned task-model option schemas."""

from __future__ import annotations

from typing import Any

import pytest

from core.model_tasks.constants import (
    TASK_IMAGE_GENERATION,
    TASK_MUSIC_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_EMBEDDING,
    TASK_TEXT_TO_SPEECH,
    TASK_VIDEO_GENERATION,
)
from core.model_tasks.options import (
    ALLOWED_OPTION_TYPES,
    PROVIDER_DEFAULT_CHOICE_LABEL,
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionsBy,
    TaskModelOptionSchema,
    TaskModelOptionValidationError,
    option_schema_for,
    validate_task_model_options,
)
from core.models import Capabilities, Model, ReasoningCapabilities


def test_field_types_are_the_renderable_set_and_json_defaults_pass_through() -> None:
    """The Settings UI renders exactly these field types; a ``json`` field hands
    its raw array/object default to the frontend unchanged."""

    assert {"text", "textarea", "select", "number", "boolean", "json"} == ALLOWED_OPTION_TYPES
    default = [{"text": "hi", "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]]}]
    field = TaskModelOptionField(
        name="text_layout", type="json", label="Text layout", default=default
    )

    assert field.to_dict()["type"] == "json"
    assert field.to_dict()["default"] == default


@pytest.mark.parametrize(
    ("name", "field_type", "message"),
    [
        pytest.param("x", "json-list", "json-list", id="unknown-type"),
        pytest.param("", "json", "name", id="empty-name"),
    ],
)
def test_invalid_field_declaration_is_rejected(name: str, field_type: str, message: str) -> None:
    with pytest.raises(TaskModelOptionValidationError, match=message):
        TaskModelOptionField(name=name, type=field_type, label="X")


def test_numeric_options_reject_overflow_as_validation_error() -> None:
    schema = option_schema_for(TASK_TEXT_TO_SPEECH, "openai", "openai/tts-1::api-key")

    with pytest.raises(TaskModelOptionValidationError):
        validate_task_model_options(schema, {"speed": 10**400})


def test_select_without_choices_accepts_no_value() -> None:
    """A select whose choices are empty (for example, no candidate Model is
    available) has no valid value; it never degrades into free text."""

    schema = TaskModelOptionSchema(
        task_type=TASK_TEXT_TO_SPEECH,
        target="openai/tts-1::api-key",
        fields=(TaskModelOptionField(name="voice", type="select", label="Voice"),),
    )

    validate_task_model_options(schema, {})
    with pytest.raises(TaskModelOptionValidationError):
        validate_task_model_options(schema, {"voice": "alloy"})


def _tier_field(
    options_by: TaskModelOptionsBy | None, *, field_type: str = "select"
) -> TaskModelOptionField:
    return TaskModelOptionField(
        name="tier",
        type=field_type,
        label="Tier",
        options=tuple(
            TaskModelOptionChoice(value=value, label=value or "Default")
            for value in ("", "fast", "deep")
        ),
        options_by=options_by,
    )


def test_select_serializes_choices_narrowed_by_another_field() -> None:
    field = _tier_field(TaskModelOptionsBy(field="engine", values={"lite": ("", "fast")}))

    assert field.to_dict()["options_by"] == {
        "field": "engine",
        "values": {"lite": ["", "fast"]},
    }
    assert "options_by" not in _tier_field(None).to_dict()


@pytest.mark.parametrize(
    ("options_by", "field_type"),
    [
        (TaskModelOptionsBy(field="tier", values={}), "select"),
        (TaskModelOptionsBy(field="engine", values={"lite": ("turbo",)}), "select"),
        (TaskModelOptionsBy(field="engine", values={}), "text"),
    ],
    ids=["self-reference", "unknown-choice", "not-a-select"],
)
def test_narrowed_choices_must_reference_another_field_and_known_choices(
    options_by: TaskModelOptionsBy, field_type: str
) -> None:
    with pytest.raises(TaskModelOptionValidationError):
        _tier_field(options_by, field_type=field_type)


@pytest.mark.parametrize(
    ("field", "values"),
    [("", {}), ("engine", {"lite": "fast"}), ("engine", {"lite": [1]}), ("engine", [])],
    ids=["empty-field", "string-list", "non-string-choice", "not-a-mapping"],
)
def test_narrowed_choices_reject_malformed_shapes(field: str, values: Any) -> None:
    with pytest.raises(TaskModelOptionValidationError):
        TaskModelOptionsBy(field=field, values=values)


def test_narrowed_choices_do_not_restrict_validation() -> None:
    schema = TaskModelOptionSchema(
        task_type=TASK_TEXT_TO_SPEECH,
        target="openai/tts-1::api-key",
        fields=(_tier_field(TaskModelOptionsBy(field="engine", values={"lite": ("fast",)})),),
    )

    validate_task_model_options(schema, {"tier": "deep"})
    with pytest.raises(TaskModelOptionValidationError):
        validate_task_model_options(schema, {"tier": "turbo"})


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


_OPENAI_VOICES = [
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
]
_KOKORO_VOICES = ("af_alloy", "af_aoede", "af_bella", "af_jessica")


def _choices(field: TaskModelOptionField) -> list[str]:
    return [choice.value for choice in field.options]


@pytest.mark.parametrize(
    ("provider_id", "model", "voice", "formats"),
    [
        # Only a model's published voices are authoritative.
        pytest.param(
            "openrouter",
            _make_model("hexgrad/kokoro-82m", supported_voices=_KOKORO_VOICES),
            ("select", True, None, list(_KOKORO_VOICES)),
            ["mp3", "pcm"],
            id="model-voices",
        ),
        # OpenAI is the only Provider with a built-in voice list.
        pytest.param(
            "openai",
            None,
            ("select", True, "alloy", _OPENAI_VOICES),
            ["mp3", "opus", "aac", "flac", "wav", "pcm"],
            id="openai-fallback",
        ),
        pytest.param(
            "openrouter", None, ("text", False, "", []), ["mp3", "pcm"], id="free-text-voice"
        ),
    ],
)
def test_tts_voice_and_formats_come_from_the_model_or_the_provider(
    provider_id: str,
    model: Model | None,
    voice: tuple[str, bool, str | None, list[str]],
    formats: list[str],
) -> None:
    schema = option_schema_for(
        TASK_TEXT_TO_SPEECH, provider_id, f"{provider_id}/tts::api-key", model=model
    )

    fields = {field.name: field for field in schema.fields}
    assert list(fields) == ["voice", "response_format", "speed", "extra_options"]
    voice_field = fields["voice"]
    assert (
        voice_field.type,
        voice_field.required,
        voice_field.default,
        _choices(voice_field),
    ) == voice
    assert _choices(fields["response_format"]) == formats


def test_tts_instructions_field_only_when_the_model_advertises_it() -> None:
    def names(*supported: str) -> list[str]:
        model = _make_model("openai-tts", supported_parameters=supported)
        schema = option_schema_for(
            TASK_TEXT_TO_SPEECH, "openai", "openai/openai-tts::api-key", model=model
        )
        return [field.name for field in schema.fields]

    assert "instructions" in names("voice", "response_format", "speed", "instructions")
    assert "instructions" not in names("voice", "response_format", "speed")


@pytest.mark.parametrize(
    ("provider_id", "model", "names"),
    [
        pytest.param(
            "openai",
            _make_model("whisper-1", supported_parameters=("response_format",)),
            ["language", "prompt", "temperature", "response_format", "extra_options"],
            id="advertised-response-format",
        ),
        pytest.param(
            "openai",
            _make_model("gpt-4o-transcribe"),
            ["language", "prompt", "temperature", "extra_options"],
            id="unadvertised-response-format",
        ),
        pytest.param(
            "openai", None, ["language", "prompt", "temperature", "extra_options"], id="no-model"
        ),
        # OpenRouter execution sends neither a prompt nor a response format.
        pytest.param(
            "openrouter",
            _make_model("openai/whisper-1", supported_parameters=("response_format",)),
            ["language", "temperature", "extra_options"],
            id="openrouter",
        ),
    ],
)
def test_stt_fields_depend_on_provider_protocol_and_model_support(
    provider_id: str, model: Model | None, names: list[str]
) -> None:
    schema = option_schema_for(
        TASK_SPEECH_TO_TEXT, provider_id, f"{provider_id}/stt::api-key", model=model
    )

    fields = {field.name: field for field in schema.fields}
    assert list(fields) == names
    if "response_format" in fields:
        assert _choices(fields["response_format"]) == [
            "json",
            "text",
            "srt",
            "verbose_json",
            "vtt",
        ]
        assert fields["response_format"].default == "json"


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
    # No hard-coded family fields (``strength``, ``rgb_colors``) appear.
    assert [field.name for field in schema.fields] == ["n", "provider_options", "extra_options"]
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


def test_video_schema_projects_dedicated_catalog_options() -> None:
    model = _make_model(
        "black-forest-labs/flux-3-video",
        task_options={
            "video_generation": {
                "parameters": {
                    "resolution": {"type": "enum", "values": ["720p", "1080p"]},
                    "duration": {"type": "enum", "values": ["5", "10"]},
                    "generate_audio": {"type": "boolean"},
                },
                "passthrough_parameters": ["safety_tolerance"],
            }
        },
    )

    schema = option_schema_for(
        TASK_VIDEO_GENERATION,
        "openrouter",
        "openrouter/black-forest-labs/flux-3-video::api-key",
        model=model,
    )

    assert [field.name for field in schema.fields] == [
        "resolution",
        "duration",
        "generate_audio",
        "provider_options",
        "extra_options",
    ]


def test_music_schema_uses_only_model_supported_sampling_options() -> None:
    model = _make_model(
        "google/lyria-3-pro-preview",
        supported_parameters=("temperature", "seed"),
    )

    schema = option_schema_for(
        TASK_MUSIC_GENERATION,
        "openrouter",
        "openrouter/google/lyria-3-pro-preview::api-key",
        model=model,
    )

    assert [field.name for field in schema.fields] == [
        "temperature",
        "seed",
        "extra_options",
    ]


# ---------------------------------------------------------------------------
# Text embedding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "names"),
    [
        # Before the catalog is loaded the knob is offered.
        pytest.param(None, ["dimensions", "extra_options"], id="unknown-model"),
        # A Provider would reject a knob the model does not list.
        pytest.param(
            _make_model("some/embedding-model-v1"), ["extra_options"], id="unsupported-dimensions"
        ),
    ],
)
def test_embedding_dimensions_field_follows_model_support(
    model: Model | None, names: list[str]
) -> None:
    schema = option_schema_for(
        TASK_TEXT_EMBEDDING, "openrouter", "openrouter/embed::api-key", model=model
    )

    assert [field.name for field in schema.fields] == names


def test_embedding_schema_defaults_send_no_dimensions() -> None:
    target = "openrouter/google/gemini-embedding-2::api-key"
    schema = option_schema_for(TASK_TEXT_EMBEDDING, "openrouter", target)

    # The empty ``dimensions`` default is dropped before the request.
    assert schema.default_options() == {"extra_options": {}}
    rendered = schema.to_dict()
    assert (rendered["task_type"], rendered["target"]) == (TASK_TEXT_EMBEDDING, target)
    dimensions = rendered["fields"][0]
    assert (dimensions["name"], dimensions["type"], dimensions["default"]) == (
        "dimensions",
        "number",
        None,
    )
    assert (dimensions["min"], dimensions["step"]) == (1, 1)
