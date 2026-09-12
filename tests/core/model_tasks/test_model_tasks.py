"""Tests for model tasks."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from core.model_tasks import (
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_EMBEDDING,
    TASK_TEXT_TO_SPEECH,
    LocalTaskTargetDescriptor,
    LocalTaskTargetRegistry,
    TaskModelOptionField,
    TaskModelService,
    TaskModelValidationError,
    parse_task_model_target_id,
)
from tests.core.model_tasks.model_tasks_test_support import (
    _Credentials,
    _model,
    _Models,
    _Providers,
    _Storage,
)


def test_parse_openrouter_target_with_nested_model_id() -> None:
    ref = parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe::api-key")

    assert ref.provider_id == "openrouter"
    assert ref.model_id == "openai/gpt-4o-transcribe"
    assert ref.connection_id == "openrouter:api-key"
    assert ref.local_connection_id == "api-key"


def test_local_speech_binding_uses_live_engine_availability_and_options() -> None:
    available = False
    registry = LocalTaskTargetRegistry(
        [
            LocalTaskTargetDescriptor(
                id="engine",
                label="Engine",
                task_types=(TASK_SPEECH_TO_TEXT,),
                availability=lambda: available,
                option_fields=(TaskModelOptionField("language", "text", "Language", default=""),),
            )
        ]
    )
    service = TaskModelService(
        _Providers(), _Models([]), _Credentials(), _Storage(), local_targets=registry
    )
    service.update({TASK_SPEECH_TO_TEXT: {"target": "local/engine", "options": {"language": "de"}}})
    assert not service.binding_is_usable(TASK_SPEECH_TO_TEXT)
    available = True
    assert service.binding_is_usable(TASK_SPEECH_TO_TEXT)
    assert service.list_targets(TASK_SPEECH_TO_TEXT)[0].usable
    with pytest.raises(TaskModelValidationError):
        service.update(
            {TASK_SPEECH_TO_TEXT: {"target": "local/engine", "options": {"unknown": True}}}
        )
    service.update({TASK_SPEECH_TO_TEXT: {"target": ""}})
    assert not service.binding_is_usable(TASK_SPEECH_TO_TEXT)


def test_parse_provider_target_requires_connection_suffix() -> None:
    with pytest.raises(TaskModelValidationError):
        parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe")


def test_parse_target_with_account_suffix() -> None:
    ref = parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe::api-key:work")

    assert ref.provider_id == "openrouter"
    assert ref.model_id == "openai/gpt-4o-transcribe"
    assert ref.connection_id == "openrouter:api-key:work"
    assert ref.local_connection_id == "api-key"
    assert ref.account_id == "work"


def test_parse_target_with_provider_prefixed_connection_and_account() -> None:
    ref = parse_task_model_target_id("openai/gpt-4o::openai:api-key:work")

    assert ref.connection_id == "openai:api-key:work"
    assert ref.local_connection_id == "api-key"
    assert ref.account_id == "work"


def test_parse_target_without_account_leaves_account_empty() -> None:
    ref = parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe::api-key")

    assert ref.account_id == ""
    assert ref.connection_id == "openrouter:api-key"


def test_parse_target_rejects_invalid_account_id() -> None:
    with pytest.raises(TaskModelValidationError):
        parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe::api-key:Work-Acct")


def test_parse_target_rejects_empty_connection_before_account() -> None:
    with pytest.raises(TaskModelValidationError):
        parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe::openrouter::work")


@pytest.mark.parametrize("dimensions", [0, -1, 256.0, True, "256"])
def test_update_rejects_invalid_embedding_dimensions(dimensions: object) -> None:
    service = TaskModelService(_Providers(), _Models([]), _Credentials(), _Storage())

    with pytest.raises(TaskModelValidationError):
        service.update(
            {
                TASK_TEXT_EMBEDDING: {
                    "target": "openrouter/google/gemini-embedding-2::api-key",
                    "options": {"dimensions": dimensions},
                }
            }
        )


def test_update_rejects_reserved_embedding_extra_options() -> None:
    service = TaskModelService(_Providers(), _Models([]), _Credentials(), _Storage())

    with pytest.raises(TaskModelValidationError):
        service.update(
            {
                TASK_TEXT_EMBEDDING: {
                    "target": "openrouter/google/gemini-embedding-2::api-key",
                    "options": {"extra_options": {"model": "other", "input": "wrong"}},
                }
            }
        )


def test_patch_options_preserves_siblings_and_validates_tts_voice() -> None:
    target = "openrouter/microsoft/mai-voice-2::api-key"
    storage = _Storage(
        {
            TASK_TEXT_TO_SPEECH: {
                "target": target,
                "options": {"voice": "de-de-klaus:mai-voice-2", "speed": 1.25},
            }
        }
    )
    service = TaskModelService(
        _Providers(),
        _Models(
            [
                _model(
                    "microsoft/mai-voice-2",
                    (TASK_TEXT_TO_SPEECH,),
                    supported_voices=(
                        "de-de-klaus:mai-voice-2",
                        "en-us-harper:mai-voice-2",
                        "es-mx-valeria:mai-voice-2",
                        "fr-fr-soleil:mai-voice-2",
                    ),
                )
            ]
        ),
        _Credentials(),
        storage,
    )

    saved = service.patch_options(
        TASK_TEXT_TO_SPEECH,
        set_values={"voice": "en-us-harper:mai-voice-2"},
    )

    assert saved[TASK_TEXT_TO_SPEECH]["options"] == {
        "voice": "en-us-harper:mai-voice-2",
        "speed": 1.25,
    }


def test_patch_options_rejects_unsupported_tts_voice_before_persistence() -> None:
    target = "openrouter/microsoft/mai-voice-2::api-key"
    storage = _Storage(
        {
            TASK_TEXT_TO_SPEECH: {
                "target": target,
                "options": {"voice": "de-de-klaus:mai-voice-2"},
            }
        }
    )
    service = TaskModelService(
        _Providers(),
        _Models(
            [
                _model(
                    "microsoft/mai-voice-2",
                    (TASK_TEXT_TO_SPEECH,),
                    supported_voices=(
                        "de-de-klaus:mai-voice-2",
                        "en-us-harper:mai-voice-2",
                        "es-mx-valeria:mai-voice-2",
                        "fr-fr-soleil:mai-voice-2",
                    ),
                )
            ]
        ),
        _Credentials(),
        storage,
    )

    with pytest.raises(TaskModelValidationError, match="en-us-harper:mai-voice-2"):
        service.patch_options(TASK_TEXT_TO_SPEECH, set_values={"voice": "Mia"})

    persisted = storage.load_model_task_settings()[TASK_TEXT_TO_SPEECH]
    assert isinstance(persisted, Mapping)
    assert persisted["options"] == {"voice": "de-de-klaus:mai-voice-2"}


def test_patch_options_can_remove_a_stale_option_no_longer_in_schema() -> None:
    target = "openrouter/microsoft/mai-voice-2::api-key"
    storage = _Storage(
        {
            TASK_TEXT_TO_SPEECH: {
                "target": target,
                "options": {
                    "voice": "de-de-klaus:mai-voice-2",
                    "retired_option": True,
                },
            }
        }
    )
    service = TaskModelService(
        _Providers(),
        _Models(
            [
                _model(
                    "microsoft/mai-voice-2",
                    (TASK_TEXT_TO_SPEECH,),
                    supported_voices=("de-de-klaus:mai-voice-2",),
                )
            ]
        ),
        _Credentials(),
        storage,
    )

    saved = service.patch_options(TASK_TEXT_TO_SPEECH, unset_names=("retired_option",))

    assert saved[TASK_TEXT_TO_SPEECH]["options"] == {"voice": "de-de-klaus:mai-voice-2"}


def test_update_rejects_unknown_option_name() -> None:
    target = "openrouter/microsoft/mai-voice-2::api-key"
    service = TaskModelService(
        _Providers(),
        _Models(
            [
                _model(
                    "microsoft/mai-voice-2",
                    (TASK_TEXT_TO_SPEECH,),
                    supported_voices=("de-de-klaus:mai-voice-2",),
                )
            ]
        ),
        _Credentials(),
        _Storage(),
    )

    with pytest.raises(TaskModelValidationError, match="fake"):
        service.update(
            {
                TASK_TEXT_TO_SPEECH: {
                    "target": target,
                    "options": {"voice": "de-de-klaus:mai-voice-2", "fake": True},
                }
            }
        )


@pytest.mark.parametrize("stale_options", [{"retired_option": True}, {"voice": "removed"}])
def test_unchanged_stale_binding_does_not_block_other_updates(stale_options: dict) -> None:
    stale = {"target": "openrouter/voice::api-key", "options": stale_options}
    storage = _Storage({TASK_TEXT_TO_SPEECH: stale})
    service = TaskModelService(
        _Providers(),
        _Models(
            [
                _model("voice", (TASK_TEXT_TO_SPEECH,), supported_voices=("available",)),
                _model("transcribe", (TASK_SPEECH_TO_TEXT,)),
            ]
        ),
        _Credentials(),
        storage,
    )
    update = {
        TASK_TEXT_TO_SPEECH: stale,
        TASK_SPEECH_TO_TEXT: {"target": "openrouter/transcribe::api-key", "options": {}},
    }
    service.validate_update(update)
    saved = service.update(update)
    assert saved[TASK_TEXT_TO_SPEECH] == stale
    assert saved[TASK_SPEECH_TO_TEXT] == update[TASK_SPEECH_TO_TEXT]
    with pytest.raises(TaskModelValidationError):
        service.validate_binding(TASK_TEXT_TO_SPEECH, stale)
    with pytest.raises(TaskModelValidationError):
        service.update({TASK_TEXT_TO_SPEECH: {"options": {**stale_options, "speed": 1.1}}})
    repaired = service.update({TASK_TEXT_TO_SPEECH: {"options": {"voice": "available"}}})
    assert repaired[TASK_TEXT_TO_SPEECH]["target"] == stale["target"]
    assert service.binding_is_usable(TASK_TEXT_TO_SPEECH)
