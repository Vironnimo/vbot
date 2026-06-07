"""Tests for provider-backed image HTTP clients and payload shaping."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.image.providers import (
    _IMAGE_CONFIG_KEYS,
    ProviderImageClient,
    _build_openrouter_image_payload,
)
from core.image.types import ImageGenerationResult
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

# ---------------------------------------------------------------------------
# Payload builder — the heart of the wire-shaping contract
# ---------------------------------------------------------------------------


def test_build_payload_omits_image_config_when_no_options_present() -> None:
    """An empty options dict produces a request without ``image_config`` and
    without a top-level ``seed``. The provider's own defaults take over."""

    payload = _build_openrouter_image_payload(
        "openai/gpt-image-1",
        "a cat",
        {},
    )

    assert payload == {
        "model": "openai/gpt-image-1",
        "messages": [{"role": "user", "content": "a cat"}],
        "modalities": ["image"],
    }
    assert "image_config" not in payload
    assert "seed" not in payload


def test_build_payload_includes_universal_aspect_ratio_and_size() -> None:
    """``aspect_ratio`` and ``image_size`` are sent under ``image_config``
    when present — the universal OpenRouter image fields."""

    payload = _build_openrouter_image_payload(
        "black-forest-labs/flux.2-pro",
        "a cat",
        {"aspect_ratio": "16:9", "image_size": "2K"},
    )

    assert payload["image_config"] == {"aspect_ratio": "16:9", "image_size": "2K"}


def test_build_payload_sends_seed_at_top_level() -> None:
    """``seed`` is a top-level field on the chat/completions request — it
    must NOT be nested under ``image_config`` (that was the previous bug)."""

    payload = _build_openrouter_image_payload(
        "black-forest-labs/flux.2-pro",
        "a cat",
        {"aspect_ratio": "1:1", "image_size": "1K", "seed": 12345},
    )

    assert payload["seed"] == 12345
    assert "seed" not in payload["image_config"]
    assert payload["image_config"] == {"aspect_ratio": "1:1", "image_size": "1K"}


def test_build_payload_omits_seed_when_absent() -> None:
    """When ``seed`` is not in options, it is not sent at the top level."""

    payload = _build_openrouter_image_payload(
        "recraft/recraft-v3",
        "a cat",
        {"aspect_ratio": "1:1", "image_size": "1K"},
    )

    assert "seed" not in payload


def test_build_payload_gemini_half_k_size_passes_through() -> None:
    """``0.5K`` is a Gemini-3.1-flash-image-only image_size that the wire
    must pass through unchanged when the user has selected it."""

    payload = _build_openrouter_image_payload(
        "google/gemini-3.1-flash-image-preview",
        "a cat",
        {"aspect_ratio": "1:1", "image_size": "0.5K", "seed": 7},
    )

    assert payload["image_config"] == {"aspect_ratio": "1:1", "image_size": "0.5K"}
    assert payload["seed"] == 7


def test_build_payload_recraft_style_and_rgb_colors_passthrough() -> None:
    """Recraft family fields (``style`` text + ``rgb_colors`` array) are
    passed through under ``image_config`` when present. The
    ``rgb_colors`` array keeps its nested structure verbatim."""

    rgb_colors = [[0, 128, 255], [255, 0, 0]]
    payload = _build_openrouter_image_payload(
        "recraft/recraft-v3",
        "a cat",
        {
            "aspect_ratio": "1:1",
            "image_size": "1K",
            "strength": 0.5,
            "style": "any_style",
            "rgb_colors": rgb_colors,
            "background_rgb_color": [255, 255, 255],
            "text_layout": [{"text": "hi", "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
        },
    )

    assert payload["image_config"] == {
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "strength": 0.5,
        "style": "any_style",
        "rgb_colors": rgb_colors,
        "background_rgb_color": [255, 255, 255],
        "text_layout": [{"text": "hi", "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
    }
    assert "seed" not in payload


def test_build_payload_sourceful_v2_5_fields_passthrough() -> None:
    """Sourceful v2.5 fields (``font_inputs``, ``scoring_rubric``,
    ``scoring_prompt``, ``background_mode``, ``background_hex_color``) are
    passed through with their JSON-typed structures intact."""

    font_inputs = [{"font_url": "https://example.com/font.ttf", "text": "Hello"}]
    scoring_rubric = [
        {
            "key": "clarity",
            "label": "Clarity",
            "description": "Visual clarity",
            "weight": 0.6,
            "passing_score": 0.5,
        }
    ]

    payload = _build_openrouter_image_payload(
        "sourceful/riverflow-v2.5-pro",
        "a cat",
        {
            "aspect_ratio": "1:1",
            "image_size": "1K",
            "font_inputs": font_inputs,
            "scoring_prompt": "Score this image",
            "scoring_rubric": scoring_rubric,
            "background_mode": "solid",
            "background_hex_color": "#FFFFFF",
        },
    )

    assert payload["image_config"] == {
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "font_inputs": font_inputs,
        "scoring_prompt": "Score this image",
        "scoring_rubric": scoring_rubric,
        "background_mode": "solid",
        "background_hex_color": "#FFFFFF",
    }


def test_build_payload_sourceful_v2_super_resolution_references() -> None:
    """Sourceful v2 ``super_resolution_references`` is a v2-only field —
    an array of URL strings — and must be passed through unchanged."""

    refs = [
        "https://example.com/ref1.png",
        "https://example.com/ref2.png",
    ]
    payload = _build_openrouter_image_payload(
        "sourceful/riverflow-v2",
        "a cat",
        {
            "aspect_ratio": "1:1",
            "image_size": "1K",
            "font_inputs": [],
            "super_resolution_references": refs,
        },
    )

    assert payload["image_config"]["super_resolution_references"] == refs
    # v2.5-only fields stay out when only v2 fields are provided.
    assert "scoring_prompt" not in payload["image_config"]
    assert "scoring_rubric" not in payload["image_config"]
    assert "background_mode" not in payload["image_config"]


def test_build_payload_ignores_unknown_keys() -> None:
    """Keys outside the known image_config set and the top-level ``seed``
    are dropped — the wire only carries the contract we know about."""

    payload = _build_openrouter_image_payload(
        "black-forest-labs/flux.2-pro",
        "a cat",
        {
            "aspect_ratio": "1:1",
            "image_size": "1K",
            "extra_unsupported_field": "ignored",
            "n": 4,
            "quality": "hd",
        },
    )

    assert payload["image_config"] == {"aspect_ratio": "1:1", "image_size": "1K"}
    assert "extra_unsupported_field" not in payload
    assert "n" not in payload
    assert "quality" not in payload


def test_build_payload_does_not_invent_defaults_for_absent_universal_keys() -> None:
    """When the universal ``aspect_ratio`` and ``image_size`` are absent
    from options, the wire does not invent defaults. The provider's own
    defaults (1:1, 1K) are relied on instead."""

    payload = _build_openrouter_image_payload(
        "recraft/recraft-v3",
        "a cat",
        {"style": "any_style"},
    )

    # ``style`` is present, universal keys are not — image_config only
    # contains the keys actually present in options.
    assert payload["image_config"] == {"style": "any_style"}
    assert "aspect_ratio" not in payload["image_config"]
    assert "image_size" not in payload["image_config"]


def test_build_payload_does_not_send_seed_under_image_config() -> None:
    """Even if a caller mistakenly places ``seed`` under image_config-shaped
    data, the helper keys off the top-level options dict — the wire stays
    correct. This is a regression guard for the historical bug where
    aspect_ratio/image_size were the only fields sent."""

    payload = _build_openrouter_image_payload(
        "black-forest-labs/flux.2-pro",
        "a cat",
        {"aspect_ratio": "1:1", "image_size": "1K", "seed": 99},
    )

    assert "seed" not in payload["image_config"]
    assert payload["seed"] == 99


def test_build_payload_preserves_explicit_none_for_seed() -> None:
    """An explicit ``seed: None`` in options is still considered present —
    the wire sends ``null``. The provider decides what to do with it."""

    payload = _build_openrouter_image_payload(
        "black-forest-labs/flux.2-pro",
        "a cat",
        {"aspect_ratio": "1:1", "image_size": "1K", "seed": None},
    )

    assert "seed" in payload
    assert payload["seed"] is None


def test_image_config_keys_constant_matches_plan() -> None:
    """The image_config key whitelist must match the spec in the plan."""

    assert _IMAGE_CONFIG_KEYS == (
        "aspect_ratio",
        "image_size",
        "strength",
        "style",
        "rgb_colors",
        "background_rgb_color",
        "text_layout",
        "font_inputs",
        "super_resolution_references",
        "scoring_prompt",
        "scoring_rubric",
        "background_mode",
        "background_hex_color",
    )


# ---------------------------------------------------------------------------
# End-to-end OpenRouter call — the payload reaches the wire correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_sends_universal_image_config() -> None:
    """The HTTP request body carries the merged image_config as JSON when
    only the universal fields are present. Result is normalized."""

    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1},
            },
        )
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    result = await client.generate(
        "a cat",
        options={"aspect_ratio": "16:9", "image_size": "2K"},
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["model"] == "black-forest-labs/flux.2-pro"
    assert payload["messages"] == [{"role": "user", "content": "a cat"}]
    assert payload["modalities"] == ["image"]
    assert payload["image_config"] == {"aspect_ratio": "16:9", "image_size": "2K"}
    assert "seed" not in payload

    # Normalized result contract: bytes, media type, model, usage, raw.
    assert isinstance(result, ImageGenerationResult)
    assert result.images == (b"hello",)
    assert result.media_type == "image/png"
    assert result.model == "black-forest-labs/flux.2-pro"
    assert result.usage == {"prompt_tokens": 1}


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_sends_top_level_seed() -> None:
    """When ``seed`` is in options, the wire carries it at the top level of
    the request body. ``image_config`` does not contain it."""

    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
                        }
                    }
                ]
            },
        )
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    await client.generate(
        "a cat",
        options={"aspect_ratio": "1:1", "image_size": "1K", "seed": 42},
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["seed"] == 42
    assert "seed" not in payload["image_config"]


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_sends_recraft_family_fields() -> None:
    """A Recraft request with style + rgb_colors + text_layout hits the
    wire with the full Recraft image_config and no top-level seed."""

    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
                        }
                    }
                ]
            },
        )
    )
    client = _openrouter_image_client("recraft/recraft-v3")

    rgb_colors = [[0, 128, 255], [255, 0, 0]]
    text_layout = [{"text": "label", "bbox": [[0, 0], [0.5, 0], [0.5, 0.2], [0, 0.2]]}]
    await client.generate(
        "a cat",
        options={
            "aspect_ratio": "1:1",
            "image_size": "1K",
            "strength": 0.5,
            "style": "any_style",
            "rgb_colors": rgb_colors,
            "background_rgb_color": [255, 255, 255],
            "text_layout": text_layout,
        },
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["image_config"] == {
        "aspect_ratio": "1:1",
        "image_size": "1K",
        "strength": 0.5,
        "style": "any_style",
        "rgb_colors": rgb_colors,
        "background_rgb_color": [255, 255, 255],
        "text_layout": text_layout,
    }
    assert "seed" not in payload


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_sends_gemini_half_k_and_seed() -> None:
    """A Gemini 3.1 flash image request with ``0.5K`` and ``seed`` sends
    the size inside ``image_config`` and the seed at the top level."""

    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
                        }
                    }
                ]
            },
        )
    )
    client = _openrouter_image_client("google/gemini-3.1-flash-image-preview")

    await client.generate(
        "a cat",
        options={"aspect_ratio": "1:1", "image_size": "0.5K", "seed": 12345},
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["image_config"] == {"aspect_ratio": "1:1", "image_size": "0.5K"}
    assert payload["seed"] == 12345


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_omits_image_config_when_no_options() -> None:
    """When no image_config keys are in options, the wire omits the
    ``image_config`` object entirely (so the provider's own defaults
    take effect)."""

    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
                        }
                    }
                ]
            },
        )
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    await client.generate("a cat", options={})

    payload = json.loads(route.calls[0].request.content)
    assert "image_config" not in payload
    assert "seed" not in payload
    # The mandatory framing fields are still present.
    assert payload["model"] == "black-forest-labs/flux.2-pro"
    assert payload["modalities"] == ["image"]


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_uses_bearer_auth_header() -> None:
    """The Authorization header is set from the connection's auth config —
    a guard that the refactor did not drop the auth wiring."""

    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
                        }
                    }
                ]
            },
        )
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    await client.generate("a cat", options={"aspect_ratio": "1:1", "image_size": "1K"})

    # The respx mock captures the last call; check its auth header.
    # (We re-assert via route to keep the contract explicit.)
    route = respx.post("https://openrouter.ai/api/v1/chat/completions")
    assert route.call_count >= 1
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openrouter_image_client(model_id: str) -> ProviderImageClient:
    """Build a ProviderImageClient wired to a mockable OpenRouter endpoint."""

    provider = ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[],
        extra_headers={"X-Title": "vBot"},
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="OPENROUTER_API_KEY",
        ),
    )
    return ProviderImageClient(
        provider=provider,
        connection=connection,
        credential="sk-test",
        model_id=model_id,
    )
