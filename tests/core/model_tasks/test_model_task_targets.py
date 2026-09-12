"""Tests for model task targets."""

from __future__ import annotations

from core.model_tasks import (
    SUPPORTED_TASK_TYPES,
    TASK_IMAGE_GENERATION,
    TASK_IMAGE_UNDERSTANDING,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_TO_SPEECH,
    LocalTaskTargetDescriptor,
    LocalTaskTargetRegistry,
    TaskModelService,
    validate_task_type,
)
from tests.core.model_tasks.model_tasks_test_support import (
    _Credentials,
    _model,
    _Models,
    _provider,
    _Providers,
    _Storage,
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
