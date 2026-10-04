"""Image profiles: what a Model offers in Settings and per call on each wire, and how a
per-call choice becomes the wire options that override the Settings."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.model_tasks import TASK_IMAGE_GENERATION
from core.model_tasks.image_profile import (
    ImageCallOptionError,
    ImageWire,
    build_image_profile,
)


def _model(parameters: dict[str, Any], *, edits: bool = True) -> Any:
    return SimpleNamespace(
        capabilities=SimpleNamespace(
            task_options={TASK_IMAGE_GENERATION: {"parameters": parameters}},
            input_modalities=("text", "image") if edits else ("text",),
        )
    )


_GPT_IMAGE = _model(
    {
        "size": {"type": "string"},
        "quality": {"type": "enum", "values": ["low", "medium", "high"]},
        "background": {"type": "enum", "values": ["auto", "transparent", "opaque"]},
        "n": {"type": "range", "min": 1, "max": 10},
        "response_format": {"type": "enum", "values": ["url", "b64_json"]},
    }
)
_DALL_E = _model(
    {
        "size": {"type": "enum", "values": ["1024x1024", "1792x1024", "1024x1792"]},
        "response_format": {"type": "enum", "values": ["url", "b64_json"]},
    },
    edits=False,
)
_OPENROUTER = _model(
    {
        "aspect_ratio": {"type": "enum", "values": ["auto", "1:1", "16:9"]},
        "resolution": {"type": "enum", "values": ["1K"]},
        "input_references": {"type": "range", "max": 3},
        "seed": {"type": "boolean"},
    }
)


@pytest.mark.parametrize(
    ("model", "wire", "settings", "choices", "max_sources", "fixed"),
    [
        pytest.param(
            _GPT_IMAGE,
            "openai_images",
            {"size", "quality", "background", "n"},
            {
                "aspect_ratio": ("1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9"),
                "resolution": ("1K", "2K", "4K"),
                "background": ("transparent", "opaque"),
            },
            16,
            {"response_format": "b64_json"},
            id="gpt-image-api",
        ),
        # The subscription carrier makes one image and picks the backend model itself.
        pytest.param(
            _GPT_IMAGE,
            "openai_subscription",
            {"size", "quality", "background"},
            {
                "aspect_ratio": ("1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9"),
                "resolution": ("1K", "2K", "4K"),
                "background": ("transparent", "opaque"),
            },
            None,
            {},
            id="gpt-image-subscription",
        ),
        # Fixed sizes are offered under the common ratio they approximate.
        pytest.param(
            _DALL_E,
            "openai_images",
            {"size"},
            {"aspect_ratio": ("1:1", "16:9", "9:16")},
            0,
            {"response_format": "b64_json"},
            id="fixed-sizes",
        ),
        # "auto" is the Settings default, and a single value is no choice.
        pytest.param(
            _OPENROUTER,
            "openrouter",
            {"aspect_ratio", "resolution", "seed"},
            {"aspect_ratio": ("1:1", "16:9")},
            3,
            {},
            id="openrouter",
        ),
        pytest.param(None, "openrouter", set(), {}, 0, {}, id="unknown-model"),
        pytest.param(_GPT_IMAGE, "unsupported", set(), {}, 0, {}, id="unsupported-provider"),
    ],
)
def test_profile_offers_what_the_model_takes_on_its_wire(
    model: Any,
    wire: ImageWire,
    settings: set[str],
    choices: dict[str, tuple[str, ...]],
    max_sources: int | None,
    fixed: dict[str, str],
) -> None:
    profile = build_image_profile(model, wire)

    assert set(profile.parameters) == settings
    assert dict(profile.call_choices) == choices
    assert profile.max_source_images == max_sources
    assert dict(profile.fixed_options) == fixed


def test_flexible_size_settings_offer_presets() -> None:
    size = build_image_profile(_GPT_IMAGE, "openai_images").parameters["size"]

    assert size["type"] == "enum"
    assert {"auto", "1024x1024", "1536x1024", "1024x1536", "3840x2160"} <= set(size["values"])


@pytest.mark.parametrize(
    ("model", "wire", "call_options", "settings", "wire_options"),
    [
        # A missing tier or ratio comes from the configured size.
        pytest.param(
            _GPT_IMAGE,
            "openai_images",
            {"aspect_ratio": "16:9"},
            {"size": "2048x2048"},
            {"size": "2816x1584"},
            id="ratio-at-configured-tier",
        ),
        pytest.param(
            _GPT_IMAGE,
            "openai_subscription",
            {"resolution": "4k"},
            {"size": "1536x1024"},
            {"size": "3504x2336"},
            id="tier-at-configured-ratio",
        ),
        pytest.param(
            _GPT_IMAGE,
            "openai_images",
            {"aspect_ratio": "9 / 16", "background": "Transparent"},
            {"size": "auto"},
            {"size": "720x1280", "background": "transparent"},
            id="default-tier-and-tolerant-spelling",
        ),
        pytest.param(
            _DALL_E,
            "openai_images",
            {"aspect_ratio": "16x9"},
            {"size": "1024x1024"},
            {"size": "1792x1024"},
            id="fixed-size",
        ),
        pytest.param(
            _OPENROUTER,
            "openrouter",
            {"aspect_ratio": "16:9", "resolution": ""},
            {"aspect_ratio": "1:1"},
            {"aspect_ratio": "16:9"},
            id="native-choice",
        ),
        pytest.param(_GPT_IMAGE, "openai_images", {}, {"size": "auto"}, {}, id="no-choice"),
    ],
)
def test_call_choices_become_wire_options(
    model: Any,
    wire: ImageWire,
    call_options: dict[str, str],
    settings: dict[str, str],
    wire_options: dict[str, str],
) -> None:
    assert build_image_profile(model, wire).wire_options(call_options, settings) == wire_options


@pytest.mark.parametrize(
    ("call_options", "message"),
    [
        pytest.param(
            {"aspect_ratio": "5:4"},
            "aspect_ratio '5:4' is not offered by the configured image model; choose one of: "
            "1:1, 16:9.",
            id="value",
        ),
        pytest.param(
            {"resolution": "2K"},
            "The configured image model has no resolution choice; omit resolution.",
            id="option",
        ),
    ],
)
def test_choices_the_model_lacks_are_refused(call_options: dict[str, str], message: str) -> None:
    profile = build_image_profile(_OPENROUTER, "openrouter")

    with pytest.raises(ImageCallOptionError) as caught:
        profile.wire_options(call_options, {})

    assert str(caught.value) == message
