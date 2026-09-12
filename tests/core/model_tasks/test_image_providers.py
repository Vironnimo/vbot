"""Tests for image providers."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.model_tasks.image_providers import (
    _UNIFIED_IMAGE_KEYS,
    _build_openai_image_payload,
    _build_openrouter_image_payload,
)
from core.model_tasks.image_types import ImageGenerationResult, ImageInput
from core.providers.errors import (
    ProviderError,
    ProviderOutcomeUnknownError,
)
from tests.core.model_tasks.image_providers_test_support import (
    OPENROUTER_IMAGES_URL,
    _openrouter_image_client,
    _unified_image_response,
)


# ---------------------------------------------------------------------------
# OpenRouter payload builder — the heart of the wire-shaping contract
# ---------------------------------------------------------------------------
def test_build_payload_carries_only_model_and_prompt_when_no_options() -> None:
    """An empty options dict produces a request with only ``model`` and
    ``prompt`` — the provider's own defaults take over."""

    payload = _build_openrouter_image_payload("openai/gpt-image-1", "a cat", {})

    assert payload == {"model": "openai/gpt-image-1", "prompt": "a cat"}


def test_build_payload_includes_present_unified_keys_top_level() -> None:
    """Unified image parameters (aspect_ratio, resolution, seed, n, …) are
    sent at the top level of the request when present in options."""

    payload = _build_openrouter_image_payload(
        "black-forest-labs/flux.2-pro",
        "a cat",
        {"aspect_ratio": "16:9", "resolution": "2K", "seed": 12345, "n": 2},
    )

    assert payload == {
        "model": "black-forest-labs/flux.2-pro",
        "prompt": "a cat",
        "aspect_ratio": "16:9",
        "resolution": "2K",
        "seed": 12345,
        "n": 2,
    }


def test_build_payload_ignores_unknown_and_legacy_keys() -> None:
    """Keys outside the unified contract — including legacy ``image_size``
    from a stale stored binding — are dropped."""

    payload = _build_openrouter_image_payload(
        "black-forest-labs/flux.2-pro",
        "a cat",
        {
            "aspect_ratio": "1:1",
            "image_size": "1K",
            "strength": 0.5,
            "some_future_field": "ignored",
        },
    )

    assert payload == {
        "model": "black-forest-labs/flux.2-pro",
        "prompt": "a cat",
        "aspect_ratio": "1:1",
    }


def test_build_payload_drops_empty_placeholder_values() -> None:
    """Empty option placeholders injected by the schema defaults (the
    "Provider default" select value ``""``, empty json objects) are unset
    and must not reach the wire; numeric ``0`` stays."""

    payload = _build_openrouter_image_payload(
        "bytedance-seed/seedream-4.5",
        "a cat",
        {
            "aspect_ratio": "",
            "resolution": "2K",
            "output_compression": 0,
            "seed": None,
            "provider_options": {},
            "extra_options": {},
        },
    )

    assert payload == {
        "model": "bytedance-seed/seedream-4.5",
        "prompt": "a cat",
        "resolution": "2K",
        "output_compression": 0,
    }


def test_build_payload_nests_provider_options() -> None:
    """``provider_options`` becomes the nested ``provider.options`` object —
    the passthrough channel for provider-specific keys (Recraft controls,
    style, text_layout, …)."""

    provider_options = {"recraft": {"style": "vector_illustration", "controls": {"colors": []}}}
    payload = _build_openrouter_image_payload(
        "recraft/recraft-v3",
        "a cat",
        {"n": 2, "provider_options": provider_options},
    )

    assert payload["provider"] == {"options": provider_options}
    assert "provider_options" not in payload


def test_build_payload_adds_non_conflicting_extra_options() -> None:
    """The ``extra_options`` escape hatch adds provider-specific fields."""

    payload = _build_openrouter_image_payload(
        "black-forest-labs/flux.2-pro",
        "a cat",
        {
            "aspect_ratio": "1:1",
            "extra_options": {"guidance": 3.5, "empty": ""},
        },
    )

    assert payload["aspect_ratio"] == "1:1"
    assert payload["guidance"] == 3.5
    assert "empty" not in payload
    assert "extra_options" not in payload


@pytest.mark.parametrize("builder", [_build_openrouter_image_payload, _build_openai_image_payload])
def test_image_payload_rejects_extra_options_model_override(
    builder: Callable[..., object],
) -> None:
    with pytest.raises(ProviderError, match="model"):
        builder(
            "safe-image-model",
            "a cat",
            {"extra_options": {"model": "redirected-image-model"}},
        )


def test_build_payload_encodes_source_images_as_input_references() -> None:
    payload = _build_openrouter_image_payload(
        "openai/gpt-image-1",
        "make it rainy",
        {},
        input_images=(ImageInput(filename="photo.png", media_type="image/png", data=b"source"),),
    )

    assert payload["input_references"] == [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(b"source").decode("ascii")
            },
        }
    ]


def test_unified_image_keys_constant_matches_contract() -> None:
    """The unified key whitelist must match OpenRouter's documented image
    API parameters."""

    assert _UNIFIED_IMAGE_KEYS == (
        "aspect_ratio",
        "resolution",
        "size",
        "quality",
        "output_format",
        "background",
        "output_compression",
        "n",
        "seed",
    )


# ---------------------------------------------------------------------------
# End-to-end OpenRouter call — the payload reaches the wire correctly
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_posts_unified_endpoint() -> None:
    """The request goes to ``POST /images`` with top-level unified
    parameters; the response ``data[].b64_json`` entries are decoded and
    usage is preserved."""

    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json=_unified_image_response(b"hello", usage={"cost": 0.04}),
        )
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    result = await client.generate(
        "a cat",
        options={"aspect_ratio": "16:9", "resolution": "2K"},
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "model": "black-forest-labs/flux.2-pro",
        "prompt": "a cat",
        "aspect_ratio": "16:9",
        "resolution": "2K",
    }

    assert isinstance(result, ImageGenerationResult)
    assert result.images == (b"hello",)
    assert result.media_type == "image/png"
    assert result.model == "black-forest-labs/flux.2-pro"
    assert result.usage == {"cost": 0.04}


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_sends_provider_options() -> None:
    """Provider passthrough options reach the wire as ``provider.options``."""

    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(200, json=_unified_image_response(b"img"))
    )
    client = _openrouter_image_client("recraft/recraft-v3")

    await client.generate(
        "a cat",
        options={
            "n": 2,
            "provider_options": {"recraft": {"style": "any_style"}},
        },
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["provider"] == {"options": {"recraft": {"style": "any_style"}}}
    assert payload["n"] == 2


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_sends_source_images() -> None:
    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(200, json=_unified_image_response(b"edited"))
    )
    client = _openrouter_image_client("openai/gpt-image-1")

    result = await client.generate(
        "make it rainy",
        options={},
        input_images=(
            ImageInput(filename="photo.jpg", media_type="image/jpeg", data=b"jpeg-source"),
        ),
    )

    payload = json.loads(route.calls[0].request.content)
    reference_url = payload["input_references"][0]["image_url"]["url"]
    assert reference_url == (
        "data:image/jpeg;base64," + base64.b64encode(b"jpeg-source").decode("ascii")
    )
    assert result.images == (b"edited",)


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_decodes_multiple_images() -> None:
    """``n > 1`` responses map one-to-one into the ``images`` tuple."""

    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json=_unified_image_response(b"alpha", b"beta", b"gamma"),
        )
    )
    client = _openrouter_image_client("recraft/recraft-v3")

    result = await client.generate("a cat", options={"n": 3})

    assert json.loads(route.calls[0].request.content)["n"] == 3
    assert result.images == (b"alpha", b"beta", b"gamma")


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_media_type_from_entry_or_format() -> None:
    """A per-entry ``media_type`` (vector outputs) wins; otherwise the
    requested ``output_format`` decides; the fallback stays ``image/png``."""

    svg_body = _unified_image_response(b"<svg/>")
    svg_body["data"][0]["media_type"] = "image/svg+xml"
    respx.post(OPENROUTER_IMAGES_URL).mock(return_value=httpx.Response(200, json=svg_body))
    client = _openrouter_image_client("recraft/recraft-v4-vector")

    result = await client.generate("a cat", options={})
    assert result.media_type == "image/svg+xml"

    respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(200, json=_unified_image_response(b"jpg"))
    )
    result = await client.generate("a cat", options={"output_format": "jpeg"})
    assert result.media_type == "image/jpeg"


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_rejects_empty_data() -> None:
    """A malformed success is not replayed because generation may be billed."""

    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(200, json={"created": 1, "data": []})
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    with pytest.raises(ProviderOutcomeUnknownError):
        await client.generate("a cat", options={})

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_does_not_retry_invalid_b64_json() -> None:
    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": "not-base64!"}]},
        )
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    with pytest.raises(ProviderOutcomeUnknownError) as exc_info:
        await client.generate("a cat", options={})

    assert exc_info.value.retryable is False
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_retries_documented_503() -> None:
    route = respx.post(OPENROUTER_IMAGES_URL)
    route.side_effect = [
        httpx.Response(503, text="no provider available"),
        httpx.Response(200, json=_unified_image_response(b"img")),
    ]
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.generate("a cat", options={})

    assert result.images == (b"img",)
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_does_not_retry_ambiguous_502() -> None:
    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(502, text="invalid upstream response")
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    with pytest.raises(ProviderOutcomeUnknownError, match="HTTP 502"):
        await client.generate("a cat", options={})

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_image_generate_uses_bearer_auth_header() -> None:
    """The Authorization header is set from the connection's auth config —
    a guard that the refactor did not drop the auth wiring."""

    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(200, json=_unified_image_response(b"img"))
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    await client.generate("a cat", options={"aspect_ratio": "1:1"})

    assert route.call_count >= 1
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"
    assert route.calls[0].request.headers["x-title"] == "vBot"
