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
        # "Provider default" selects stay empty.
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
            # A stored option the schema no longer offers never reaches a request.
            TaskModelBinding(
                task_type=TASK_IMAGE_GENERATION,
                target="openai/dall-e-3::api-key",
                options={"response_format": "url"},
            ),
            {"size": "", "extra_options": {}},
            id="empty-defaults-and-inert-stale-option",
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


def test_live_voice_options_offer_who_answers_the_voice_model() -> None:
    service = _live_voice_service()

    schema = service.options(TASK_LIVE_VOICE, "openai/live-sub::subscription")

    fields = {field.name: field for field in schema.fields}
    assert list(fields) == ["voice", "backend", "openai_backend_model"]
    assert [choice.value for choice in fields["backend"].options] == ["vbot", "openai"]
    assert "extra_options" not in fields


def test_live_voice_update_validates_backend_against_the_same_choices() -> None:
    storage = _Storage()
    service = _live_voice_service(storage)
    target = "openai/live-sub::subscription"

    for field_name, value in (("backend", "none"), ("openai_backend_model", "terra")):
        rejected = {"target": target, "options": {field_name: value}}
        with pytest.raises(TaskModelValidationError, match=field_name):
            service.update({TASK_LIVE_VOICE: rejected})
    assert TASK_LIVE_VOICE not in storage.load_model_task_settings()

    saved = service.update({TASK_LIVE_VOICE: {"target": target, "options": {}}})

    assert saved[TASK_LIVE_VOICE] == {"target": target, "options": {}}
    binding = service.binding_for(TASK_LIVE_VOICE)
    assert service.options_with_defaults(binding) == {
        "voice": "juniper",
        "backend": "vbot",
        "openai_backend_model": "luna",
    }


def test_live_voice_patch_sets_and_unsets_the_backend() -> None:
    target = "openai/live-sub::subscription"
    storage = _Storage({TASK_LIVE_VOICE: {"target": target, "options": {"voice": "cove"}}})
    service = _live_voice_service(storage)

    selected = service.patch_options(TASK_LIVE_VOICE, set_values={"backend": "openai"})
    assert selected[TASK_LIVE_VOICE]["options"] == {"voice": "cove", "backend": "openai"}

    with pytest.raises(TaskModelValidationError):
        service.patch_options(TASK_LIVE_VOICE, set_values={"backend": "quiet"})

    cleared = service.patch_options(TASK_LIVE_VOICE, unset_names=("backend",))
    assert cleared[TASK_LIVE_VOICE]["options"] == {"voice": "cove"}
    assert service.options_with_defaults(service.binding_for(TASK_LIVE_VOICE)) == {
        "voice": "cove",
        "backend": "vbot",
        "openai_backend_model": "luna",
    }
