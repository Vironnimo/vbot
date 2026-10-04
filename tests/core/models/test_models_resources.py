"""The bundled Model DB: every shipped catalog loads, and live-verified facts stay pinned."""

import json
from pathlib import Path
from typing import Any

import pytest

from core.models.models import Model, ModelRegistry
from core.models.query import ModelQuery
from core.providers.ollama import OllamaCloudAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.providers import ProviderRegistry
from core.providers.reasoning import resolve_reasoning_intent
from core.providers.wire_profile import WireProfile
from core.providers.wire_profiles import standalone_wire_binding

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
RESOURCES_DIR = PROJECT_ROOT / "resources"


@pytest.fixture(scope="module")
def registry() -> ModelRegistry:
    """The bundled Model DB, assembled once and never shared through the path cache."""

    return ModelRegistry.load(RESOURCES_DIR, custom_providers={})


def _profile(model: Model, *keys: str, wire: WireProfile | None = None) -> dict[str, Any]:
    """The requested Model facts, plus the wire decisions of ``wire`` when given."""

    reasoning = model.capabilities.reasoning
    facts = {
        "name": model.name,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "tools": model.capabilities.tools,
        "vision": model.capabilities.vision,
        "input_modalities": model.capabilities.input_modalities,
        "reasoning": (reasoning.supported, reasoning.control, reasoning.levels),
    }
    if wire is not None:
        facts["replay_scope"] = wire.replay.scope
        facts["list_announced_tools"] = wire.request.list_announced_tools
    return {key: facts[key] for key in keys}


def _wire_profile(registry: ModelRegistry, provider_id: str, model_id: str) -> WireProfile:
    """The bundled wire profile of one catalog Model on the Provider's first Connection."""

    adapter = {
        "ollama-cloud": OllamaCloudAdapter,
        "opencode-go": OpenCodeGoAdapter,
        "opencode-zen": OpenCodeZenAdapter,
    }[provider_id]
    return standalone_wire_binding(
        provider_id=provider_id,
        connection_id="api-key",
        protocols=adapter.WIRE_PROTOCOLS,
        model_lookup=lambda selected: registry.get(provider_id, selected),
    ).profile(model_id)


# ---------------------------------------------------------------------------
# The whole catalog
# ---------------------------------------------------------------------------


def test_every_bundled_model_loads_with_typed_facts(registry: ModelRegistry) -> None:
    models = [model for _, model in registry.query(ModelQuery())]

    for provider_id in (
        "anthropic",
        "github-copilot",
        "mistral",
        "ollama-cloud",
        "openai",
        "opencode-go",
        "openrouter",
    ):
        assert registry.list_for_provider(provider_id), provider_id
    for model in models:
        capabilities = model.capabilities
        assert model.model_id and model.name and isinstance(model.family, str)
        assert all(
            isinstance(flag, bool)
            for flag in (
                capabilities.vision,
                capabilities.tools,
                capabilities.json_mode,
                capabilities.reasoning.supported,
            )
        ), model.model_id
        assert isinstance(capabilities.reasoning.levels, tuple)
        assert capabilities.reasoning.control in (None, "levels", "on_off", "budget")
        for limit in (model.context_window, model.max_output_tokens):
            assert limit is None or (isinstance(limit, int) and limit >= 0), model.model_id


def test_a_provider_model_without_a_canonical_join_loads(registry: ModelRegistry) -> None:
    # OpenCode Go keys ``deepseek-v4-pro`` bare; the canonical id is
    # ``deepseek/deepseek-v4-pro``, so it loads on Provider + Override data alone.
    assert (RESOURCES_DIR / "models" / "models.json").exists()

    assert registry.get("opencode-go", "deepseek-v4-pro").capabilities.reasoning.supported is True


def test_the_bundled_root_loads_without_dropping_records(registry: ModelRegistry) -> None:
    """A refreshed catalog must not leave omitted records or partial overrides."""

    assert ModelRegistry.validate(RESOURCES_DIR) == []
    for path in sorted((RESOURCES_DIR / "models").glob("*.overrides.json")):
        provider_id = path.name.removesuffix(".overrides.json")
        if provider_id == "models":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for model_id in data.get("models", {}):
            assert registry.get(provider_id, model_id).model_id == model_id


# ---------------------------------------------------------------------------
# Ollama Cloud (live-verified through 2026-09-11)
# ---------------------------------------------------------------------------


def test_ollama_cloud_catalog_is_separate_and_entirely_remote(registry: ModelRegistry) -> None:
    models = registry.list_for_provider("ollama-cloud")

    assert models
    assert registry.get("ollama-cloud", "gpt-oss:120b").model_id == "gpt-oss:120b"
    assert all(model.connections == ("api-key",) for model in models)
    assert all(model.metadata.get("ollama", {}).get("remote") is True for model in models)
    assert all(model.metadata.get("ollama", {}).get("local") is not True for model in models)


# Streaming token accounting on /v1/chat/completions (billed-input deltas in
# vBot's real request shapes, visible-content control per variant):
# - full_history (the wire default): the ``reasoning`` carrier is billed in
#   both scopes. DeepSeek cross-Run replay is conditional: a Tool-free
#   follow-up ignores history, one carrying Tools bills the whole history.
# - current_run: in-run replayed reasoning is billed, cross-run is stripped.
# - none: the carrier is stripped in both scopes (zero delta, positive control).
_OLLAMA_CLOUD_REPLAY = {
    "deepseek-v4.1-flash": "full_history",
    "deepseek-v4-pro:0813": "full_history",
    "gemma4:31b": "none",
    "glm-5.2": "full_history",
    "glm-5.3": "full_history",
    "glm-5.3-flash": "full_history",
    "gpt-oss:120b": "none",
    "gpt-oss:20b": "none",
    "kimi-k2.6": "current_run",
    "kimi-k2.7-code": "current_run",
    "kimi-k3": "full_history",
    "minimax-m2.7": "none",
    "minimax-m3": "none",
    "nemotron-3-nano:30b": "none",
    "nemotron-3-super": "none",
    "nemotron-3-ultra": "none",
}


def test_ollama_cloud_reasoning_replay_policies(registry: ModelRegistry) -> None:
    assert {
        model_id: _wire_profile(registry, "ollama-cloud", model_id).replay.scope
        for model_id in _OLLAMA_CLOUD_REPLAY
    } == _OLLAMA_CLOUD_REPLAY


def test_ollama_cloud_models_emit_reasoning_as_their_response_carrier(
    registry: ModelRegistry,
) -> None:
    """The earlier ``reasoning_content``/``reasoning_details`` profiles were wrong:
    those fields are stripped even in-run on this wire."""

    carriers = {
        model_id: _wire_profile(registry, "ollama-cloud", model_id)
        for model_id in _OLLAMA_CLOUD_REPLAY
    }

    assert {
        model_id: (wire.replay.history_field, wire.response.reasoning_fields[0])
        for model_id, wire in carriers.items()
    } == dict.fromkeys(_OLLAMA_CLOUD_REPLAY, ("reasoning", "reasoning"))


def test_ollama_cloud_gateway_output_limits(registry: ModelRegistry) -> None:
    limits = {
        "deepseek-v4-pro:0813": 65_536,
        "glm-5.2": 131_072,
        "kimi-k2.7-code": 262_144,
        "minimax-m2.7": 131_072,
        "minimax-m3": 131_072,
        "nemotron-3-nano:30b": 131_072,
        "nemotron-3-super": 65_536,
        "nemotron-3-ultra": 65_536,
    }

    assert {
        model_id: registry.get("ollama-cloud", model_id).max_output_tokens for model_id in limits
    } == limits


def test_ollama_cloud_reasoning_controls(registry: ModelRegistry) -> None:
    # Documented ladders exclude ``none``, which stays the wire's explicit off
    # switch. MiniMax ignores both documented off-control shapes: no control.
    ladder = (True, "levels", ("low", "high", "max"))
    expected = {
        "deepseek-v4.1-flash": ladder,
        "deepseek-v4-pro:0813": ladder,
        "glm-5.3": ladder,
        "minimax-m2.7": (True, None, ()),
        "minimax-m3": (True, None, ()),
    }

    assert {
        model_id: _profile(registry.get("ollama-cloud", model_id), "reasoning")["reasoning"]
        for model_id in expected
    } == expected


def test_ollama_cloud_deepseek_v41_verified_profile(registry: ModelRegistry) -> None:
    """Pin the Cloud profile verified on 2026-09-11, including its larger output cap."""

    model = registry.get("ollama-cloud", "deepseek-v4.1-flash")

    assert model.model_id == "deepseek-v4.1-flash"
    assert model.connections == ("api-key",)
    assert model.context_window == 1_048_576
    assert model.capabilities.vision is True
    assert model.capabilities.tools is True
    assert _profile(model, "reasoning")["reasoning"] == (True, "levels", ("low", "high", "max"))
    assert model.max_output_tokens == 393_216
    assert model.recommended_temperature == 1.0
    assert model.recommended_top_p == 0.95
    assert model.metadata["ollama"]["remote"] is True
    wire = _wire_profile(registry, "ollama-cloud", "deepseek-v4.1-flash")
    assert (wire.replay.scope, wire.replay.history_field) == ("full_history", "reasoning")
    assert wire.media.types == {"image/jpeg", "image/png", "image/webp"}


# ---------------------------------------------------------------------------
# OpenCode Go and OpenCode Zen
# ---------------------------------------------------------------------------


def test_opencode_go_current_endpoint_profiles_load(registry: ModelRegistry) -> None:
    """Protect the current wire of every published id.

    The bundled catalog's protocol hints and ``resources/wire/opencode-go.json`` route them.
    """

    expected_by_protocol = {
        "responses": (
            "grok-4.6",
            "grok-4.7",
            "gpt-5.6-luna",
            "gpt-6-luna",
            "muse-spark-1.2-contributor",
            "muse-spark-1.3-contributor",
        ),
        "chat_completions": (
            "glm-5.1",
            "glm-5.3-flash",
            "glm-5.3",
            "glm-5.2",
            "kimi-k3",
            "kimi-k2.6",
            "kimi-k2.7-code",
            "longcat-2.0",
            "longcat-2.5-preview-free",
            "deepseek-flash",
            "deepseek-v4.1-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "mimo-v2.5",
            "mimo-v2.5-pro",
            "mimo-v2.6-flash",
            "mimo-v2.6-pro",
            "hy4-preview",
            "hy3",
            "space-bunny-free",
            "omen-alpha",
        ),
        "messages": (
            "minimax-m2.5",
            "minimax-m3",
            "minimax-m2.7",
            "qwen3.8-max",
            "qwen3.8-flash",
            "qwen3.7-plus",
            "qwen3.7-max",
            "qwen3.6-plus",
        ),
    }
    expected = {
        model_id: protocol
        for protocol, model_ids in expected_by_protocol.items()
        for model_id in model_ids
    }
    assert len(expected) == 36

    assert {
        model_id: _wire_profile(registry, "opencode-go", model_id).protocol for model_id in expected
    } == expected
    assert {model.model_id for model in registry.list_for_provider("opencode-go")} == set(expected)


def test_opencode_go_response_fields_are_not_history_field_guesses(
    registry: ModelRegistry,
) -> None:
    """Profiles describe inbound response carriers, not outbound replay."""

    expected = {
        **dict.fromkeys(("kimi-k2.6", "kimi-k3", "hy3", "hy4-preview"), "reasoning"),
        **dict.fromkeys(
            (
                "glm-5.1",
                "omen-alpha",
                "mimo-v2.5",
                "mimo-v2.5-pro",
                "mimo-v2.6-flash",
                "mimo-v2.6-pro",
            ),
            "reasoning_content",
        ),
    }

    profiles = {model_id: _wire_profile(registry, "opencode-go", model_id) for model_id in expected}

    assert {
        model_id: profile.response.reasoning_fields[0] for model_id, profile in profiles.items()
    } == expected
    assert {profile.replay.history_field for profile in profiles.values()} == {"reasoning_content"}


_FIVE_LEVELS = (True, "levels", ("low", "medium", "high", "xhigh", "max"))


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        pytest.param(
            "space-bunny-free",
            {
                "name": "Space Bunny Free",
                "context_window": 1_048_576,
                "max_output_tokens": 524_288,
                "reasoning": _FIVE_LEVELS,
            },
            id="space-bunny-free",
        ),
        pytest.param(
            "longcat-2.5-preview-free",
            {
                "name": "LongCat 2.5 Preview Free",
                "context_window": 1_000_000,
                "max_output_tokens": 131_072,
                "tools": True,
                "vision": True,
                "input_modalities": ("text", "image"),
                "reasoning": (True, "on_off", ()),
                "replay_scope": "full_history",
            },
            id="longcat-2.5-preview-free",
        ),
        *(
            pytest.param(
                model_id,
                {
                    "context_window": 1_048_576,
                    "max_output_tokens": 131_072,
                    "tools": True,
                    "reasoning": (True, "on_off", ()),
                    "replay_scope": "full_history",
                },
                id=model_id,
            )
            for model_id in ("mimo-v2.6-flash", "mimo-v2.6-pro")
        ),
        *(
            pytest.param(
                model_id,
                {
                    "name": "DeepSeek V4.1 Flash",
                    "context_window": 1_000_000,
                    "max_output_tokens": 384_000,
                    "reasoning": (True, "levels", ("low", "high", "max")),
                    "tools": True,
                    "input_modalities": ("text", "image"),
                    "vision": True,
                    "replay_scope": "full_history",
                },
                id=model_id,
            )
            for model_id in ("deepseek-flash", "deepseek-v4.1-flash")
        ),
        # Every Go Model replays full history. Exact vBot probes on 2026-09-02
        # showed persisted ``reasoning_content`` of GLM 5.2/5.3 is billed in a
        # Tool continuation. No ``reasoning_request_format`` field.
        pytest.param("glm-5.2", {"replay_scope": "full_history"}, id="glm-5.2"),
        # Only GLM-5.3-Flash on this gateway silently dropped calls to Tools that a
        # System Reminder announced outside ``tools[]`` (vBot probes 2026-09-28).
        pytest.param(
            "glm-5.3",
            {"replay_scope": "full_history", "list_announced_tools": False},
            id="glm-5.3",
        ),
        pytest.param(
            "glm-5.3-flash",
            {
                "context_window": 1_000_000,
                "replay_scope": "full_history",
                "list_announced_tools": True,
            },
            id="glm-5.3-flash",
        ),
    ],
)
def test_opencode_go_gateway_profiles(
    registry: ModelRegistry,
    model_id: str,
    expected: dict[str, Any],
) -> None:
    model = registry.get("opencode-go", model_id)
    wire = _wire_profile(registry, "opencode-go", model_id)

    assert _profile(model, *expected, wire=wire) == expected


def test_openrouter_space_bunny_gateway_facts(registry: ModelRegistry) -> None:
    router = registry.get("openrouter", "stealth/space-bunny-alpha")

    assert _profile(router, "context_window", "max_output_tokens", "reasoning") == {
        "context_window": 1_000_000,
        "max_output_tokens": 524_288,
        "reasoning": _FIVE_LEVELS,
    }
    assert router.capabilities.reasoning.mandatory is True


def test_zen_snapshot_serves_every_reviewed_model_on_both_connections(
    registry: ModelRegistry,
) -> None:
    provider = ProviderRegistry.load(RESOURCES_DIR).get("opencode-zen")
    usable = registry.list_for_provider("opencode-zen")

    assert usable
    assert {model.model_id for model in usable}.isdisjoint(provider.catalog_exclusions)
    for model in usable:
        assert set(model.connections) == {"api-key", "account"}
        # Discovery keeps only Models the wire profile admits.
        assert _wire_profile(registry, "opencode-zen", model.model_id).admission.state == (
            "available"
        )


# ---------------------------------------------------------------------------
# OpenAI, xAI, Anthropic, OpenRouter
# ---------------------------------------------------------------------------


def test_openai_reasoning_models_load_connection_specific_limits(
    registry: ModelRegistry,
) -> None:
    """Current OpenAI reasoning Models load their Connections and per-Connection windows.

    The unsuffixed GPT-5.6 alias is a Platform alias only: the live ChatGPT
    Codex endpoint rejects it, while the named 5.6 variants are available on
    both connections. (Their wire, including Responses routing, is the wire
    profile's: ``resources/wire/openai.json``.)
    """

    gpt_52 = registry.get("openai", "gpt-5.2")
    assert gpt_52.connections == ("api-key",)
    assert gpt_52.context_window == 400_000
    assert gpt_52.max_output_tokens == 128_000
    assert set(gpt_52.capabilities.supported_parameters) == {
        "max_output_tokens",
        "parallel_tool_calls",
        "reasoning",
        "response_format",
        "tools",
    }

    gpt_55 = registry.get("openai", "gpt-5.5")
    assert gpt_55.connections == ("api-key", "subscription")

    for model_id in ("gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"):
        model = registry.get("openai", model_id)
        assert model.context_window_for("api-key") == 1_050_000
        assert model.context_window_for("subscription") == 272_000

    alias = registry.get("openai", "gpt-5.6")
    assert alias.connections == ("api-key",)


def test_gpt6_astra_profile_loads(registry: ModelRegistry) -> None:
    gpt = registry.get("openai", "gpt-6-astra")

    assert gpt.connections == ("subscription",)
    assert gpt.context_window_for("subscription") == 272_000
    assert gpt.capabilities.reasoning.levels == ("low", "medium", "high", "xhigh", "max")
    assert gpt.capabilities.tools is True


@pytest.mark.parametrize(
    ("model_id", "short_rates", "long_rates", "supports_none"),
    [
        ("gpt-6-sol", (2.0, 0.2, 2.5, 10.0), (4.0, 0.4, 5.0, 15.0), True),
        ("gpt-6-luna", (0.1, 0.01, 0.125, 0.5), (0.2, 0.02, 0.25, 0.75), True),
        ("gpt-6.1-sol", (2.0, 0.1, 2.5, 10.0), (4.0, 0.2, 5.0, 15.0), False),
    ],
)
def test_gpt6_loads_with_official_limits_and_pricing_on_every_published_provider(
    registry: ModelRegistry,
    model_id: str,
    short_rates: tuple[float, ...],
    long_rates: tuple[float, ...],
    supports_none: bool,
) -> None:
    model = registry.get("openai", model_id)

    assert model.connections == ("api-key", "subscription")
    assert model.context_window_for("api-key") == 1_050_000
    assert model.context_window_for("subscription") == 272_000
    assert model.max_output_tokens == 128_000
    assert model.capabilities.input_modalities[:2] == ("text", "image")
    assert model.capabilities.output_modalities == ("text",)
    assert model.capabilities.tools is True
    assert model.capabilities.json_mode is True
    levels = ("low", "medium", "high", "xhigh", "max")
    assert model.capabilities.reasoning.levels == (("none", *levels) if supports_none else levels)
    assert model.pricing is not None
    assert model.pricing.source == f"models.dev:openai/{model_id}"
    rates = model.pricing.rates
    assert (rates.input, rates.cache_read, rates.cache_write, rates.output) == short_rates
    [tier] = model.pricing.tiers
    assert tier.above_tokens == 272_000
    assert (
        tier.rates.input,
        tier.rates.cache_read,
        tier.rates.cache_write,
        tier.rates.output,
    ) == long_rates

    zen = registry.get("opencode-zen", model_id)
    zen_wire = _wire_profile(registry, "opencode-zen", model_id)
    assert zen.connections == ("api-key", "account")
    assert zen_wire.protocol == "responses"
    assert zen.capabilities.reasoning.levels == model.capabilities.reasoning.levels
    assert zen.capabilities.tools is True
    if not supports_none:
        assert zen_wire.reasoning.off == "low"
        assert zen.capabilities.json_mode is True
        assert zen_wire.replay.scope == "none"
    openrouter = registry.get("openrouter", f"openai/{model_id}")
    assert openrouter.connections == ("api-key",)
    assert openrouter.capabilities.tools is True
    assert openrouter.context_window == 1_050_000


def test_openai_task_model_overrides_are_limited_to_working_connections(
    registry: ModelRegistry,
) -> None:
    """Override-only task models load; those without a subscription wire are
    api-key only, while ``gpt-image-2`` stays valid for both connections."""

    api_key_only_models = (
        "tts-1",
        "tts-1-hd",
        "gpt-4o-mini-tts",
        "whisper-1",
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
        "dall-e-2",
        "dall-e-3",
        "gpt-image-1",
        "gpt-image-1-mini",
        "gpt-image-1.5",
    )
    for model_id in api_key_only_models:
        model = registry.get("openai", model_id)
        assert model.connections == ("api-key",)
        assert model.allows_connection("api-key") is True
        assert model.allows_connection("subscription") is False
    assert registry.get("openai", "tts-1").capabilities.task_types == (
        "text_to_speech",
        "audio_generation",
    )

    gpt_image_2 = registry.get("openai", "gpt-image-2")
    assert gpt_image_2.connections == ()
    assert gpt_image_2.allows_connection("api-key") is True
    assert gpt_image_2.allows_connection("subscription") is True

    for model_id in ("gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5", "gpt-image-2"):
        model = registry.get("openai", model_id)
        assert model.capabilities.input_modalities == ("image", "text")


@pytest.mark.parametrize(
    ("model_id", "connection_id", "voice_count", "default_voice"),
    [
        ("gpt-live-1", "api-key", 22, "marin"),
        ("gpt-live-1-codex", "subscription", 9, "cove"),
    ],
)
def test_openai_live_voice_models_are_task_only_per_connection(
    registry: ModelRegistry,
    model_id: str,
    connection_id: str,
    voice_count: int,
    default_voice: str,
) -> None:
    """GPT-Live targets are live voice Task Models, never Chat Models."""

    model = registry.get("openai", model_id)
    assert model.connections == (connection_id,)
    assert model.capabilities.task_types == ("live_voice",)
    assert model.capabilities.tools is False
    assert model.max_output_tokens is None
    chat_models = registry.query(ModelQuery(provider_id="openai", tasks=("chat",)))
    assert model_id not in {chat_model.model_id for _, chat_model in chat_models}
    parameters = model.capabilities.task_options["live_voice"]["parameters"]
    assert len(parameters["voice"]["values"]) == voice_count
    assert parameters["voice"]["default"] == default_voice
    assert parameters["backend_model"] == {"type": "model", "default": "gpt-5.6-terra"}
    backend = registry.get("openai", "gpt-5.6-terra")
    assert backend.capabilities.tools is True
    assert backend.allows_connection(connection_id)


def test_xai_grok_voice_is_a_task_only_live_voice_model_without_a_backend_default(
    registry: ModelRegistry,
) -> None:
    """Grok Voice is a live voice Task Model on both xAI Connections.

    It calls the Live app Tools itself, so its backend model defaults to
    none; tool-capable Grok chat Models remain available as backends.
    """

    model = registry.get("xai", "grok-voice-think-fast-2.0")
    assert model.connections == ("api-key", "subscription")
    assert model.capabilities.task_types == ("live_voice",)
    assert model.capabilities.tools is False
    assert model.capabilities.input_modalities == ("audio",)
    assert model.capabilities.output_modalities == ("audio",)
    chat_models = registry.query(ModelQuery(provider_id="xai", tasks=("chat",)))
    assert "grok-voice-think-fast-2.0" not in {chat.model_id for _, chat in chat_models}
    parameters = model.capabilities.task_options["live_voice"]["parameters"]
    voices = parameters["voice"]["values"]
    assert len(voices) == len(set(voices)) == 28
    assert all(voice == voice.lower() for voice in voices)
    assert parameters["voice"]["default"] == "eve"
    assert parameters["backend_model"] == {"type": "model", "default": "", "allow_none": True}
    assert registry.get("xai", "grok-4.6").capabilities.tools is True


def test_anthropic_opus_4_5_override_pins_budget_control(registry: ModelRegistry) -> None:
    """Opus 4.5 exposes an effort ladder but rejects adaptive thinking.

    The canonical layer labels it ``levels``, whose render
    (``thinking: {type: adaptive}``) 400s there. The override forces native
    ``budget`` rendering; with no ``budget_max`` to seed, ``high`` derives the
    absolute fallback budget (16384).
    """

    opus45 = registry.get("anthropic", "claude-opus-4-5-20251101")
    reasoning = opus45.capabilities.reasoning
    assert (reasoning.supported, reasoning.control, reasoning.budget_max) == (True, "budget", None)

    intent = resolve_reasoning_intent(
        supported=reasoning.supported,
        control=reasoning.control,
        levels=reasoning.levels,
        effort="high",
        budget_max=reasoning.budget_max,
    )
    assert (intent.kind, intent.budget_tokens) == ("budget", 16384)


@pytest.mark.parametrize("model_id", ["typesafe/jev-1.13", "~typesafe/jev-latest"])
def test_jev_is_a_decision_target_without_chat_capability(
    registry: ModelRegistry, model_id: str
) -> None:
    model = registry.get("openrouter", model_id)

    assert model.capabilities.input_modalities == ("text",)
    assert model.capabilities.output_modalities == ("decisions",)
    assert model.capabilities.task_types == ("decision",)


# ---------------------------------------------------------------------------
# Custom Provider Models over the bundled layers
# ---------------------------------------------------------------------------


def _custom_provider(model_name: str) -> dict[str, dict[str, object]]:
    return {
        model_name: {
            "models": {
                "chat-model": {
                    "name": model_name,
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": False,
                        "reasoning": False,
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "supported_voices": [],
                        "task_types": ["chat"],
                        "task_options": {},
                    },
                }
            }
        }
    }


def test_custom_provider_registries_do_not_share_the_path_cache(tmp_path: Path) -> None:
    first = ModelRegistry.load(tmp_path, custom_providers=_custom_provider("first"))
    second = ModelRegistry.load(tmp_path, custom_providers=_custom_provider("second"))
    bundled_only = ModelRegistry.load(tmp_path)

    assert first is not second
    assert first.get("first", "chat-model").name == "first"
    assert second.get("second", "chat-model").name == "second"
    assert bundled_only.list_for_provider("first") == []
    assert bundled_only.list_for_provider("second") == []


def test_manual_custom_model_overlays_discovered_record(tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    models_dir.joinpath("local-ai.json").write_text(
        json.dumps(
            {
                "provider_id": "local-ai",
                "models": {
                    "chat-model": {
                        "name": "Discovered",
                        "capabilities": {
                            "vision": False,
                            "tools": False,
                            "json_mode": False,
                            "reasoning": {"supported": False},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    custom: dict[str, Any] = {
        "local-ai": {
            "models": {
                "chat-model": {
                    "name": "Manual",
                    "context_window": 65_536,
                    "max_output_tokens": 2_048,
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": True,
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "supported_voices": [],
                        "task_types": ["chat", "image_understanding"],
                        "task_options": {},
                    },
                }
            }
        }
    }

    registry = ModelRegistry.load(tmp_path, custom_providers=custom)
    held_reference = registry
    model = registry.get("local-ai", "chat-model")

    assert model.name == "Manual"
    assert model.context_window == 65_536
    assert model.capabilities.reasoning.supported is True
    assert model.connections == ("default",)

    custom["local-ai"]["models"]["chat-model"]["name"] = "Updated"
    registry.reload(tmp_path, custom_providers=custom)

    assert held_reference is registry
    assert held_reference.get("local-ai", "chat-model").name == "Updated"
