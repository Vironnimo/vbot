"""Tests for OpenRouterAdapter."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.models.query import ModelQuery
from core.providers.openrouter import (
    SUPPLEMENTARY_OUTPUT_MODALITIES,
    OpenRouterAdapter,
    _normalize_image_parameters,
    _passthrough_from_detail,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-openrouter-key"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SUCCESS_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]


@pytest.fixture()
def openrouter_config() -> ProviderConfig:
    return ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="OPENROUTER_API_KEY",
                ),
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
    )


@pytest.fixture()
def openrouter_adapter(openrouter_config: ProviderConfig) -> OpenRouterAdapter:
    return OpenRouterAdapter(openrouter_config, API_KEY)


def raw_openrouter_model(
    *,
    input_modalities: list[str] | None = None,
    output_modalities: list[str] | None = None,
    supported_parameters: list[str] | None = None,
    max_completion_tokens: int | None = 64000,
    supported_voices: list[str] | None = None,
) -> dict:
    raw: dict = {
        "id": "anthropic/claude-sonnet-4",
        "name": "Anthropic: Claude Sonnet 4",
        "architecture": {
            "input_modalities": input_modalities or ["text", "image"],
            "output_modalities": output_modalities or ["text"],
            "modality": "text+image->text",
        },
        "supported_parameters": (
            supported_parameters
            if supported_parameters is not None
            else ["tools", "response_format", "reasoning"]
        ),
        "context_length": 128000,
        "top_provider": {"max_completion_tokens": max_completion_tokens},
    }
    if supported_voices is not None:
        raw["supported_voices"] = supported_voices
    return raw


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_reasoning_uses_openrouter_wire_format(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await openrouter_adapter.send(
        SAMPLE_MESSAGES,
        model_id="openai/gpt-5.2",
        thinking_effort="xhigh",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": "xhigh"}
    assert request_body["include_reasoning"] is True
    assert "reasoning_effort" not in request_body


@pytest.mark.parametrize(
    ("thinking_effort", "expected_effort"),
    [("none", "none"), ("max", "xhigh")],
)
@respx.mock
@pytest.mark.asyncio
async def test_openrouter_reasoning_maps_to_nearest_supported_effort(
    openrouter_adapter: OpenRouterAdapter,
    thinking_effort: str,
    expected_effort: str,
) -> None:
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await openrouter_adapter.send(
        SAMPLE_MESSAGES,
        model_id="openai/gpt-5.2",
        thinking_effort=thinking_effort,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": expected_effort}
    assert request_body["include_reasoning"] is True


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_normalizes_explicit_reasoning_effort_kwarg(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await openrouter_adapter.send(
        SAMPLE_MESSAGES,
        model_id="openai/gpt-5.2",
        reasoning_effort="xhigh",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": "xhigh"}
    assert request_body["include_reasoning"] is True
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_suppresses_reasoning_when_catalog_disables_it(
    openrouter_config: ProviderConfig,
) -> None:
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        model_lookup=lambda model_id: Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=128000,
            max_output_tokens=4096,
        ),
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="openai/gpt-4o",
        thinking_effort="high",
        reasoning={"effort": "high"},
        include_reasoning=True,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning" not in request_body
    assert "include_reasoning" not in request_body
    assert "reasoning_effort" not in request_body


def _ladder_lookup(levels: tuple[str, ...]):
    def model_lookup(model_id: str) -> Model:
        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control="levels",
                    levels=levels,
                ),
            ),
            context_window=128000,
            max_output_tokens=4096,
        )

    return model_lookup


@pytest.mark.parametrize(
    ("thinking_effort", "expected_effort"),
    [("max", "xhigh"), ("medium", "high"), ("high", "high")],
)
@respx.mock
@pytest.mark.asyncio
async def test_openrouter_snaps_against_effective_model_ladder(
    openrouter_config: ProviderConfig,
    thinking_effort: str,
    expected_effort: str,
) -> None:
    """A model with a feed ladder snaps within that ladder, not the provider constant.

    ``deepseek/deepseek-v4-pro`` at OpenRouter publishes ``[high, xhigh]``; every
    selection must land inside it (``max`` -> ``xhigh``, ``medium`` -> ``high``).
    The provider constant (which includes ``low``/``medium``) is bypassed.
    """
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        model_lookup=_ladder_lookup(("high", "xhigh")),
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="deepseek/deepseek-v4-pro",
        thinking_effort=thinking_effort,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": expected_effort}
    assert request_body["include_reasoning"] is True


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_falls_back_to_constant_without_ladder(
    openrouter_config: ProviderConfig,
) -> None:
    """A model with an empty feed ladder snaps against the provider constant (floor).

    The constant carries ``low`` (the ladder above does not), so a ``low``
    selection surviving as ``low`` proves the floor path is taken when the
    looked-up model has no ladder.
    """
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        model_lookup=_ladder_lookup(()),
    )

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="some/model-without-ladder",
        thinking_effort="low",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": "low"}


def _control_lookup(control: str):
    def model_lookup(model_id: str) -> Model:
        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True, control=control),
            ),
            context_window=128000,
            max_output_tokens=4096,
        )

    return model_lookup


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_on_off_model_toggles_enabled(
    openrouter_config: ProviderConfig,
) -> None:
    """An ``on_off`` model toggles ``reasoning.enabled`` rather than sending an effort."""
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(openrouter_config, API_KEY, model_lookup=_control_lookup("on_off"))

    await adapter.send(SAMPLE_MESSAGES, model_id="some/toggle-model", thinking_effort="high")

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"enabled": True}
    assert request_body["include_reasoning"] is True


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_on_off_model_disables_on_none(
    openrouter_config: ProviderConfig,
) -> None:
    """An ``on_off`` model sends the native off-shape for a ``none`` selection."""
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(openrouter_config, API_KEY, model_lookup=_control_lookup("on_off"))

    await adapter.send(SAMPLE_MESSAGES, model_id="some/toggle-model", thinking_effort="none")

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"enabled": False}


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_budget_model_renders_as_effort(
    openrouter_config: ProviderConfig,
) -> None:
    """A ``budget`` model renders as an effort — OpenRouter maps effort→budget itself."""
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenRouterAdapter(openrouter_config, API_KEY, model_lookup=_control_lookup("budget"))

    await adapter.send(
        SAMPLE_MESSAGES, model_id="anthropic/claude-opus-4-1", thinking_effort="high"
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning"] == {"effort": "high"}
    assert request_body["include_reasoning"] is True
    assert "budget_tokens" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_stream_requests_usage(openrouter_adapter: OpenRouterAdapter) -> None:
    sse_body = 'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
    route = respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(
            200, text=sse_body, headers={"content-type": "text/event-stream"}
        )
    )

    async for _ in openrouter_adapter.stream(SAMPLE_MESSAGES, model_id="openai/gpt-5.2"):
        pass

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["stream"] is True
    assert request_body["stream_options"] == {"include_usage": True}


def test_reasoning_replay_policy_stays_current_run(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """current_run is the genuinely correct target, not a placeholder.

    OpenRouter's docs frame reasoning preservation as in-run ("useful for tool
    calling"); cross-run replay is undocumented. The in-run hard requirements
    (e.g. Gemini thought signatures) are satisfied because current_run keeps
    reasoning_meta within the run — pinned by the round-trip test below.
    """
    assert openrouter_adapter.reasoning_replay_policy("anthropic/claude-sonnet-4") == "current_run"
    assert openrouter_adapter.reasoning_replay_policy("google/gemini-2.5-pro") == "current_run"


@respx.mock
@pytest.mark.asyncio
async def test_in_run_round_trips_reasoning_details(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """In-run replay must echo reasoning_details unchanged (Gemini upstreams 400 without it)."""
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    reasoning_details = [
        {"type": "reasoning.encrypted", "data": "enc-signature-blob"},
        {"type": "reasoning.text", "text": "step one", "signature": "sig-1"},
    ]
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "Use the tool"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "name": "lookup", "arguments": {"q": "x"}}],
            "reasoning_meta": {"reasoning_details": reasoning_details},
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]

    await openrouter_adapter.send(history, model_id="google/gemini-2.5-pro")

    request_body = json.loads(route.calls.last.request.content)
    assistant_message = request_body["messages"][1]
    assert assistant_message["reasoning_details"] == reasoning_details


def test_normalize_catalog_entry_maps_all_openrouter_fields() -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(raw_openrouter_model(), {"max_tokens": 8192})

    assert model == Model(
        model_id="anthropic/claude-sonnet-4",
        name="Anthropic: Claude Sonnet 4",
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
            input_modalities=("text", "image"),
            output_modalities=("text",),
            supported_parameters=("reasoning", "response_format", "tools"),
            task_types=(
                "chat",
                "text_output",
                "image_input",
                "image_understanding",
            ),
        ),
        context_window=128000,
        max_output_tokens=64000,
        metadata={"openrouter": {"modality": "text+image->text"}},
    )


def test_normalize_catalog_entry_preserves_non_text_outputs() -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(
        raw_openrouter_model(
            input_modalities=["text", "image", "file"],
            output_modalities=["text", "image"],
        ),
        {},
    )

    assert model.capabilities.input_modalities == ("text", "image", "file")
    assert model.capabilities.output_modalities == ("text", "image")
    assert "image_generation" in model.capabilities.task_types
    assert "file_input" in model.capabilities.task_types


def test_normalize_catalog_entry_preserves_unknown_null_max_tokens() -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(
        raw_openrouter_model(max_completion_tokens=None),
        {"max_tokens": 8192},
    )

    assert model.max_output_tokens is None


@pytest.mark.parametrize(
    ("supported_parameters", "tools", "json_mode", "reasoning"),
    [
        (["tools"], True, False, False),
        (["response_format"], False, True, False),
        (["structured_outputs"], False, True, False),
        (["reasoning"], False, False, True),
        (["include_reasoning"], False, False, True),
        ([], False, False, False),
    ],
)
def test_supported_parameters_derive_capabilities(
    supported_parameters: list[str],
    tools: bool,
    json_mode: bool,
    reasoning: bool,
) -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(
        raw_openrouter_model(supported_parameters=supported_parameters),
        {},
    )

    assert model.capabilities.tools is tools
    assert model.capabilities.json_mode is json_mode
    assert model.capabilities.reasoning.supported is reasoning


@pytest.mark.parametrize(
    ("input_modalities", "vision"), [(["text", "image"], True), (["text"], False)]
)
def test_input_modalities_derive_vision(input_modalities: list[str], vision: bool) -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(
        raw_openrouter_model(input_modalities=input_modalities),
        {},
    )

    assert model.capabilities.vision is vision


def test_normalize_catalog_entry_captures_supported_voices() -> None:
    raw = {
        "id": "hexgrad/kokoro-82m",
        "name": "hexgrad: Kokoro 82M",
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": ["speech"],
            "modality": "text->speech",
        },
        "supported_parameters": ["response_format", "seed"],
        "supported_voices": ["af_alloy", "af_aoede", "af_sky"],
        "context_length": 4096,
        "top_provider": {"max_completion_tokens": None},
    }

    model = OpenRouterAdapter.normalize_catalog_entry(raw, {})

    assert model.capabilities.supported_voices == ("af_alloy", "af_aoede", "af_sky")
    assert "text_to_speech" in model.capabilities.task_types


def test_normalize_catalog_entry_defaults_supported_voices_when_absent() -> None:
    model = OpenRouterAdapter.normalize_catalog_entry(raw_openrouter_model(), {})

    assert model.capabilities.supported_voices == ()


def test_normalize_catalog_entry_ignores_malformed_supported_voices() -> None:
    """A non-list/mixed-type ``supported_voices`` value is treated as absent."""

    raw = raw_openrouter_model()
    raw["supported_voices"] = "not-a-list"

    model = OpenRouterAdapter.normalize_catalog_entry(raw, {})

    assert model.capabilities.supported_voices == ()


# ---------------------------------------------------------------------------
# Embedding model discovery (text_embedding)
# ---------------------------------------------------------------------------


def test_supplementary_output_modalities_includes_embeddings() -> None:
    """The ``embeddings`` modality is part of the supplementary fetch list so
    OpenRouter's dedicated text-embedding models are discoverable.
    """

    assert "embeddings" in SUPPLEMENTARY_OUTPUT_MODALITIES
    assert "transcription" in SUPPLEMENTARY_OUTPUT_MODALITIES
    assert "speech" in SUPPLEMENTARY_OUTPUT_MODALITIES
    assert "image" in SUPPLEMENTARY_OUTPUT_MODALITIES
    assert "audio" in SUPPLEMENTARY_OUTPUT_MODALITIES
    assert "video" in SUPPLEMENTARY_OUTPUT_MODALITIES


def test_supplementary_discovery_params_includes_embeddings_query() -> None:
    """``supplementary_discovery_params()`` emits the ``embeddings`` query param."""

    params = OpenRouterAdapter.supplementary_discovery_params()

    assert {"output_modalities": "embeddings"} in params
    # All non-text modalities are present as separate fetches.
    for modality in (
        "transcription",
        "speech",
        "image",
        "audio",
        "video",
        "embeddings",
    ):
        assert {"output_modalities": modality} in params


def test_normalize_catalog_entry_tags_embedding_models_with_text_embedding_task() -> None:
    """An entry with ``output_modalities=embeddings`` is tagged ``text_embedding``
    and preserves context_length and supported_parameters.
    """

    raw = raw_openrouter_model(
        input_modalities=["text"],
        output_modalities=["embeddings"],
        supported_parameters=["response_format"],
    )

    model = OpenRouterAdapter.normalize_catalog_entry(raw, {})

    assert model.capabilities.output_modalities == ("embeddings",)
    assert model.capabilities.task_types == ("text_embedding",)
    assert model.context_window == 128000
    assert model.capabilities.supported_parameters == ("response_format",)
    # Embedding models output vectors, not text/chat.
    assert "chat" not in model.capabilities.task_types
    assert "text_output" not in model.capabilities.task_types


def test_normalize_catalog_entry_zero_context_length_becomes_unknown() -> None:
    """OpenRouter reports ``context_length: 0`` for non-chat models (STT,
    image/video generation). A 0 is no usable window, so it normalizes to None
    (honest unknown) rather than a fake fact (Phase 6)."""

    raw = raw_openrouter_model()
    raw["context_length"] = 0

    model = OpenRouterAdapter.normalize_catalog_entry(raw, {})

    assert model.context_window is None


def test_registry_query_returns_embedding_models_for_text_embedding_task() -> None:
    """A registry built from normalized embedding entries returns those entries
    when queried with ``tasks=("text_embedding",)`` and excludes them from
    chat-only queries.
    """

    embedding_raw = raw_openrouter_model(
        input_modalities=["text"],
        output_modalities=["embeddings"],
    )
    # The helper defaults ``id``/``name``; override to the embedding example ids.
    embedding_raw["id"] = "google/gemini-embedding-2"
    embedding_raw["name"] = "Google: Gemini Embedding 2"

    chat_raw = raw_openrouter_model(
        input_modalities=["text"],
        output_modalities=["text"],
    )

    embedding_model = OpenRouterAdapter.normalize_catalog_entry(embedding_raw, {})
    chat_model = OpenRouterAdapter.normalize_catalog_entry(chat_raw, {})

    registry = ModelRegistry(
        {
            ("openrouter", embedding_model.model_id): embedding_model,
            ("openrouter", chat_model.model_id): chat_model,
        }
    )

    embedding_matches = registry.query(ModelQuery(tasks=("text_embedding",)))
    assert ("openrouter", embedding_model) in embedding_matches
    # Chat-only model must not match the text_embedding filter.
    assert ("openrouter", chat_model) not in embedding_matches

    chat_matches = registry.query(ModelQuery(tasks=("chat",)))
    assert ("openrouter", chat_model) in chat_matches
    assert ("openrouter", embedding_model) not in chat_matches


# ---------------------------------------------------------------------------
# Image task-catalog discovery (dedicated image API)
# ---------------------------------------------------------------------------


def _fake_fetch_json(responses: dict[str, Any]):
    """Build a fetch_json double that serves canned payloads per endpoint."""

    calls: list[str] = []

    async def fetch_json(endpoint: str) -> Any:
        calls.append(endpoint)
        result = responses[endpoint]
        if isinstance(result, Exception):
            raise result
        return result

    fetch_json.calls = calls  # type: ignore[attr-defined]
    return fetch_json


def _chat_catalog_model(model_id: str) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
            input_modalities=("text",),
            output_modalities=("image",),
        ),
        context_window=None,
        max_output_tokens=None,
    )


@pytest.mark.asyncio
async def test_discover_task_models_enriches_existing_model() -> None:
    """A model already discovered through /models gets its typed parameter
    schema and passthrough keys merged into capabilities.task_options."""

    existing = {"recraft/recraft-v3": _chat_catalog_model("recraft/recraft-v3")}
    fetch_json = _fake_fetch_json(
        {
            "/images/models": {
                "data": [
                    {
                        "id": "recraft/recraft-v3",
                        "name": "Recraft V3",
                        "supported_parameters": {
                            "n": {"type": "range", "min": 1, "max": 6},
                            "seed": {"type": "boolean"},
                        },
                    }
                ]
            },
            "/images/models/recraft/recraft-v3/endpoints": {
                "endpoints": [
                    {
                        "provider_slug": "recraft",
                        "allowed_passthrough_parameters": ["style", "controls", "text_layout"],
                    }
                ]
            },
        }
    )

    discovered = await OpenRouterAdapter.discover_task_models(existing, fetch_json)

    model = discovered["recraft/recraft-v3"]
    image_options = model.capabilities.task_options["image_generation"]
    assert image_options["parameters"]["n"] == {"type": "range", "min": 1, "max": 6}
    assert image_options["parameters"]["seed"] == {"type": "boolean"}
    # Loaded task options are frozen: mappings become read-only views and
    # lists become tuples.
    assert dict(image_options["passthrough"]) == {"recraft": ("controls", "style", "text_layout")}
    # The enriched model keeps its chat-catalog identity.
    assert model.name == "recraft/recraft-v3"


@pytest.mark.asyncio
async def test_discover_task_models_creates_minimal_model_for_image_only_entry() -> None:
    """An entry only present in the image API becomes a new minimal model —
    it must appear as an image-generation target even though /models never
    returned it (new image models land exclusively on the image API)."""

    fetch_json = _fake_fetch_json(
        {
            "/images/models": {
                "data": [
                    {
                        "id": "future-lab/pixel-marvel",
                        "name": "Pixel Marvel",
                        "architecture": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["image"],
                        },
                        "supported_parameters": {
                            "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9"]},
                        },
                    }
                ]
            },
            "/images/models/future-lab/pixel-marvel/endpoints": {"endpoints": []},
        }
    )

    discovered = await OpenRouterAdapter.discover_task_models({}, fetch_json)

    model = discovered["future-lab/pixel-marvel"]
    assert model.name == "Pixel Marvel"
    assert model.context_window is None
    assert "image_generation" in model.capabilities.task_types
    parameters = model.capabilities.task_options["image_generation"]["parameters"]
    assert parameters["aspect_ratio"]["values"] == ("1:1", "16:9")


@pytest.mark.asyncio
async def test_discover_task_models_tolerates_detail_fetch_failure() -> None:
    """A failing per-model endpoint-detail fetch degrades to an empty
    passthrough — the typed parameters from the list entry still project."""

    fetch_json = _fake_fetch_json(
        {
            "/images/models": {
                "data": [
                    {
                        "id": "x/y",
                        "name": "XY",
                        "supported_parameters": {
                            "resolution": {"type": "enum", "values": ["1K", "2K"]},
                        },
                    }
                ]
            },
            "/images/models/x/y/endpoints": ValueError("boom"),
        }
    )

    discovered = await OpenRouterAdapter.discover_task_models({}, fetch_json)

    image_options = discovered["x/y"].capabilities.task_options["image_generation"]
    assert "passthrough" not in image_options
    assert image_options["parameters"]["resolution"]["values"] == ("1K", "2K")


@pytest.mark.asyncio
async def test_discover_task_models_rejects_malformed_catalog() -> None:
    """A payload without a data list is a hard error the discovery layer
    downgrades to a warning (catalog without task options)."""

    fetch_json = _fake_fetch_json({"/images/models": {"unexpected": True}})

    with pytest.raises(ValueError, match="data list"):
        await OpenRouterAdapter.discover_task_models({}, fetch_json)


def test_normalize_image_parameters_shapes() -> None:
    """Known spec shapes validate strictly; unknown types project verbatim;
    shapeless entries drop."""

    parameters = _normalize_image_parameters(
        {
            "aspect_ratio": {"type": "enum", "values": ["1:1", "4:3"]},
            "n": {"type": "range", "min": 1, "max": 4},
            "seed": {"type": "boolean"},
            "future": {"type": "matrix", "rows": 2},
            "bad_enum": {"type": "enum", "values": [1, 2]},
            "shapeless": "nope",
            "typeless": {"values": ["a"]},
        }
    )

    assert parameters["aspect_ratio"] == {"type": "enum", "values": ["1:1", "4:3"]}
    assert parameters["n"] == {"type": "range", "min": 1, "max": 4}
    assert parameters["seed"] == {"type": "boolean"}
    assert parameters["future"] == {"type": "matrix", "rows": 2}
    assert "bad_enum" not in parameters
    assert "shapeless" not in parameters
    assert "typeless" not in parameters


def test_passthrough_from_detail_merges_endpoints_per_slug() -> None:
    """Multiple endpoints of the same upstream provider union their key
    lists deterministically (sorted slugs, sorted keys)."""

    passthrough = _passthrough_from_detail(
        {
            "endpoints": [
                {"provider_slug": "recraft", "allowed_passthrough_parameters": ["style"]},
                {"provider_slug": "recraft", "allowed_passthrough_parameters": ["controls"]},
                {"provider_slug": "other", "allowed_passthrough_parameters": ["zoom", "angle"]},
                {"provider_slug": "", "allowed_passthrough_parameters": ["ignored"]},
            ]
        }
    )

    assert passthrough == {
        "other": ["angle", "zoom"],
        "recraft": ["controls", "style"],
    }
