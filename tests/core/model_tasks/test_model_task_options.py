"""Tests for model task options."""

from __future__ import annotations

from typing import Any

import pytest

from core.model_tasks import (
    SUPPORTED_TASK_TYPES,
    TASK_IMAGE_GENERATION,
    TASK_IMAGE_UNDERSTANDING,
    TASK_LIVE_VOICE,
    TASK_MUSIC_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_EMBEDDING,
    TASK_TEXT_TO_SPEECH,
    TASK_VIDEO_GENERATION,
    LocalTaskTargetDescriptor,
    LocalTaskTargetRegistry,
    TaskModelBinding,
    TaskModelOptionField,
    TaskModelOptionSchema,
    TaskModelService,
    TaskModelValidationError,
    validate_task_type,
)
from tests.core.model_tasks.model_tasks_test_support import (
    _Credentials,
    _live_voice_registry,
    _model,
    _Models,
    _provider,
    _Providers,
    _Storage,
)


@pytest.mark.parametrize(
    ("task_type", "value"),
    [
        (TASK_IMAGE_UNDERSTANDING, "image_understanding"),
        (TASK_TEXT_EMBEDDING, "text_embedding"),
        (TASK_VIDEO_GENERATION, "video_generation"),
        (TASK_MUSIC_GENERATION, "music_generation"),
        (TASK_LIVE_VOICE, "live_voice"),
    ],
)
def test_binding_task_types_keep_their_persisted_names(task_type: str, value: str) -> None:
    assert task_type == value
    assert value in SUPPORTED_TASK_TYPES
    assert validate_task_type(value) == value


def test_removed_image_edit_task_type_is_rejected() -> None:
    assert "image_edit" not in SUPPORTED_TASK_TYPES
    with pytest.raises(TaskModelValidationError):
        validate_task_type("image_edit")


def test_local_target_schema_comes_from_its_descriptor() -> None:
    """A local engine declares its own fields; Provider option code is not involved."""

    local_registry = LocalTaskTargetRegistry(
        [
            LocalTaskTargetDescriptor(
                id="whisper-local",
                label="Local Whisper",
                task_types=(TASK_SPEECH_TO_TEXT,),
                option_fields=(
                    TaskModelOptionField(
                        name="language", type="text", label="Language", default="auto"
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
            ),
            LocalTaskTargetDescriptor(
                id="bare-local", label="Bare", task_types=(TASK_SPEECH_TO_TEXT,)
            ),
        ]
    )
    service = TaskModelService(
        _Providers(), _Models([]), _Credentials(), _Storage(), local_targets=local_registry
    )

    schema = service.options(TASK_SPEECH_TO_TEXT, "local/whisper-local")
    bare = service.options(TASK_SPEECH_TO_TEXT, "local/bare-local")

    assert (schema.task_type, schema.target) == (TASK_SPEECH_TO_TEXT, "local/whisper-local")
    assert schema.default_options() == {"language": "auto", "beam_size": 5}
    assert (bare.fields, bare.default_options()) == ((), {})
    binding = TaskModelBinding(
        task_type=TASK_SPEECH_TO_TEXT, target="local/whisper-local", options={"language": "en"}
    )
    assert service.options_with_defaults(binding) == {"language": "en", "beam_size": 5}


@pytest.mark.parametrize(
    ("models", "target", "voice_default", "voices"),
    [
        pytest.param(
            [
                _model(
                    "hexgrad/kokoro-82m",
                    (TASK_TEXT_TO_SPEECH,),
                    supported_voices=("af_heart", "af_bella"),
                )
            ],
            "openrouter/hexgrad/kokoro-82m::api-key",
            None,
            ["af_heart", "af_bella"],
            id="registry-model",
        ),
        # A missing catalog entry is transient; the Provider fallback applies.
        pytest.param([], "openai/tts-1::api-key", "alloy", None, id="model-not-in-registry"),
    ],
)
def test_provider_target_schema_uses_the_registry_model_or_the_provider_fallback(
    models: list[Any], target: str, voice_default: str | None, voices: list[str] | None
) -> None:
    service = TaskModelService(_Providers(), _Models(models), _Credentials(), _Storage())

    voice = service.options(TASK_TEXT_TO_SPEECH, target).fields[0]

    assert (voice.name, voice.type, voice.default) == ("voice", "select", voice_default)
    if voices is not None:
        assert [choice.value for choice in voice.options] == voices


@pytest.mark.parametrize(
    ("models", "binding", "expected"),
    [
        pytest.param(
            [],
            TaskModelBinding(
                task_type=TASK_TEXT_TO_SPEECH,
                target="openrouter/openai/gpt-4o-mini-tts::api-key",
                options={"voice": "nova"},
            ),
            {"voice": "nova", "response_format": "mp3", "speed": 1.0, "extra_options": {}},
            id="binding-values-win",
        ),
        # A forced enum default survives; "Provider default" selects stay empty.
        pytest.param(
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
                                "response_format": {
                                    "type": "enum",
                                    "values": ["b64_json", "url"],
                                },
                            }
                        }
                    },
                )
            ],
            TaskModelBinding(
                task_type=TASK_IMAGE_GENERATION, target="openai/dall-e-3::api-key", options={}
            ),
            {"size": "", "response_format": "b64_json", "extra_options": {}},
            id="forced-and-empty-defaults",
        ),
    ],
)
def test_options_with_defaults_merges_binding_values_over_schema_defaults(
    models: list[Any], binding: TaskModelBinding, expected: dict[str, Any]
) -> None:
    service = TaskModelService(_Providers(), _Models(models), _Credentials(), _Storage())

    assert service.options_with_defaults(binding) == expected


# ---------------------------------------------------------------------------
# Live voice — registry-aware backend Model choices
# ---------------------------------------------------------------------------
def _live_voice_service(storage: _Storage | None = None) -> TaskModelService:
    return TaskModelService(
        _Providers(
            providers=[
                _provider(
                    "openai",
                    "OpenAI",
                    [("api-key", "API Key"), ("subscription", "ChatGPT Plus/Pro")],
                )
            ]
        ),
        _live_voice_registry(),
        _Credentials({"openai:api-key", "openai:subscription"}),
        storage or _Storage(),
    )


def test_live_voice_options_offer_backend_models_of_the_target_connection() -> None:
    service = _live_voice_service()

    subscription = service.options(TASK_LIVE_VOICE, "openai/live-sub::subscription")
    api_key = service.options(TASK_LIVE_VOICE, "openai/live-key::api-key")

    def backend_values(schema: TaskModelOptionSchema) -> list[str]:
        fields = {field.name: field for field in schema.fields}
        return [choice.value for choice in fields["backend_model"].options]

    assert backend_values(subscription) == ["astra", "terra"]
    assert backend_values(api_key) == ["platform", "terra"]
    assert "extra_options" not in {field.name for field in subscription.fields}


def test_live_voice_update_validates_backend_against_the_same_choices() -> None:
    storage = _Storage()
    service = _live_voice_service(storage)
    target = "openai/live-sub::subscription"

    for field_name, value in (("backend_model", "platform"), ("backend_thinking_effort", "turbo")):
        rejected = {"target": target, "options": {field_name: value}}
        with pytest.raises(TaskModelValidationError, match=field_name):
            service.update({TASK_LIVE_VOICE: rejected})
    assert TASK_LIVE_VOICE not in storage.load_model_task_settings()

    saved = service.update({TASK_LIVE_VOICE: {"target": target, "options": {}}})

    assert saved[TASK_LIVE_VOICE] == {"target": target, "options": {}}
    binding = service.binding_for(TASK_LIVE_VOICE)
    assert service.options_with_defaults(binding) == {
        "voice": "juniper",
        "backend_model": "terra",
        "backend_thinking_effort": "low",
    }


def test_live_voice_keeps_an_explicit_model_default_reasoning_effort() -> None:
    target = "openai/live-sub::subscription"
    storage = _Storage()
    service = _live_voice_service(storage)

    service.update(
        {TASK_LIVE_VOICE: {"target": target, "options": {"backend_thinking_effort": ""}}}
    )

    binding = service.binding_for(TASK_LIVE_VOICE)
    assert service.options_with_defaults(binding)["backend_thinking_effort"] == ""


def test_live_voice_patch_sets_and_unsets_backend_model() -> None:
    target = "openai/live-sub::subscription"
    storage = _Storage({TASK_LIVE_VOICE: {"target": target, "options": {"voice": "cove"}}})
    service = _live_voice_service(storage)

    selected = service.patch_options(TASK_LIVE_VOICE, set_values={"backend_model": "astra"})
    assert selected[TASK_LIVE_VOICE]["options"] == {"voice": "cove", "backend_model": "astra"}

    with pytest.raises(TaskModelValidationError):
        service.patch_options(TASK_LIVE_VOICE, set_values={"backend_model": "quiet"})

    cleared = service.patch_options(TASK_LIVE_VOICE, unset_names=("backend_model",))
    assert cleared[TASK_LIVE_VOICE]["options"] == {"voice": "cove"}
    assert service.options_with_defaults(service.binding_for(TASK_LIVE_VOICE)) == {
        "voice": "cove",
        "backend_model": "terra",
        "backend_thinking_effort": "low",
    }
