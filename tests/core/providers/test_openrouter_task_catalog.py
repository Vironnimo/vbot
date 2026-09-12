"""Openrouter: task catalog behavior."""

from __future__ import annotations

from typing import Any

import pytest

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._openrouter_catalog import (
    _normalize_image_parameters,
    _passthrough_from_detail,
)
from core.providers.openrouter import (
    OpenRouterAdapter,
)


# Image task-catalog discovery (dedicated image API)
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

    with pytest.raises(ValueError):
        await OpenRouterAdapter.discover_task_models({}, fetch_json)


@pytest.mark.asyncio
async def test_discover_task_models_projects_video_catalog_capabilities() -> None:
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
