"""OpenRouter catalogs: ``/models`` normalization, supplementary discovery, and the
dedicated image and video task catalogs."""

from __future__ import annotations

from typing import Any

import pytest

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openrouter import OpenRouterAdapter


def raw_openrouter_model(
    *,
    input_modalities: list[str] | None = None,
    output_modalities: list[str] | None = None,
    supported_parameters: list[str] | None = None,
    max_completion_tokens: int | None = 64000,
) -> dict[str, Any]:
    return {
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


def test_catalog_entry_maps_all_openrouter_fields() -> None:
    raw = raw_openrouter_model()
    raw["reasoning"] = {"mandatory": True, "supported_efforts": ["low", "high"]}

    model = OpenRouterAdapter.normalize_catalog_entry(raw, {"max_tokens": 8192})

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
            task_types=("chat", "text_output", "image_input", "image_understanding"),
        ),
        context_window=128000,
        max_output_tokens=64000,
        metadata={"openrouter": {"modality": "text+image->text", "reasoning_mandatory": True}},
    )


def test_unknown_limits_stay_unknown_instead_of_copying_defaults() -> None:
    raw = raw_openrouter_model(max_completion_tokens=None)
    # Non-chat Models report a zero window, which is no usable window.
    raw["context_length"] = 0

    model = OpenRouterAdapter.normalize_catalog_entry(raw, {"max_tokens": 8192})

    assert model.max_output_tokens is None
    assert model.context_window is None


@pytest.mark.parametrize(
    ("input_modalities", "supported_parameters", "vision", "tools", "json_mode", "reasoning"),
    [
        (
            ["text", "image"],
            ["tools", "structured_outputs", "include_reasoning"],
            True,
            True,
            True,
            True,
        ),
        (["text"], ["response_format", "reasoning"], False, False, True, True),
        (["text"], [], False, False, False, False),
    ],
)
def test_modalities_and_supported_parameters_derive_capabilities(
    input_modalities: list[str],
    supported_parameters: list[str],
    vision: bool,
    tools: bool,
    json_mode: bool,
    reasoning: bool,
) -> None:
    capabilities = OpenRouterAdapter.normalize_catalog_entry(
        raw_openrouter_model(
            input_modalities=input_modalities, supported_parameters=supported_parameters
        ),
        {},
    ).capabilities

    assert capabilities.vision is vision
    assert capabilities.tools is tools
    assert capabilities.json_mode is json_mode
    assert capabilities.reasoning.supported is reasoning


@pytest.mark.parametrize(
    ("input_modalities", "output_modalities", "present", "absent"),
    [
        pytest.param(
            ["text", "image", "file"],
            ["text", "image"],
            {"image_generation", "file_input"},
            set(),
            id="image-output-and-file-input",
        ),
        pytest.param(
            ["text", "image"],
            ["text", "audio"],
            {"music_generation", "audio_generation"},
            set(),
            id="music-signature",
        ),
        pytest.param(
            ["text", "audio"],
            ["text", "audio"],
            {"audio_generation"},
            {"music_generation"},
            id="general-audio-is-not-music",
        ),
        pytest.param(["text"], ["speech"], {"text_to_speech"}, set(), id="speech"),
        pytest.param(
            ["text"],
            ["embeddings"],
            {"text_embedding"},
            {"chat", "text_output"},
            id="embeddings-output-vectors-not-chat",
        ),
    ],
)
def test_output_modalities_derive_task_types(
    input_modalities: list[str],
    output_modalities: list[str],
    present: set[str],
    absent: set[str],
) -> None:
    raw = raw_openrouter_model(
        input_modalities=input_modalities, output_modalities=output_modalities
    )
    raw["architecture"]["modality"] = f"{'+'.join(input_modalities)}->{'+'.join(output_modalities)}"

    model = OpenRouterAdapter.normalize_catalog_entry(raw, {})

    assert model.capabilities.output_modalities == tuple(output_modalities)
    assert present <= set(model.capabilities.task_types)
    assert not absent & set(model.capabilities.task_types)


@pytest.mark.parametrize(
    ("output_modalities", "prompt", "expected_input"),
    [
        pytest.param(["embeddings"], "0.00000002", 0.02, id="per-token-to-per-million"),
        pytest.param(["embeddings"], "0", 0.0, id="free-is-a-real-zero"),
        pytest.param(["embeddings"], "-1", None, id="variable-price-router"),
        pytest.param(["embeddings"], "n/a", None, id="malformed"),
        # Chat Models keep the models.dev price the refresh projects.
        pytest.param(["text"], "0.000003", None, id="chat-model"),
    ],
)
def test_embedding_models_carry_their_catalog_input_price(
    output_modalities: list[str], prompt: str, expected_input: float | None
) -> None:
    raw = raw_openrouter_model(output_modalities=output_modalities)
    raw["pricing"] = {"prompt": prompt, "completion": "0"}

    model = OpenRouterAdapter.normalize_catalog_entry(raw, {})

    if expected_input is None:
        assert model.pricing is None
    else:
        assert model.pricing is not None
        assert model.pricing.source == "openrouter:anthropic/claude-sonnet-4"
        assert (model.pricing.rates.input, model.pricing.rates.output) == (expected_input, None)


@pytest.mark.parametrize(
    ("supported_voices", "expected"),
    [
        (["af_alloy", "af_aoede", "af_sky"], ("af_alloy", "af_aoede", "af_sky")),
        pytest.param(None, (), id="absent"),
        pytest.param("not-a-list", (), id="malformed"),
    ],
)
def test_supported_voices_are_read_defensively(supported_voices: Any, expected: tuple) -> None:
    raw = raw_openrouter_model(output_modalities=["speech"])
    if supported_voices is not None:
        raw["supported_voices"] = supported_voices

    model = OpenRouterAdapter.normalize_catalog_entry(raw, {})

    assert model.capabilities.supported_voices == expected


def test_supplementary_discovery_fetches_every_non_text_output_modality() -> None:
    # The default /models response returns only text-output Models.
    assert OpenRouterAdapter.supplementary_discovery_params() == [
        {"output_modalities": modality}
        for modality in (
            "transcription",
            "speech",
            "image",
            "audio",
            "video",
            "embeddings",
            "decisions",
        )
    ]


def _fake_fetch_json(responses: dict[str, Any]):
    """A fetch_json double that serves canned payloads (or raises) per endpoint."""

    async def fetch_json(endpoint: str) -> Any:
        result = responses[endpoint]
        if isinstance(result, Exception):
            raise result
        return result

    return fetch_json


def _image_output_model(model_id: str) -> Model:
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
async def test_image_catalog_enriches_existing_model_with_parameters_and_passthrough() -> None:
    existing = {"recraft/recraft-v3": _image_output_model("recraft/recraft-v3")}
    fetch_json = _fake_fetch_json(
        {
            "/images/models": {
                "data": [
                    {
                        "id": "recraft/recraft-v3",
                        "name": "Recraft V3",
                        "supported_parameters": {
                            "aspect_ratio": {"type": "enum", "values": ["1:1", "4:3"]},
                            "n": {"type": "range", "min": 1, "max": 6},
                            "seed": {"type": "boolean"},
                            "future": {"type": "matrix", "rows": 2},
                            "bad_enum": {"type": "enum", "values": [1, 2]},
                            "shapeless": "nope",
                            "typeless": {"values": ["a"]},
                        },
                    }
                ]
            },
            "/images/models/recraft/recraft-v3/endpoints": {
                "endpoints": [
                    {"provider_slug": "recraft", "allowed_passthrough_parameters": ["style"]},
                    {
                        "provider_slug": "recraft",
                        "allowed_passthrough_parameters": ["controls", "text_layout"],
                    },
                    {"provider_slug": "other", "allowed_passthrough_parameters": ["zoom", "angle"]},
                    {"provider_slug": "", "allowed_passthrough_parameters": ["ignored"]},
                ]
            },
        }
    )

    discovered = await OpenRouterAdapter.discover_task_models(existing, fetch_json)

    model = discovered["recraft/recraft-v3"]
    image_options = model.capabilities.task_options["image_generation"]
    # Known shapes validate strictly, unknown types project verbatim, shapeless
    # entries drop; loaded options are frozen (lists become tuples).
    parameters = image_options["parameters"]
    assert parameters["aspect_ratio"] == {"type": "enum", "values": ("1:1", "4:3")}
    assert parameters["n"] == {"type": "range", "min": 1, "max": 6}
    assert parameters["seed"] == {"type": "boolean"}
    assert parameters["future"] == {"type": "matrix", "rows": 2}
    assert set(parameters) == {"aspect_ratio", "n", "seed", "future"}
    # Endpoints of one upstream Provider union their keys deterministically.
    assert dict(image_options["passthrough"]) == {
        "other": ("angle", "zoom"),
        "recraft": ("controls", "style", "text_layout"),
    }
    # The enriched Model keeps its chat-catalog identity.
    assert model.name == "recraft/recraft-v3"


@pytest.mark.asyncio
async def test_image_catalog_creates_minimal_model_for_image_only_entry() -> None:
    """New image Models land exclusively on the image API, never on /models."""

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
async def test_image_catalog_tolerates_endpoint_detail_failure() -> None:
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
async def test_image_catalog_without_data_list_is_a_hard_error() -> None:
    """The discovery layer downgrades this to a warning (catalog without task options)."""

    fetch_json = _fake_fetch_json({"/images/models": {"unexpected": True}})

    with pytest.raises(ValueError):
        await OpenRouterAdapter.discover_task_models({}, fetch_json)


@pytest.mark.asyncio
async def test_video_catalog_projects_exact_video_controls() -> None:
    fetch_json = _fake_fetch_json(
        {
            "/images/models": {"data": []},
            "/videos/models": {
                "data": [
                    {
                        "id": "black-forest-labs/flux-3-video",
                        "name": "FLUX.3 Video",
                        "supported_resolutions": ["720p", "1080p"],
                        "supported_aspect_ratios": ["16:9", "9:16"],
                        "supported_sizes": None,
                        "supported_durations": [5, 10],
                        "supported_frame_images": ["first_frame", "last_frame"],
                        "generate_audio": True,
                        "seed": False,
                        "allowed_passthrough_parameters": ["safety_tolerance"],
                    }
                ]
            },
        }
    )

    discovered = await OpenRouterAdapter.discover_task_models({}, fetch_json)

    model = discovered["black-forest-labs/flux-3-video"]
    assert model.capabilities.task_types == ("video_generation",)
    options = model.capabilities.task_options["video_generation"]
    assert options["parameters"]["resolution"]["values"] == ("720p", "1080p")
    assert options["parameters"]["duration"]["values"] == ("5", "10")
    assert options["parameters"]["generate_audio"] == {"type": "boolean"}
    assert "seed" not in options["parameters"]
    assert options["frame_images"] == ("first_frame", "last_frame")
    assert options["passthrough_parameters"] == ("safety_tolerance",)
