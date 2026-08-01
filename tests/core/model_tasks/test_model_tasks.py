"""Tests for task-model binding and target discovery."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import cast

import pytest

from core.model_tasks import (
    SUPPORTED_TASK_TYPES,
    TASK_IMAGE_GENERATION,
    TASK_IMAGE_UNDERSTANDING,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_EMBEDDING,
    TASK_TEXT_TO_SPEECH,
    LocalTaskTargetDescriptor,
    LocalTaskTargetRegistry,
    TaskModelBinding,
    TaskModelOptionField,
    TaskModelService,
    TaskModelValidationError,
    parse_task_model_target_id,
    validate_task_type,
)
from core.model_tasks import constants as model_task_constants
from core.models import Model, ModelQuery


def test_parse_openrouter_target_with_nested_model_id() -> None:
    ref = parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe::api-key")

    assert ref.provider_id == "openrouter"
    assert ref.model_id == "openai/gpt-4o-transcribe"
    assert ref.connection_id == "openrouter:api-key"
    assert ref.local_connection_id == "api-key"


def test_parse_provider_target_requires_connection_suffix() -> None:
    with pytest.raises(TaskModelValidationError, match="connection suffix"):
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
    with pytest.raises(TaskModelValidationError, match="account id"):
        parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe::api-key:Work-Acct")


def test_parse_target_rejects_empty_connection_before_account() -> None:
    with pytest.raises(TaskModelValidationError, match="Invalid provider task model target"):
        parse_task_model_target_id("openrouter/openai/gpt-4o-transcribe::openrouter::work")


@pytest.mark.parametrize("dimensions", [0, -1, 256.0, True, "256"])
def test_update_rejects_invalid_embedding_dimensions(dimensions: object) -> None:
    service = TaskModelService(_Providers(), _Models([]), _Credentials(), _Storage())

    with pytest.raises(TaskModelValidationError, match="positive integer or null"):
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

    with pytest.raises(TaskModelValidationError, match="reserved fields: input, model"):
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

    with pytest.raises(TaskModelValidationError, match="not supported: fake"):
        service.update(
            {
                TASK_TEXT_TO_SPEECH: {
                    "target": target,
                    "options": {"voice": "de-de-klaus:mai-voice-2", "fake": True},
                }
            }
        )


def test_list_targets_filters_by_task_type_and_credentials() -> None:
    providers = _Providers()
    models = _Models(
        [
            _model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,)),
            _model("openai/gpt-4o-mini-tts", (TASK_TEXT_TO_SPEECH,)),
        ]
    )
    service = TaskModelService(providers, models, _Credentials(), _Storage())

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    assert [target.id for target in targets] == ["openrouter/openai/gpt-4o-transcribe::api-key"]
    assert targets[0].connection_id == "openrouter:api-key"
    assert targets[0].label == "OpenRouter / OpenAI GPT-4o Transcribe"


def test_list_targets_for_tts_returns_only_tts_models() -> None:
    providers = _Providers()
    models = _Models(
        [
            _model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,)),
            _model("openai/gpt-4o-mini-tts", (TASK_TEXT_TO_SPEECH,)),
        ]
    )
    service = TaskModelService(providers, models, _Credentials(), _Storage())

    targets = service.list_targets(TASK_TEXT_TO_SPEECH)

    assert [target.id for target in targets] == ["openrouter/openai/gpt-4o-mini-tts::api-key"]
    assert targets[0].label == "OpenRouter / OpenAI GPT-4o Mini TTS"


def test_list_targets_for_image_generation() -> None:
    providers = _Providers()
    models = _Models(
        [
            _model("dall-e-3", (TASK_IMAGE_GENERATION,), name="DALL-E 3"),
            _model("gpt-image-1", (TASK_IMAGE_GENERATION,), name="GPT Image 1"),
        ]
    )
    service = TaskModelService(providers, models, _Credentials(), _Storage())

    targets = service.list_targets(TASK_IMAGE_GENERATION)

    assert [target.id for target in targets] == [
        "openrouter/dall-e-3::api-key",
        "openrouter/gpt-image-1::api-key",
    ]


def test_list_targets_for_image_understanding_filters_by_capability() -> None:
    providers = _Providers([_provider("opencode-go", "OpenCode Go", [("api-key", "API Key")])])
    models = _Models(
        [
            _model(
                "kimi-k2.5",
                ("chat", "text_output"),
                name="Kimi K2.5",
                provider_id="opencode-go",
                connections=("api-key",),
                input_modalities=("text", "image"),
            ),
            _model(
                "image-only-input",
                ("text_output",),
                name="Image-only Input",
                provider_id="opencode-go",
                connections=("api-key",),
                input_modalities=("image",),
            ),
            _model(
                "text-model",
                ("chat", "text_output"),
                name="Text Model",
                provider_id="opencode-go",
                connections=("api-key",),
            ),
        ]
    )
    service = TaskModelService(
        providers,
        models,
        _Credentials({"opencode-go:api-key"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_IMAGE_UNDERSTANDING)

    assert [target.id for target in targets] == ["opencode-go/kimi-k2.5::api-key"]
    assert TASK_IMAGE_UNDERSTANDING in targets[0].task_types


def test_image_understanding_is_a_supported_task_type() -> None:
    assert TASK_IMAGE_UNDERSTANDING == "image_understanding"
    assert TASK_IMAGE_UNDERSTANDING in SUPPORTED_TASK_TYPES
    assert validate_task_type(TASK_IMAGE_UNDERSTANDING) == TASK_IMAGE_UNDERSTANDING


def test_binding_is_usable_validates_live_image_understanding_target() -> None:
    target = "openrouter/vision-model::api-key"
    model = _model(
        "vision-model",
        ("chat", "text_output"),
        name="Vision Model",
        input_modalities=("text", "image"),
    )
    settings = {TASK_IMAGE_UNDERSTANDING: {"target": target, "options": {}}}

    usable = TaskModelService(
        _Providers(),
        _Models([model]),
        _Credentials(),
        _Storage(settings),
    )
    stale = TaskModelService(
        _Providers(),
        _Models([]),
        _Credentials(),
        _Storage(settings),
    )
    uncredentialed = TaskModelService(
        _Providers(),
        _Models([model]),
        _Credentials(granted=set()),
        _Storage(settings),
    )
    missing = TaskModelService(
        _Providers(),
        _Models([model]),
        _Credentials(),
        _Storage(),
    )

    assert usable.binding_is_usable(TASK_IMAGE_UNDERSTANDING) is True
    assert stale.binding_is_usable(TASK_IMAGE_UNDERSTANDING) is False
    assert uncredentialed.binding_is_usable(TASK_IMAGE_UNDERSTANDING) is False
    assert missing.binding_is_usable(TASK_IMAGE_UNDERSTANDING) is False


def test_binding_is_usable_rejects_forbidden_connection_and_local_target() -> None:
    forbidden_model = _model(
        "vision-model",
        (TASK_IMAGE_UNDERSTANDING,),
        name="Vision Model",
        connections=("oauth",),
        input_modalities=("text", "image"),
    )
    provider_settings = {
        TASK_IMAGE_UNDERSTANDING: {
            "target": "openrouter/vision-model::api-key",
            "options": {},
        }
    }
    local_settings = {
        TASK_IMAGE_UNDERSTANDING: {
            "target": "local/vision",
            "options": {},
        }
    }

    assert (
        TaskModelService(
            _Providers(),
            _Models([forbidden_model]),
            _Credentials(),
            _Storage(provider_settings),
        ).binding_is_usable(TASK_IMAGE_UNDERSTANDING)
        is False
    )
    assert (
        TaskModelService(
            _Providers(),
            _Models([forbidden_model]),
            _Credentials(),
            _Storage(local_settings),
        ).binding_is_usable(TASK_IMAGE_UNDERSTANDING)
        is False
    )


def test_list_targets_expands_multiple_usable_connections() -> None:
    providers = _Providers(
        providers=[
            _provider(
                "openrouter",
                "OpenRouter",
                [("api-key", "API Key"), ("oauth", "OAuth")],
            )
        ]
    )
    models = _Models([_model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,))])
    service = TaskModelService(
        providers,
        models,
        _Credentials(granted={"openrouter:api-key", "openrouter:oauth"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    # Multi-connection expansion: one target per usable connection, sorted
    # by (kind, label.lower(), id) — alphabetical on label puts "API Key" before "OAuth".
    assert [target.id for target in targets] == [
        "openrouter/openai/gpt-4o-transcribe::api-key",
        "openrouter/openai/gpt-4o-transcribe::oauth",
    ]
    assert [target.label for target in targets] == [
        "OpenRouter / OpenAI GPT-4o Transcribe (API Key)",
        "OpenRouter / OpenAI GPT-4o Transcribe (OAuth)",
    ]


def test_list_targets_respects_per_model_connections_allowlist() -> None:
    """A provider with two usable connections and per-connection-tagged
    models produces exactly one target per model — never the cross product.
    Codex-style models (gpt-5.5) only get the ``subscription`` connection;
    Platform-style models (gpt-5.2) only get the ``api-key`` connection.
    """

    providers = _Providers(
        providers=[
            _provider(
                "openai",
                "OpenAI",
                [("api-key", "API Key"), ("subscription", "ChatGPT Plus/Pro")],
            )
        ]
    )
    models = _Models(
        [
            _model(
                "gpt-5.2",
                (TASK_SPEECH_TO_TEXT,),
                name="GPT-5.2",
                provider_id="openai",
                connections=("api-key",),
            ),
            _model(
                "gpt-5.5",
                (TASK_SPEECH_TO_TEXT,),
                name="GPT-5.5",
                provider_id="openai",
                connections=("subscription",),
            ),
        ]
    )
    service = TaskModelService(
        providers,
        models,
        _Credentials(granted={"openai:api-key", "openai:subscription"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    # One target per (model, allowed-connection) — no cross product.
    assert [target.id for target in targets] == [
        "openai/gpt-5.2::api-key",
        "openai/gpt-5.5::subscription",
    ]
    assert targets[0].connection_id == "openai:api-key"
    assert targets[1].connection_id == "openai:subscription"


def test_list_targets_with_empty_connections_keeps_existing_expansion() -> None:
    """Models with ``connections == ()`` are still valid for every usable
    connection — the per-model allowlist is opt-in. The legacy
    cross-product expansion remains unchanged for these entries."""

    providers = _Providers(
        providers=[
            _provider(
                "openrouter",
                "OpenRouter",
                [("api-key", "API Key"), ("oauth", "OAuth")],
            )
        ]
    )
    models = _Models([_model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,), connections=())])
    service = TaskModelService(
        providers,
        models,
        _Credentials(granted={"openrouter:api-key", "openrouter:oauth"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    assert [target.id for target in targets] == [
        "openrouter/openai/gpt-4o-transcribe::api-key",
        "openrouter/openai/gpt-4o-transcribe::oauth",
    ]


def test_list_targets_with_connections_allowlist_skips_non_matching_connection() -> None:
    """When a connection in the usable set is not in the model's allowlist,
    no target is produced for that (model, connection) pair — the model
    is simply absent on that connection's side of the expansion."""

    providers = _Providers(
        providers=[
            _provider(
                "openai",
                "OpenAI",
                [("api-key", "API Key"), ("subscription", "ChatGPT Plus/Pro")],
            )
        ]
    )
    models = _Models(
        [
            _model(
                "gpt-5.5",
                (TASK_SPEECH_TO_TEXT,),
                name="GPT-5.5",
                provider_id="openai",
                connections=("subscription",),
            )
        ]
    )
    service = TaskModelService(
        providers,
        models,
        _Credentials(granted={"openai:api-key", "openai:subscription"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    # Only the subscription target exists; api-key is not in the allowlist.
    assert [target.id for target in targets] == ["openai/gpt-5.5::subscription"]


def test_list_targets_single_usable_connection_omits_label_suffix() -> None:
    """With one usable connection, the label is the bare model label — unchanged from before."""

    providers = _Providers(
        providers=[
            _provider(
                "openrouter",
                "OpenRouter",
                [("api-key", "API Key"), ("oauth", "OAuth")],
            )
        ]
    )
    models = _Models([_model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,))])
    service = TaskModelService(
        providers,
        models,
        # Only one connection has credentials — only it is expanded.
        _Credentials(granted={"openrouter:api-key"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    assert [target.id for target in targets] == [
        "openrouter/openai/gpt-4o-transcribe::api-key",
    ]
    assert targets[0].label == "OpenRouter / OpenAI GPT-4o Transcribe"


def test_list_targets_skips_provider_without_credentials() -> None:
    providers = _Providers(
        providers=[
            _provider("openrouter", "OpenRouter", [("api-key", "API Key")]),
            _provider("unauth", "Unauth Provider", [("api-key", "API Key")]),
        ]
    )
    models = _Models(
        [
            _model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,)),
            _model(
                "openai/gpt-4o-transcribe",
                (TASK_SPEECH_TO_TEXT,),
                name="Unauth Transcribe",
                provider_id="unauth",
            ),
        ]
    )
    service = TaskModelService(
        providers,
        models,
        _Credentials(granted={"openrouter:api-key"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    # Credential gating removes "unauth" entirely — both from provider
    # iteration and from query results.
    assert [target.id for target in targets] == ["openrouter/openai/gpt-4o-transcribe::api-key"]


def test_list_targets_merges_local_targets_with_provider_targets() -> None:
    providers = _Providers()
    models = _Models([_model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,))])
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
        providers,
        models,
        _Credentials(),
        _Storage(),
        local_targets=local_registry,
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    # Sorted by (kind, label.lower(), id): "local" < "provider".
    assert [(target.kind, target.id) for target in targets] == [
        ("local", "local/whisper-local"),
        ("provider", "openrouter/openai/gpt-4o-transcribe::api-key"),
    ]


def test_list_targets_query_delegation_does_not_reach_provider_without_match() -> None:
    """When the core query excludes all models for a provider, no targets are produced."""

    providers = _Providers()
    models = _Models(
        [
            # Only TTS-capable; STT query should exclude this.
            _model("openai/gpt-4o-mini-tts", (TASK_TEXT_TO_SPEECH,)),
        ]
    )
    service = TaskModelService(providers, models, _Credentials(), _Storage())

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    assert targets == []


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

    with pytest.raises(TaskModelValidationError, match="Unsupported task type 'image_edit'"):
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


def test_options_for_stt_includes_response_format_when_model_advertises_it() -> None:
    """``response_format`` is added to the STT schema only when the model's
    ``supported_parameters`` includes it. Whisper-style models do; non-
    Whisper STT models may not."""

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
    assert "response_format" in field_names
    response_format = next(field for field in schema.fields if field.name == "response_format")
    assert response_format.type == "select"
    response_values = {choice.value for choice in response_format.options}
    assert {"json", "text", "srt", "verbose_json", "vtt"} <= response_values


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


def _model(
    model_id: str,
    task_types: tuple[str, ...],
    *,
    name: str | None = None,
    provider_id: str = "openrouter",
    supported_voices: tuple[str, ...] = (),
    supported_parameters: tuple[str, ...] = (),
    connections: tuple[str, ...] = (),
    task_options: dict | None = None,
    input_modalities: tuple[str, ...] = ("text",),
    output_modalities: tuple[str, ...] = ("text",),
) -> SimpleNamespace:
    """Build a model stub that satisfies ``ModelQuery.matches``.

    The capability fields beyond ``task_types`` are populated with neutral
    defaults so the core query can run end-to-end without raising on
    missing attributes. Callers that care about a specific name must pass
    it explicitly.

    ``supported_voices`` and ``supported_parameters`` flow through to the
    model-aware schema builder so the Phase 3 TTS voice / image seed /
    STT response_format behavior is exercised end-to-end.

    ``connections`` flows through to target expansion: an empty tuple
    means the model is valid for every connection, a non-empty tuple
    gates the model to those local connection ids only.
    """

    if name is None:
        name = (
            "OpenAI GPT-4o Transcribe"
            if TASK_SPEECH_TO_TEXT in task_types
            else "OpenAI GPT-4o Mini TTS"
        )
    return SimpleNamespace(
        provider_id=provider_id,
        model_id=model_id,
        name=name,
        context_window=128000,
        connections=connections,
        # Mirror Model.allows_connection so target expansion sees the real rule.
        allows_connection=lambda connection_id: not connections or connection_id in connections,
        capabilities=SimpleNamespace(
            task_types=task_types,
            input_modalities=input_modalities,
            output_modalities=output_modalities,
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=SimpleNamespace(supported=False),
            supported_voices=supported_voices,
            supported_parameters=supported_parameters,
            task_options=task_options or {},
        ),
    )


def _provider(provider_id: str, name: str, connections: list[tuple[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        id=provider_id,
        name=name,
        connections=[SimpleNamespace(id=cid, label=clabel) for cid, clabel in connections],
    )


class _Providers:
    def __init__(self, providers: list[SimpleNamespace] | None = None) -> None:
        self._providers = providers or [
            _provider("openrouter", "OpenRouter", [("api-key", "API Key")])
        ]

    def list_ids(self) -> list[str]:
        return [provider.id for provider in self._providers]

    def get(self, provider_id: str) -> SimpleNamespace:
        for provider in self._providers:
            if provider.id == provider_id:
                return provider
        raise KeyError(provider_id)


class _Models:
    def __init__(self, models: list[SimpleNamespace] | None = None) -> None:
        self._models: list[SimpleNamespace] = list(models or [])

    def query(self, model_query: ModelQuery) -> list[tuple[str, SimpleNamespace]]:
        provider_filter = model_query.provider_id
        matches: list[tuple[str, SimpleNamespace]] = []
        for model in self._models:
            if provider_filter and model.provider_id != provider_filter:
                continue
            if not model_query.matches(cast("Model", model)):
                continue
            matches.append((model.provider_id, model))
        return sorted(matches, key=lambda item: (item[0], item[1].model_id))

    def get(self, provider_id: str, model_id: str) -> SimpleNamespace:
        """Look up a model by ``(provider_id, model_id)`` — mirrors
        :meth:`core.models.ModelRegistry.get` and raises ``KeyError`` when
        no model matches. The model-aware ``options()`` path catches the
        ``KeyError`` to fall back to provider-level conservative defaults.
        """

        for model in self._models:
            if model.provider_id == provider_id and model.model_id == model_id:
                return model
        raise KeyError(f"Model not found: {provider_id}/{model_id}")


class _Credentials:
    def __init__(self, granted: set[str] | None = None) -> None:
        self._granted = granted if granted is not None else {"openrouter:api-key"}

    def has_credentials(self, _provider_id: str, connection_id: str) -> bool:
        return connection_id in self._granted

    def is_usable(self, provider_id: str, connection_id: str) -> bool:
        return self.has_credentials(provider_id, connection_id)


class _Storage:
    def __init__(self, settings: Mapping[str, object] | None = None) -> None:
        self._settings = dict(settings or {})

    def load_model_task_settings(self) -> dict[str, object]:
        return dict(self._settings)

    def update_model_task_settings(self, model_tasks: object) -> object:
        assert isinstance(model_tasks, Mapping)
        for task_type, binding in model_tasks.items():
            if isinstance(binding, Mapping) and binding.get("target"):
                self._settings[task_type] = dict(binding)
            else:
                self._settings.pop(task_type, None)
        return dict(self._settings)
