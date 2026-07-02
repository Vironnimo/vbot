"""Tests for provider-backed image HTTP clients and payload shaping."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from core.model_tasks.image_providers import (
    _OPENAI_IMAGE_KEYS,
    _UNIFIED_IMAGE_KEYS,
    ProviderImageClient,
    _build_openai_image_payload,
    _build_openrouter_image_payload,
)
from core.model_tasks.image_types import ImageGenerationResult
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"


def _unified_image_response(*image_bytes: bytes, usage: dict | None = None) -> dict:
    """Build an OpenRouter unified image API response body."""

    body: dict = {
        "created": 1,
        "data": [
            {"b64_json": base64.b64encode(payload).decode("ascii")} for payload in image_bytes
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


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


def test_build_payload_merges_extra_options_last() -> None:
    """The ``extra_options`` escape hatch merges into the top-level payload
    and wins over authored keys — it is the user's last word."""

    payload = _build_openrouter_image_payload(
        "black-forest-labs/flux.2-pro",
        "a cat",
        {
            "aspect_ratio": "1:1",
            "extra_options": {"aspect_ratio": "21:9", "guidance": 3.5, "empty": ""},
        },
    )

    assert payload["aspect_ratio"] == "21:9"
    assert payload["guidance"] == 3.5
    assert "empty" not in payload
    assert "extra_options" not in payload


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
    """A 200 with no ``data`` entries is a malformed response and surfaces
    as a retryable ``ProviderError``."""

    from core.providers.errors import ProviderError

    respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(200, json={"created": 1, "data": []})
    )
    client = _openrouter_image_client("black-forest-labs/flux.2-pro")

    with pytest.raises(ProviderError, match="no data"):
        await client.generate("a cat", options={})


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


def _openai_image_client(model_id: str) -> ProviderImageClient:
    """Build a ProviderImageClient wired to a mockable OpenAI endpoint."""

    provider = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai_compatible",
        base_url="https://api.openai.com/v1",
        connections=[],
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="OPENAI_API_KEY",
        ),
    )
    return ProviderImageClient(
        provider=provider,
        connection=connection,
        credential="sk-test",
        model_id=model_id,
    )


# ---------------------------------------------------------------------------
# OpenAI payload builder — minimal schema, only the documented fields
# ---------------------------------------------------------------------------


def test_build_openai_payload_includes_model_and_prompt() -> None:
    """The OpenAI wire always carries ``model`` and ``prompt`` at the top level.
    No fields are invented when the user has not pinned a value."""

    payload = _build_openai_image_payload("gpt-image-1", "a cat", {})

    assert payload == {"model": "gpt-image-1", "prompt": "a cat"}


def test_build_openai_payload_includes_only_known_option_keys() -> None:
    """The OpenAI request only carries the documented ``/v1/images/generations``
    keys — OpenRouter-only fields like ``aspect_ratio`` and ``seed`` are not
    forwarded. ``style`` and ``response_format`` are valid OpenAI fields for
    dall-e models, so they are forwarded when present (the provider is
    responsible for rejecting an unsupported shape)."""

    payload = _build_openai_image_payload(
        "gpt-image-1",
        "a cat",
        {
            "size": "1024x1024",
            "quality": "auto",
            "background": "opaque",
            "moderation": "low",
            "n": 2,
            "output_format": "png",
            "output_compression": 80,
            # OpenRouter-only fields — must be dropped.
            "aspect_ratio": "1:1",
            "resolution": "1K",
            "seed": 42,
            "provider_options": {"recraft": {}},
        },
    )

    assert payload == {
        "model": "gpt-image-1",
        "prompt": "a cat",
        "size": "1024x1024",
        "quality": "auto",
        "background": "opaque",
        "moderation": "low",
        "n": 2,
        "output_format": "png",
        "output_compression": 80,
    }


def test_build_openai_payload_drops_empty_placeholder_values() -> None:
    """The OpenAI builder also drops empty placeholders so a cleared optional
    field is not forwarded to ``/v1/images/generations``."""

    payload = _build_openai_image_payload(
        "gpt-image-1",
        "a cat",
        {"size": "1024x1024", "style": "", "output_format": "", "n": 1},
    )

    assert payload == {
        "model": "gpt-image-1",
        "prompt": "a cat",
        "size": "1024x1024",
        "n": 1,
    }


def test_build_openai_payload_merges_extra_options_last() -> None:
    """The escape hatch also applies to the OpenAI native path."""

    payload = _build_openai_image_payload(
        "gpt-image-2",
        "a cat",
        {
            "size": "1024x1024",
            "extra_options": {"size": "2048x1152", "partial_images": 2},
        },
    )

    assert payload["size"] == "2048x1152"
    assert payload["partial_images"] == 2


def test_openai_image_keys_constant_matches_contract() -> None:
    """The OpenAI image key whitelist must match the documented
    ``/v1/images/generations`` parameters."""

    assert _OPENAI_IMAGE_KEYS == (
        "n",
        "size",
        "quality",
        "background",
        "moderation",
        "output_format",
        "output_compression",
        "style",
        "response_format",
    )


# ---------------------------------------------------------------------------
# OpenAI end-to-end wire — payload reaches /v1/images/generations correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_openai_image_generate_sends_request_and_decodes_b64_json() -> None:
    """A single-image OpenAI request returns one ``ImageGenerationResult``
    with decoded bytes and the correct media type. The HTTP body contains
    only model + prompt + the user-pinned options."""

    b64_payload = base64.b64encode(b"openai-png").decode("ascii")
    route = respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": b64_payload}]},
        )
    )
    client = _openai_image_client("gpt-image-1")

    result = await client.generate(
        "a cat",
        options={"size": "1024x1024", "quality": "auto"},
    )

    request = route.calls[0].request
    assert json.loads(request.content) == {
        "model": "gpt-image-1",
        "prompt": "a cat",
        "size": "1024x1024",
        "quality": "auto",
    }
    assert isinstance(result, ImageGenerationResult)
    assert result.images == (b"openai-png",)
    assert result.media_type == "image/png"
    assert result.model == "gpt-image-1"


@pytest.mark.asyncio
@respx.mock
async def test_openai_image_generate_maps_n_multiple_to_multiple_artifacts() -> None:
    """``n > 1`` is honored: the response ``data`` array is mapped one-to-one
    into the normalized ``images`` tuple. ``ImageService.generate_artifacts``
    then creates one artifact per image downstream."""

    payloads = [
        base64.b64encode(b"alpha").decode("ascii"),
        base64.b64encode(b"beta").decode("ascii"),
        base64.b64encode(b"gamma").decode("ascii"),
    ]
    route = respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": p} for p in payloads]},
        )
    )
    client = _openai_image_client("gpt-image-1")

    result = await client.generate("a cat", options={"n": 3, "size": "1024x1024"})

    assert json.loads(route.calls[0].request.content)["n"] == 3
    assert result.images == (b"alpha", b"beta", b"gamma")


@pytest.mark.asyncio
@respx.mock
async def test_openai_image_generate_records_output_format_in_media_type() -> None:
    """When the request pins ``output_format`` the response media type mirrors
    it (``image/jpeg``, ``image/webp``, …) so the artifact's file extension
    is correct."""

    b64_payload = base64.b64encode(b"jpegbytes").decode("ascii")
    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": b64_payload}]},
        )
    )
    client = _openai_image_client("gpt-image-1")

    result = await client.generate(
        "a cat",
        options={"size": "1024x1024", "output_format": "jpeg"},
    )

    assert result.images == (b"jpegbytes",)
    assert result.media_type == "image/jpeg"


@pytest.mark.asyncio
@respx.mock
async def test_openai_image_generate_rejects_empty_data_list() -> None:
    """A 200 with no ``data`` array is a malformed OpenAI image response and
    must surface as a ``ProviderError`` (retryable, since the same request
    could succeed against a healthy gateway)."""

    from core.providers.errors import ProviderError

    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(200, json={"created": 1, "data": []})
    )
    client = _openai_image_client("gpt-image-1")

    with pytest.raises(ProviderError, match="no data"):
        await client.generate("a cat", options={})


@pytest.mark.asyncio
@respx.mock
async def test_openai_image_generate_url_response_is_a_provider_error() -> None:
    """When OpenAI returns ``url`` entries (response_format=url) the wire
    layer raises a non-retryable error rather than silently dropping the
    image. The Settings schema defaults ``response_format`` to ``b64_json``;
    a user who explicitly chose ``url`` sees a clear actionable error."""

    from core.providers.errors import ProviderError

    respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200,
            json={"created": 1, "data": [{"url": "https://example.com/x.png"}]},
        )
    )
    client = _openai_image_client("dall-e-3")

    with pytest.raises(ProviderError, match="b64_json"):
        await client.generate("a cat", options={"response_format": "url", "size": "1024x1024"})


@pytest.mark.asyncio
async def test_provider_image_client_rejects_unknown_provider() -> None:
    """A provider other than OpenRouter / OpenAI surfaces an explicit
    ``ProviderError`` so the caller (ImageService) can map it to an
    ``ImageExecutionError``."""

    from core.providers.errors import ProviderError

    client = _openai_image_client("gpt-image-1")
    # Swap the provider id to one we do not support.
    client._provider = ProviderConfig(  # type: ignore[attr-defined]
        id="anthropic",
        name="Anthropic",
        adapter="openai_compatible",
        base_url="https://api.anthropic.com/v1",
        connections=[],
    )

    with pytest.raises(ProviderError, match="anthropic"):
        await client.generate("a cat", options={})
