"""Tests for model task targets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.model_tasks import (
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
    TaskModelService,
    TaskModelValidationError,
)
from core.model_tasks.constants import TASK_DECISION
from core.models.pricing import TokenPricing
from core.providers.wire_profile import DecisionWire, WireProfile
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
    ("task_type", "expected"),
    [
        pytest.param(
            TASK_SPEECH_TO_TEXT,
            [("openrouter/openai/gpt-4o-transcribe::api-key", "OpenAI GPT-4o Transcribe")],
            id="speech-to-text",
        ),
        pytest.param(
            TASK_TEXT_TO_SPEECH,
            [("openrouter/openai/gpt-4o-mini-tts::api-key", "OpenAI GPT-4o Mini TTS")],
            id="text-to-speech",
        ),
        pytest.param(
            TASK_IMAGE_GENERATION,
            [
                ("openrouter/dall-e-3::api-key", "DALL-E 3"),
                ("openrouter/gpt-image-1::api-key", "GPT Image 1"),
            ],
            id="image-generation",
        ),
        # Video Models without a duration choice edit or upscale videos.
        pytest.param(
            TASK_VIDEO_GENERATION,
            [("openrouter/text-to-video::api-key", "Text to Video")],
            id="video-generation",
        ),
        pytest.param(
            TASK_DECISION,
            [
                ("openrouter/cloudflare/clef::api-key", "Cloudflare: Clef"),
                ("openrouter/future/decider::api-key", "Future Decider"),
                ("openrouter/typesafe/jev-1.14::api-key", "TypeSafe: Jev 1.14"),
                ("openrouter/~typesafe/jev-latest::api-key", "TypeSafe: Jev Latest"),
            ],
            id="decision",
        ),
        pytest.param(TASK_LIVE_VOICE, [], id="no-matching-model"),
    ],
)
def test_list_targets_offers_only_models_tagged_for_the_task(
    task_type: str, expected: list[tuple[str, str]]
) -> None:
    models = _Models(
        [
            _model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,)),
            _model("openai/gpt-4o-mini-tts", (TASK_TEXT_TO_SPEECH,)),
            _model("dall-e-3", (TASK_IMAGE_GENERATION,), name="DALL-E 3"),
            _model("gpt-image-1", (TASK_IMAGE_GENERATION,), name="GPT Image 1"),
            # A router picks another Model per request.
            _model("openrouter/auto", (TASK_IMAGE_GENERATION,), name="Auto Router"),
            _model(
                "text-to-video",
                (TASK_VIDEO_GENERATION,),
                name="Text to Video",
                task_options={
                    TASK_VIDEO_GENERATION: {
                        "parameters": {"duration": {"type": "enum", "values": ["5"]}}
                    }
                },
            ),
            _model("video-upscale", (TASK_VIDEO_GENERATION,), name="Video Upscale"),
            _model("typesafe/jev-1.14", (TASK_DECISION,), name="TypeSafe: Jev 1.14"),
            _model("~typesafe/jev-latest", (TASK_DECISION,), name="TypeSafe: Jev Latest"),
            _model("cloudflare/clef", (TASK_DECISION,), name="Cloudflare: Clef"),
            _model("future/decider", (TASK_DECISION,), name="Future Decider"),
            # Jev Router's catalog describes a Chat Model, not native Decisions.
            _model("typesafe/jev-router", ("chat", "text_output"), name="TypeSafe: Jev Router"),
        ]
    )
    service = TaskModelService(_Providers(), models, _Credentials(), _Storage())

    targets = service.list_targets(task_type)

    assert [(target.id, target.label) for target in targets] == [
        (target_id, f"OpenRouter / {label}") for target_id, label in expected
    ]
    assert all(target.connection_id == "openrouter:api-key" for target in targets)


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
            # Music Models read images but answer with a track.
            _model(
                "music-model",
                ("chat", "text_output", TASK_MUSIC_GENERATION),
                name="Music Model",
                provider_id="opencode-go",
                connections=("api-key",),
                input_modalities=("text", "image"),
                output_modalities=("text", "audio"),
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


_OPENROUTER_TWO_CONNECTIONS = _provider(
    "openrouter", "OpenRouter", [("api-key", "API Key"), ("oauth", "OAuth")]
)
_OPENAI_TWO_CONNECTIONS = _provider(
    "openai", "OpenAI", [("api-key", "API Key"), ("subscription", "ChatGPT Plus/Pro")]
)
_TRANSCRIBE = "openrouter/openai/gpt-4o-transcribe"


def test_decision_targets_require_the_connections_native_route() -> None:
    target = "openai/gpt-6-luna::api-key"
    binding = {"target": target, "options": {}}
    service = TaskModelService(
        _Providers([_OPENAI_TWO_CONNECTIONS]),
        _Models(
            [
                _model(
                    "gpt-6-luna",
                    ("chat", "text_output", TASK_DECISION),
                    name="GPT-6 Luna",
                    provider_id="openai",
                )
            ]
        ),
        _Credentials({"openai:api-key", "openai:subscription"}),
        _Storage({TASK_DECISION: binding}),
    )

    targets = service.list_targets(TASK_DECISION)

    assert [(candidate.id, candidate.connection_id) for candidate in targets] == [
        (target, "openai:api-key")
    ]
    assert service.binding_is_usable(TASK_DECISION)
    service.validate_execution_target(service.binding_for(TASK_DECISION))
    with pytest.raises(TaskModelValidationError):
        service.validate_binding(
            TASK_DECISION, {"target": "openai/gpt-6-luna::subscription", "options": {}}
        )


@pytest.mark.parametrize(
    ("wire", "tagged", "credentialed", "usable"),
    [
        pytest.param(
            DecisionWire(protocol="system_one", endpoint="/v1/systemone"),
            True,
            True,
            True,
            id="system-one-compatible-provider",
        ),
        pytest.param(
            DecisionWire(protocol="openai_decisions", endpoint="/v1/decisions"),
            True,
            True,
            True,
            id="openai-decisions-compatible-provider",
        ),
        pytest.param(DecisionWire(), True, True, False, id="no-native-route"),
        pytest.param(
            DecisionWire(protocol="system_one"), True, True, False, id="no-route-endpoint"
        ),
        pytest.param(
            DecisionWire(protocol="system_one", endpoint="/v1/systemone"),
            False,
            True,
            False,
            id="chat-model-without-decision-capability",
        ),
        pytest.param(
            DecisionWire(protocol="system_one", endpoint="/v1/systemone"),
            True,
            False,
            False,
            id="no-usable-credentials",
        ),
    ],
)
def test_decision_selection_and_execution_share_the_native_route_gate(
    wire: DecisionWire, tagged: bool, credentialed: bool, usable: bool
) -> None:
    target = "new-provider/next-model::api-key"

    def resolve(provider_id: str, connection_id: str, model_id: str) -> WireProfile:
        assert (provider_id, connection_id) == ("new-provider", "api-key")
        assert model_id in {"next-model", "no-native-route"}
        return WireProfile(
            provider_id=provider_id,
            connection_id=connection_id,
            model_id=model_id,
            protocol="chat_completions",
            decision=wire if model_id == "next-model" else DecisionWire(),
        )

    service = TaskModelService(
        _Providers([_provider("new-provider", "New Provider", [("api-key", "API Key")])]),
        _Models(
            [
                _model(
                    "next-model",
                    (TASK_DECISION,) if tagged else ("chat", "text_output"),
                    provider_id="new-provider",
                    name="Next Model",
                ),
                # A hidden namesake must not change the selectable Model's label.
                _model(
                    "no-native-route",
                    (TASK_DECISION,),
                    provider_id="new-provider",
                    name="Next Model",
                ),
            ]
        ),
        _Credentials({"new-provider:api-key"} if credentialed else set()),
        _Storage({TASK_DECISION: {"target": target, "options": {}}}),
        wire_profile_resolver=resolve,
    )

    assert [
        (candidate.id, candidate.label) for candidate in service.list_targets(TASK_DECISION)
    ] == ([(target, "New Provider / Next Model")] if usable else [])
    assert service.binding_is_usable(TASK_DECISION) is usable
    binding = service.binding_for(TASK_DECISION)
    if usable:
        service.validate_execution_target(binding)
    else:
        with pytest.raises(TaskModelValidationError):
            service.validate_execution_target(binding)


@pytest.mark.parametrize(
    ("providers", "granted", "models", "expected"),
    [
        # Sorted by label: "API Key" before "OAuth".
        pytest.param(
            [_OPENROUTER_TWO_CONNECTIONS],
            {"openrouter:api-key", "openrouter:oauth"},
            [_model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,))],
            [
                (f"{_TRANSCRIBE}::api-key", "OpenRouter / OpenAI GPT-4o Transcribe (API Key)"),
                (f"{_TRANSCRIBE}::oauth", "OpenRouter / OpenAI GPT-4o Transcribe (OAuth)"),
            ],
            id="one-target-per-usable-connection",
        ),
        pytest.param(
            [_OPENROUTER_TWO_CONNECTIONS],
            {"openrouter:api-key"},
            [_model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,))],
            [(f"{_TRANSCRIBE}::api-key", "OpenRouter / OpenAI GPT-4o Transcribe (API Key)")],
            id="single-usable-connection-is-still-named",
        ),
        pytest.param(
            [_provider("mistral", "Mistral AI", [("api-key", "API Key")])],
            {"mistral:api-key"},
            [
                _model(
                    model_id,
                    (TASK_SPEECH_TO_TEXT,),
                    name="Voxtral Mini Transcribe",
                    provider_id="mistral",
                )
                for model_id in ("voxtral-mini-2602", "voxtral-mini-latest")
            ],
            [
                ("mistral/voxtral-mini-2602::api-key", "Mistral AI / voxtral-mini-2602"),
                ("mistral/voxtral-mini-latest::api-key", "Mistral AI / voxtral-mini-latest"),
            ],
            id="same-name-alias-and-pinned-version-stay-distinguishable",
        ),
        pytest.param(
            [_OPENROUTER_TWO_CONNECTIONS],
            {"openrouter:api-key"},
            [
                _model(model_id, (TASK_SPEECH_TO_TEXT,), connections=(connection,))
                for model_id, connection in (
                    ("openai/gpt-4o-transcribe", "api-key"),
                    ("unavailable-alias", "oauth"),
                )
            ],
            [(f"{_TRANSCRIBE}::api-key", "OpenRouter / OpenAI GPT-4o Transcribe (API Key)")],
            id="hidden-namesake-does-not-change-visible-label",
        ),
        # A per-model allowlist yields no cross product.
        pytest.param(
            [_OPENAI_TWO_CONNECTIONS],
            {"openai:api-key", "openai:subscription"},
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
            ],
            [
                ("openai/gpt-5.2::api-key", "OpenAI / GPT-5.2 (API Key)"),
                ("openai/gpt-5.5::subscription", "OpenAI / GPT-5.5 (ChatGPT Plus/Pro)"),
            ],
            id="per-model-connection-allowlist",
        ),
        pytest.param(
            [
                _provider("openrouter", "OpenRouter", [("api-key", "API Key")]),
                _provider("unauth", "Unauth Provider", [("api-key", "API Key")]),
            ],
            {"openrouter:api-key"},
            [
                _model("openai/gpt-4o-transcribe", (TASK_SPEECH_TO_TEXT,)),
                _model(
                    "openai/gpt-4o-transcribe",
                    (TASK_SPEECH_TO_TEXT,),
                    name="Unauth Transcribe",
                    provider_id="unauth",
                ),
            ],
            [(f"{_TRANSCRIBE}::api-key", "OpenRouter / OpenAI GPT-4o Transcribe")],
            id="provider-without-credentials",
        ),
    ],
)
def test_list_targets_expand_only_usable_allowed_connections(
    providers: list[SimpleNamespace],
    granted: set[str],
    models: list[SimpleNamespace],
    expected: list[tuple[str, str]],
) -> None:
    service = TaskModelService(
        _Providers(providers=providers), _Models(models), _Credentials(granted), _Storage()
    )

    targets = service.list_targets(TASK_SPEECH_TO_TEXT)

    assert [(target.id, target.label) for target in targets] == expected
    assert all(
        target.connection_id == f"{target.id.split('/')[0]}:{target.id.split('::')[1]}"
        for target in targets
    )


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


def test_decision_targets_carry_published_limits_and_prices_without_reusing_chat_context() -> None:
    limited = _model(
        "example/limited-decision",
        (TASK_DECISION,),
        name="Limited Decision",
        output_modalities=("decisions",),
        task_options={
            TASK_DECISION: {
                "limits": {
                    "max_input_tokens": 4096,
                    "max_questions": 20,
                    "max_choices": 32,
                    "max_levels": 5,
                }
            }
        },
    )
    limited.pricing = TokenPricing.from_cost({"input": 0.12}, source="openrouter:decision")
    decision_only = _model(
        "future/decision-only",
        (TASK_DECISION,),
        name="Decision Only",
        output_modalities=("decisions",),
    )
    chat_and_decision = _model(
        "future/chat-and-decision",
        ("chat", "text_output", TASK_DECISION),
        name="Chat and Decision",
    )
    service = TaskModelService(
        _Providers(),
        _Models([limited, decision_only, chat_and_decision]),
        _Credentials(),
        _Storage(),
    )

    facts = {target.id: target.to_dict()["facts"] for target in service.list_targets(TASK_DECISION)}

    assert facts == {
        "openrouter/example/limited-decision::api-key": {
            "question_types": ["yes_no", "choice", "score"],
            "max_input_tokens": 4096,
            "max_questions": 20,
            "max_choices": 32,
            "max_levels": 5,
            "input_price_per_million": 0.12,
        },
        "openrouter/future/decision-only::api-key": {
            "question_types": ["yes_no", "choice", "score"],
            "max_input_tokens": 128000,
            "max_questions": None,
            "max_choices": None,
            "max_levels": None,
            "input_price_per_million": None,
        },
        "openrouter/future/chat-and-decision::api-key": {
            "question_types": ["yes_no", "choice", "score"],
            "max_input_tokens": None,
            "max_questions": None,
            "max_choices": None,
            "max_levels": None,
            "input_price_per_million": None,
        },
    }


def test_embedding_targets_carry_selection_facts() -> None:
    hosted = _model("qwen/qwen3-embedding-8b", (TASK_TEXT_EMBEDDING,), name="Qwen3 Embedding")
    hosted.metadata = {"openrouter": {"modality": "text->embeddings"}}
    hosted.pricing = TokenPricing.from_cost({"input": 0.01}, source="openrouter:qwen")
    on_machine = _model(
        "nomic-embed-text:latest", (TASK_TEXT_EMBEDDING,), provider_id="ollama", name="Nomic"
    )
    on_machine.metadata = {"ollama": {"local": True}}
    on_machine.pricing = None
    unprofiled = _model("example/embed-x", (TASK_TEXT_EMBEDDING,), name="Embed X")
    unprofiled.metadata = {}
    unprofiled.pricing = None
    service = TaskModelService(
        _Providers(
            [
                _provider("openrouter", "OpenRouter", [("api-key", "API Key")]),
                _provider("ollama", "Ollama", [("local", "Local")]),
            ]
        ),
        _Models([hosted, on_machine, unprofiled, _model("openai/whisper", (TASK_SPEECH_TO_TEXT,))]),
        _Credentials({"openrouter:api-key", "ollama:local"}),
        _Storage(),
        local_targets=LocalTaskTargetRegistry(
            [
                LocalTaskTargetDescriptor(
                    id="granite-embedding-r2",
                    label="Granite Embedding",
                    task_types=(TASK_TEXT_EMBEDDING,),
                    metadata={"max_input_tokens": 2048},
                )
            ]
        ),
    )

    facts = {
        target.id: target.to_dict()["facts"] for target in service.list_targets(TASK_TEXT_EMBEDDING)
    }

    assert {
        target_id: (
            fact["local"],
            fact["multilingual"],
            fact["recommended_rank"],
            fact["max_input_tokens"],
            fact["input_price_per_million"],
            bool(fact["note"]),
        )
        for target_id, fact in facts.items()
    } == {
        # A local engine costs nothing; its own input limit replaces the family's.
        "local/granite-embedding-r2": (True, True, 1, 2048, 0.0, True),
        "ollama/nomic-embed-text:latest::local": (True, False, None, 8192, None, True),
        "openrouter/qwen/qwen3-embedding-8b::api-key": (False, True, 4, 32768, 0.01, True),
        # An unprofiled Model keeps its catalog context window and has no curated facts.
        "openrouter/example/embed-x::api-key": (False, None, None, 128000, None, False),
    }
    # Other task types carry no facts yet.
    assert [target.to_dict()["facts"] for target in service.list_targets(TASK_SPEECH_TO_TEXT)] == [
        {}
    ]


def test_live_voice_targets_are_explicit_and_limited_to_allowed_connections() -> None:
    providers = _Providers(
        providers=[
            _provider(
                "openai", "OpenAI", [("api-key", "API Key"), ("subscription", "ChatGPT Plus/Pro")]
            )
        ]
    )
    service = TaskModelService(
        providers,
        _live_voice_registry(),
        _Credentials({"openai:api-key", "openai:subscription"}),
        _Storage(),
    )

    targets = service.list_targets(TASK_LIVE_VOICE)

    assert [target.id for target in targets] == [
        "openai/live-key::api-key",
        "openai/live-sub::subscription",
    ]
    assert all(target.task_types == (TASK_LIVE_VOICE,) for target in targets)
    # An audio Model is not a live voice target without the explicit tag.
    audio = _Models(
        [_model("audio-chat", ("audio_input", "audio_generation"), provider_id="openai")]
    )
    audio_service = TaskModelService(providers, audio, _Credentials({"openai:api-key"}), _Storage())
    assert audio_service.list_targets(TASK_LIVE_VOICE) == []
