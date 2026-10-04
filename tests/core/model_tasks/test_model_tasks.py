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
    task_model_targets_equal,
)
from tests.core.model_tasks.model_tasks_test_support import (
    _Credentials,
    _model,
    _Models,
    _Providers,
    _Storage,
)


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


@pytest.mark.parametrize("operation", ["update", "options"])
def test_unknown_local_target_is_a_binding_validation_error(operation: str) -> None:
    service = TaskModelService(_Providers(), _Models([]), _Credentials(), _Storage())

    with pytest.raises(TaskModelValidationError):
        if operation == "update":
            service.update({TASK_SPEECH_TO_TEXT: {"target": "local/missing"}})
        else:
            service.options(TASK_SPEECH_TO_TEXT, "local/missing")


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param(
            "openrouter/openai/gpt-4o-transcribe::api-key",
            ("openrouter", "openai/gpt-4o-transcribe", "openrouter:api-key", "api-key", ""),
            id="nested-model-id",
        ),
        pytest.param(
            "openrouter/openai/gpt-4o-transcribe::api-key:work",
            (
                "openrouter",
                "openai/gpt-4o-transcribe",
                "openrouter:api-key:work",
                "api-key",
                "work",
            ),
            id="account-suffix",
        ),
        pytest.param(
            "openai/gpt-4o::openai:api-key:work",
            ("openai", "gpt-4o", "openai:api-key:work", "api-key", "work"),
            id="provider-prefixed-connection",
        ),
    ],
)
def test_provider_target_names_provider_model_connection_and_account(
    target: str, expected: tuple[str, str, str, str, str]
) -> None:
    ref = parse_task_model_target_id(target)

    assert (
        ref.provider_id,
        ref.model_id,
        ref.connection_id,
        ref.local_connection_id,
        ref.account_id,
    ) == expected


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("openrouter/openai/gpt-4o-transcribe", id="no-connection"),
        pytest.param(
            "openrouter/openai/gpt-4o-transcribe::api-key:Work-Acct", id="invalid-account"
        ),
        pytest.param(
            "openrouter/openai/gpt-4o-transcribe::openrouter::work",
            id="empty-connection-before-account",
        ),
    ],
)
def test_malformed_provider_target_is_a_validation_error(target: str) -> None:
    with pytest.raises(TaskModelValidationError):
        parse_task_model_target_id(target)


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        ("local/engine", " local/engine ", True),
        ("local/engine", "local/other", False),
        ("openai/model::api-key", "openai/model::openai:api-key", True),
        ("openai/model::api-key:work", "openai/model::api-key:home", False),
        ("openai/model::api-key:work", "openai/model::api-key", False),
        ("openai/model::api-key", "other/model::api-key", False),
        ("malformed", "malformed", True),
        ("malformed", "other", False),
    ],
)
def test_task_target_identity_preserves_local_provider_and_account_boundaries(
    left: str, right: str, equal: bool
) -> None:
    assert task_model_targets_equal(left, right) is equal


@pytest.mark.parametrize("dimensions", [0, 256.0, True])
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
    # Setting a sibling also drops it, so it never blocks an edit.
    storage.update_model_task_settings(
        {
            TASK_TEXT_TO_SPEECH: {
                "target": target,
                "options": {"voice": "de-de-klaus:mai-voice-2", "retired_option": True},
            }
        }
    )
    saved = service.patch_options(TASK_TEXT_TO_SPEECH, set_values={"speed": 1.1})
    assert saved[TASK_TEXT_TO_SPEECH]["options"] == {
        "voice": "de-de-klaus:mai-voice-2",
        "speed": 1.1,
    }


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


@pytest.mark.parametrize(
    ("stale_options", "saved_after_edit"),
    [
        # An option the schema no longer offers is dropped with the next edit.
        ({"retired_option": True}, {"voice": "available", "speed": 1.1}),
        # A value the schema no longer accepts still needs a repair.
        ({"voice": "removed"}, None),
    ],
)
def test_unchanged_stale_binding_does_not_block_other_updates(
    stale_options: dict, saved_after_edit: dict | None
) -> None:
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
    sibling_edit = {
        TASK_TEXT_TO_SPEECH: {"options": {"voice": "available", **stale_options, "speed": 1.1}}
    }
    if saved_after_edit is not None:
        saved = service.update(sibling_edit)
        assert saved[TASK_TEXT_TO_SPEECH]["options"] == saved_after_edit
    else:
        with pytest.raises(TaskModelValidationError):
            service.update(sibling_edit)
    repaired = service.update({TASK_TEXT_TO_SPEECH: {"options": {"voice": "available"}}})
    assert repaired[TASK_TEXT_TO_SPEECH]["target"] == stale["target"]
    assert service.binding_is_usable(TASK_TEXT_TO_SPEECH)
