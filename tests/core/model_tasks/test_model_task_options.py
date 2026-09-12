"""Tests for model task options."""

from __future__ import annotations

import pytest

from core.model_tasks import (
    SUPPORTED_TASK_TYPES,
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_TO_SPEECH,
    LocalTaskTargetDescriptor,
    LocalTaskTargetRegistry,
    TaskModelBinding,
    TaskModelOptionField,
    TaskModelService,
    TaskModelValidationError,
    validate_task_type,
)
from core.model_tasks import constants as model_task_constants
from tests.core.model_tasks.model_tasks_test_support import (
    _Credentials,
    _model,
    _Models,
    _provider,
    _Providers,
    _Storage,
)


def test_options_with_defaults_merges_binding_values() -> None:
    service = TaskModelService(_Providers(), _Models([]), _Credentials(), _Storage())
    binding = TaskModelBinding(
        task_type=TASK_TEXT_TO_SPEECH,
        target="openrouter/openai/gpt-4o-mini-tts::api-key",
        options={"voice": "nova"},
    )

    options = service.options_with_defaults(binding)

    assert options["voice"] == "nova"
    assert options["response_format"] == "mp3"
    assert options["speed"] == 1.0


def test_options_for_local_descriptor_surfaces_descriptor_option_fields() -> None:
    """A test-only local descriptor carrying option fields exposes them via
    ``task_model.options``. The descriptor owns the schema — provider option
    code is not involved."""

    local_registry = LocalTaskTargetRegistry(
        [
            LocalTaskTargetDescriptor(
                id="whisper-local",
                label="Local Whisper",
                task_types=(TASK_SPEECH_TO_TEXT,),
                option_fields=(
                    TaskModelOptionField(
                        name="language",
                        type="text",
                        label="Language",
                        default="auto",
                    ),
                    TaskModelOptionField(
                        name="beam_size",
                        type="number",
                        label="Beam size",
                        default=5,
                        min_value=1,
                        max_value=10,
                    ),
                ),
            )
        ]
    )
    service = TaskModelService(
        _Providers(),
        _Models([]),
        _Credentials(),
        _Storage(),
        local_targets=local_registry,
    )

    schema = service.options(TASK_SPEECH_TO_TEXT, "local/whisper-local")

    assert schema.task_type == TASK_SPEECH_TO_TEXT
    assert schema.target == "local/whisper-local"
    assert [field.name for field in schema.fields] == ["language", "beam_size"]
    assert schema.default_options() == {"language": "auto", "beam_size": 5}


def test_options_for_local_descriptor_without_option_fields_returns_empty_schema() -> None:
    """A local descriptor that declares no option fields still produces a
    valid (empty) schema — pre-existing descriptors without options stay
    backward-compatible."""

    local_registry = LocalTaskTargetRegistry(
        [
            LocalTaskTargetDescriptor(
                id="whisper-local",
                label="Local Whisper",
                task_types=(TASK_SPEECH_TO_TEXT,),
            )
        ]
    )
    service = TaskModelService(
        _Providers(),
        _Models([]),
        _Credentials(),
        _Storage(),
        local_targets=local_registry,
    )

    schema = service.options(TASK_SPEECH_TO_TEXT, "local/whisper-local")

    assert schema.task_type == TASK_SPEECH_TO_TEXT
    assert schema.target == "local/whisper-local"
    assert schema.fields == ()
    assert schema.default_options() == {}


def test_options_with_defaults_uses_descriptor_fields_for_local_target() -> None:
    """``options_with_defaults`` merges descriptor-owned defaults for a local
    target, just like it does for provider targets."""

    local_registry = LocalTaskTargetRegistry(
        [
            LocalTaskTargetDescriptor(
                id="whisper-local",
                label="Local Whisper",
                task_types=(TASK_SPEECH_TO_TEXT,),
                option_fields=(
                    TaskModelOptionField(
                        name="language",
                        type="text",
                        label="Language",
                        default="auto",
                    ),
                ),
            )
        ]
    )
    service = TaskModelService(
        _Providers(),
        _Models([]),
        _Credentials(),
        _Storage(),
        local_targets=local_registry,
    )
    binding = TaskModelBinding(
        task_type=TASK_SPEECH_TO_TEXT,
        target="local/whisper-local",
        options={"language": "en"},
    )

    options = service.options_with_defaults(binding)

    # User value wins over descriptor default.
    assert options == {"language": "en"}


def test_options_for_provider_target_unchanged() -> None:
    """The provider branch of ``options()`` still uses the
    task/provider option code in :mod:`core.model_tasks.options` —
    descriptor-owned fields do not leak into provider schemas."""

    service = TaskModelService(_Providers(), _Models([]), _Credentials(), _Storage())

    schema = service.options(
        TASK_SPEECH_TO_TEXT,
        "openrouter/openai/gpt-4o-transcribe::api-key",
    )

    field_names = [field.name for field in schema.fields]
    assert "language" in field_names
    assert "temperature" in field_names


def test_image_edit_vocabulary_removed_from_constants() -> None:
    """``TASK_IMAGE_EDIT`` and the string ``"image_edit"`` are gone from the
    task-model vocabulary. ``SUPPORTED_TASK_TYPES`` and the exported constant
    names no longer mention image-edit."""

    assert not hasattr(model_task_constants, "TASK_IMAGE_EDIT")
    assert "image_edit" not in SUPPORTED_TASK_TYPES
    assert "image_edit" not in set(dir(model_task_constants))


def test_validate_task_type_rejects_image_edit() -> None:
    """``validate_task_type`` rejects ``"image_edit"`` with a clear message —
    removing the constant does not accidentally let the dead vocabulary
    back in via string equality."""

    with pytest.raises(TaskModelValidationError):
        validate_task_type("image_edit")


def test_text_embedding_added_to_supported_task_types() -> None:
    """``text_embedding`` is a first-class binding task type: exported as
    ``TASK_TEXT_EMBEDDING`` on the constants module, present in
    ``SUPPORTED_TASK_TYPES``, and accepted by ``validate_task_type``.
    This is the vocabulary foundation that later phases (discovery,
    ``core/embeddings/``, vector recall) build on."""

    assert model_task_constants.TASK_TEXT_EMBEDDING == "text_embedding"
    assert "text_embedding" in SUPPORTED_TASK_TYPES
    assert validate_task_type("text_embedding") == "text_embedding"


def test_generated_media_tasks_are_supported_binding_types() -> None:
    assert model_task_constants.TASK_VIDEO_GENERATION == "video_generation"
    assert model_task_constants.TASK_MUSIC_GENERATION == "music_generation"
    assert {"video_generation", "music_generation"} <= SUPPORTED_TASK_TYPES
    assert validate_task_type("video_generation") == "video_generation"
    assert validate_task_type("music_generation") == "music_generation"


# ---------------------------------------------------------------------------
# Phase 3 — model-aware option schemas
# ---------------------------------------------------------------------------
def test_options_for_tts_kokoro_uses_model_specific_voice_list() -> None:
    """When the registry has a model with ``supported_voices``, the schema's
    ``voice`` field is a ``select`` with exactly those voices — not the
    OpenAI fallback list. This is the bug the phase fixes: a kokoro target
    should show 54 voices, not 11."""

    kokoro_voices = tuple(f"af_voice_{index}" for index in range(54))
    models = _Models(
        [
            _model(
                "hexgrad/kokoro-82m",
                (TASK_TEXT_TO_SPEECH,),
                name="Kokoro TTS",
                supported_voices=kokoro_voices,
            )
        ]
    )
    service = TaskModelService(_Providers(), models, _Credentials(), _Storage())

    schema = service.options(TASK_TEXT_TO_SPEECH, "openrouter/hexgrad/kokoro-82m::api-key")

    voice_field = schema.fields[0]
    assert voice_field.name == "voice"
    assert voice_field.type == "select"
    assert voice_field.required is True
    assert [choice.value for choice in voice_field.options] == list(kokoro_voices)


def test_options_for_tts_openai_provider_without_model_uses_openai_voice_list() -> None:
    """When the model is missing but the provider is OpenAI, the schema
    keeps the OpenAI canonical voice list as a select — that provider's
    voices are the authoritative fallback."""

    service = TaskModelService(_Providers(), _Models(), _Credentials(), _Storage())

    schema = service.options(TASK_TEXT_TO_SPEECH, "openai/tts-1::api-key")

    voice_field = schema.fields[0]
    assert voice_field.name == "voice"
    assert voice_field.type == "select"
    assert voice_field.required is True
    voice_values = [choice.value for choice in voice_field.options]
    # The full OpenAI TTS voice set is present.
    for known in (
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
    ):
        assert known in voice_values
    # Default is still alloy so existing test data carries over.
    assert voice_field.default == "alloy"


def test_options_for_tts_openrouter_without_model_uses_free_text_voice() -> None:
    """When the model is missing on a non-OpenAI provider, the schema no
    longer silently falls back to the OpenAI voice list — voice becomes
    a free-text field, because the valid voice ids are model-specific
    and we do not know them."""

    service = TaskModelService(_Providers(), _Models(), _Credentials(), _Storage())

    schema = service.options(TASK_TEXT_TO_SPEECH, "openrouter/mistralai/voxtral-tts::api-key")

    voice_field = schema.fields[0]
    assert voice_field.name == "voice"
    assert voice_field.type == "text"
    # No default — the user must provide a model-accepted voice id.
    assert voice_field.default is None or voice_field.default == ""


def test_options_for_tts_response_format_differs_between_openai_and_openrouter() -> None:
    """OpenAI exposes the full response_format set; OpenRouter exposes
    the trimmed mp3/pcm pair. The provider branch decides the set."""

    openai_service = TaskModelService(
        _Providers(providers=[_provider("openai", "OpenAI", [("api-key", "API Key")])]),
        _Models(),
        _Credentials(granted={"openai:api-key"}),
        _Storage(),
    )
    openrouter_service = TaskModelService(_Providers(), _Models(), _Credentials(), _Storage())

    openai_schema = openai_service.options(TASK_TEXT_TO_SPEECH, "openai/tts-1::api-key")
    openrouter_schema = openrouter_service.options(TASK_TEXT_TO_SPEECH, "openrouter/x::api-key")

    openai_formats = {
        choice.value
        for field in openai_schema.fields
        if field.name == "response_format"
        for choice in field.options
    }
    openrouter_formats = {
        choice.value
        for field in openrouter_schema.fields
        if field.name == "response_format"
        for choice in field.options
    }
    assert {"mp3", "opus", "aac", "flac", "wav", "pcm"} <= openai_formats
    assert openrouter_formats == {"mp3", "pcm"}


def test_options_for_image_renders_model_task_options_from_registry() -> None:
    """``TaskModelService.options`` resolves the target's model from the
    registry and hands its typed ``task_options`` parameter schema to the
    builder — the rendered fields mirror the data, not any hardcoded
    family profile."""

    models = _Models(
        [
            _model(
                "recraft/recraft-v3",
                (TASK_IMAGE_GENERATION,),
                name="Recraft v3",
                task_options={
                    "image_generation": {
                        "parameters": {
                            "n": {"type": "range", "min": 1, "max": 6},
                            "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9"]},
                        },
                        "passthrough": {"recraft": ["controls", "style", "text_layout"]},
                    }
                },
            )
        ]
    )
    service = TaskModelService(_Providers(), models, _Credentials(), _Storage())

    schema = service.options(TASK_IMAGE_GENERATION, "openrouter/recraft/recraft-v3::api-key")
    fields_by_name = {field.name: field for field in schema.fields}

    assert fields_by_name["n"].type == "number"
    assert fields_by_name["n"].max_value == 6
    aspect = fields_by_name["aspect_ratio"]
    assert aspect.type == "select"
    assert {choice.value for choice in aspect.options} == {"", "1:1", "16:9"}
    provider_options = fields_by_name["provider_options"]
    assert provider_options.type == "json"
    assert "recraft: controls, style, text_layout" in provider_options.description
    # No hardcoded family fields leak in anymore.
    assert "strength" not in fields_by_name
    assert "rgb_colors" not in fields_by_name


def test_options_for_image_without_task_options_uses_fallback() -> None:
    """A model with no typed parameter schema (unrefreshed catalog) gets the
    conservative aspect-ratio/resolution fallback."""

    models = _Models(
        [_model("black-forest-labs/flux.2-pro", (TASK_IMAGE_GENERATION,), name="Flux 2 Pro")]
    )
    service = TaskModelService(_Providers(), models, _Credentials(), _Storage())

    schema = service.options(
        TASK_IMAGE_GENERATION, "openrouter/black-forest-labs/flux.2-pro::api-key"
    )
    field_names = {field.name for field in schema.fields}

    assert {"aspect_ratio", "resolution", "extra_options"} <= field_names
    assert "provider_options" not in field_names


def test_options_for_image_seed_only_when_supported_by_model() -> None:
    """In the fallback, ``seed`` appears only when the model's flat
    ``supported_parameters`` includes it — the typed schema replaces this
    gate for refreshed models."""

    flux_models = _Models(
        [
            _model(
                "black-forest-labs/flux.2-pro",
                (TASK_IMAGE_GENERATION,),
                supported_parameters=("seed",),
            )
        ]
    )
    flux_service = TaskModelService(_Providers(), flux_models, _Credentials(), _Storage())
    flux_schema = flux_service.options(
        TASK_IMAGE_GENERATION, "openrouter/black-forest-labs/flux.2-pro::api-key"
    )
    flux_names = {field.name for field in flux_schema.fields}
    assert "seed" in flux_names

    recraft_models = _Models(
        [
            _model(
                "recraft/recraft-v3",
                (TASK_IMAGE_GENERATION,),
                supported_parameters=(),
            )
        ]
    )
    recraft_service = TaskModelService(_Providers(), recraft_models, _Credentials(), _Storage())
    recraft_schema = recraft_service.options(
        TASK_IMAGE_GENERATION, "openrouter/recraft/recraft-v3::api-key"
    )
    recraft_names = {field.name for field in recraft_schema.fields}
    assert "seed" not in recraft_names


def test_openrouter_stt_omits_response_format_even_when_advertised() -> None:
    """OpenRouter execution consumes JSON and does not send response_format."""

    models = _Models(
        [
            _model(
                "openai/whisper-1",
                (TASK_SPEECH_TO_TEXT,),
                supported_parameters=("response_format",),
            )
        ]
    )
    service = TaskModelService(_Providers(), models, _Credentials(), _Storage())

    schema = service.options(TASK_SPEECH_TO_TEXT, "openrouter/openai/whisper-1::api-key")
    field_names = {field.name for field in schema.fields}
    assert "response_format" not in field_names


def test_options_for_stt_omits_response_format_when_model_does_not_advertise_it() -> None:
    """When the model is present but ``response_format`` is not in
    ``supported_parameters``, the field is not exposed."""

    models = _Models(
        [
            _model(
                "openai/gpt-4o-transcribe",
                (TASK_SPEECH_TO_TEXT,),
                supported_parameters=(),
            )
        ]
    )
    service = TaskModelService(_Providers(), models, _Credentials(), _Storage())

    schema = service.options(TASK_SPEECH_TO_TEXT, "openrouter/openai/gpt-4o-transcribe::api-key")
    field_names = {field.name for field in schema.fields}
    assert "response_format" not in field_names


def test_options_for_stt_prompt_field_is_provider_based_not_model_based() -> None:
    """``prompt`` is added for non-OpenRouter STT targets regardless of
    whether the model resolves. The provider branch decides — it is a
    protocol capability, not a per-model capability."""

    models = _Models([_model("openai/whisper-1", (TASK_SPEECH_TO_TEXT,))])
    service = TaskModelService(_Providers(), models, _Credentials(), _Storage())

    openai_schema = service.options(TASK_SPEECH_TO_TEXT, "openai/whisper-1::api-key")
    openrouter_schema = service.options(TASK_SPEECH_TO_TEXT, "openrouter/openai/whisper-1::api-key")

    openai_names = {field.name for field in openai_schema.fields}
    openrouter_names = {field.name for field in openrouter_schema.fields}

    assert "prompt" in openai_names
    assert "prompt" not in openrouter_names


def test_options_falls_back_when_model_not_in_registry() -> None:
    """If the registry has no entry for the target's model, ``options()``
    falls back to the provider-level conservative schema (the same
    shape that existed before model-awareness). The error does not
    surface — missing catalog entries are an expected transient state
    until the next catalog refresh."""

    providers = _Providers(providers=[_provider("openai", "OpenAI", [("api-key", "API Key")])])
    models = _Models()  # No models loaded.
    service = TaskModelService(
        providers,
        models,
        _Credentials(granted={"openai:api-key"}),
        _Storage(),
    )

    # STT path: still works, prompt is present (non-OpenRouter is wrong
    # here — openai is the provider, so prompt is included).
    stt_schema = service.options(TASK_SPEECH_TO_TEXT, "openai/whisper-1::api-key")
    stt_names = {field.name for field in stt_schema.fields}
    assert {"language", "temperature", "prompt"} <= stt_names

    # TTS path: still falls back to the OpenAI voice select.
    tts_schema = service.options(TASK_TEXT_TO_SPEECH, "openai/tts-1::api-key")
    voice_field = tts_schema.fields[0]
    assert voice_field.name == "voice"
    assert voice_field.type == "select"
    assert voice_field.default == "alloy"


def test_options_with_defaults_uses_model_specific_voice_default() -> None:
    """``options_with_defaults`` merges the model-aware defaults; the
    ``voice`` field gets the schema's default (no default for free-text
    voice on non-OpenAI providers) and binding overrides win."""

    providers = _Providers(providers=[_provider("openai", "OpenAI", [("api-key", "API Key")])])
    models = _Models(
        [
            _model(
                "openai/tts-1",
                (TASK_TEXT_TO_SPEECH,),
                name="OpenAI TTS-1",
            )
        ]
    )
    service = TaskModelService(
        providers,
        models,
        _Credentials(granted={"openai:api-key"}),
        _Storage(),
    )
    binding = TaskModelBinding(
        task_type=TASK_TEXT_TO_SPEECH,
        target="openai/tts-1::api-key",
        options={"voice": "echo"},
    )

    options = service.options_with_defaults(binding)

    # User value wins over OpenAI's "alloy" default.
    assert options["voice"] == "echo"
    # Unrelated defaults still flow through.
    assert options["response_format"] == "mp3"
    assert options["speed"] == 1.0


def test_options_with_defaults_carries_forced_and_escape_hatch_defaults() -> None:
    """``options_with_defaults`` surfaces the schema defaults execution
    domains rely on: a forced enum default (dall-e response_format →
    b64_json) and the empty escape-hatch object, while unpinned
    "Provider default" selects contribute empty strings the wire layer
    drops."""

    models = _Models(
        [
            _model(
                "dall-e-3",
                (TASK_IMAGE_GENERATION,),
                provider_id="openai",
                task_options={
                    "image_generation": {
                        "parameters": {
                            "size": {
                                "type": "enum",
                                "values": ["1024x1024", "1792x1024", "1024x1792"],
                            },
                            "response_format": {"type": "enum", "values": ["b64_json", "url"]},
                        }
                    }
                },
            )
        ]
    )
    service = TaskModelService(_Providers(), models, _Credentials(), _Storage())
    binding = TaskModelBinding(
        task_type=TASK_IMAGE_GENERATION,
        target="openai/dall-e-3::api-key",
        options={},
    )

    options = service.options_with_defaults(binding)

    assert options["response_format"] == "b64_json"
    assert options["size"] == ""
    assert options["extra_options"] == {}
