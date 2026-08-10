"""Tests for provider-backed image HTTP clients and payload shaping."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.model_tasks.image_providers import (
    _OPENAI_CODEX_IMAGE_CARRIER_MODEL,
    _OPENAI_CODEX_IMAGE_TOOL_KEYS,
    _OPENAI_IMAGE_KEYS,
    _UNIFIED_IMAGE_KEYS,
    ProviderImageClient,
    _build_openai_codex_image_payload,
    _build_openai_image_payload,
    _build_openrouter_image_payload,
    _parse_openai_codex_image_response,
)
from core.model_tasks.image_types import ImageGenerationResult, ImageInput
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderOutcomeUnknownError,
)
from core.providers.openai import CODEX_EXTRA_HEADERS, CODEX_RESPONSES_MODE
from core.providers.openai_subscription_auth import OPENAI_AUTH_CLAIM
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"
OPENAI_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"


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


def _openai_subscription_access_token(account_id: str = "account-123") -> str:
    """Build a minimal unsigned JWT with the ChatGPT account claim."""

    header = _base64url_json({"alg": "none"})
    payload = _base64url_json({OPENAI_AUTH_CLAIM: {"chatgpt_account_id": account_id}})
    return f"{header}.{payload}."


def _base64url_json(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _openai_codex_image_sse(
    image_bytes: bytes = b"codex-image",
    *,
    output_format: str = "png",
) -> str:
    result = base64.b64encode(image_bytes).decode("ascii")
    return (
        _sse_event({"type": "response.created", "response": {"id": "resp-1"}})
        + _sse_event(
            {
                "type": "response.image_generation_call.partial_image",
                "partial_image_b64": base64.b64encode(b"preview").decode("ascii"),
            }
        )
        + _sse_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "image_generation_call",
                    "status": "completed",
                    "result": result,
                    "output_format": output_format,
                    "quality": "medium",
                    "size": "1536x1024",
                    "background": "opaque",
                    "revised_prompt": "a revised image prompt",
                },
            }
        )
        + _sse_event(
            {
                "type": "response.completed",
                "response": {
                    "tool_usage": {
                        "image_gen": {
                            "input_tokens": 12,
                            "output_tokens": 456,
                            "image_tokens": 450,
                            "total_tokens": 468,
                        }
                    },
                    "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                },
            }
        )
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


def _openai_subscription_image_client(
    model_id: str,
    *,
    credential: str | None = None,
) -> ProviderImageClient:
    """Build a ProviderImageClient wired to the OpenAI subscription endpoint."""

    provider = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai",
        base_url="https://api.openai.com/v1",
        connections=[],
    )
    connection = ConnectionConfig(
        id="subscription",
        type="oauth",
        label="ChatGPT Plus/Pro",
        auth=AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="",
        ),
        base_url="https://chatgpt.com/backend-api",
        mode=CODEX_RESPONSES_MODE,
    )
    return ProviderImageClient(
        provider=provider,
        connection=connection,
        credential=credential or _openai_subscription_access_token(),
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
# OpenAI subscription Codex Responses payload and parser
# ---------------------------------------------------------------------------


def test_build_openai_codex_payload_uses_carrier_and_image_tool() -> None:
    """The subscription image wire asks a Codex carrier to call the backend
    image_generation tool and keeps size/quality/background in the prompt text."""

    payload = _build_openai_codex_image_payload(
        "a cat",
        {
            "size": "1024x1536",
            "quality": "low",
            "background": "opaque",
            "moderation": "low",
            "output_format": "webp",
            "output_compression": 50,
        },
    )

    assert payload["model"] == _OPENAI_CODEX_IMAGE_CARRIER_MODEL
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["instructions"] == "You are an image generation assistant."
    assert payload["tools"] == [
        {
            "type": "image_generation",
            "output_format": "webp",
            "output_compression": 50,
            "moderation": "low",
            "background": "opaque",
            "size": "1024x1536",
            "quality": "low",
        }
    ]
    text = payload["input"][0]["content"][0]["text"]
    assert text == (
        "Use the image_generation tool to render "
        "(size 1024x1536, quality low, background opaque): a cat"
    )


def test_build_openai_codex_payload_drops_n_and_model_even_from_extra_options() -> None:
    """The Codex image tool rejects ``n`` and silently overrides ``model``, so
    the builder never forwards either field while still honoring the escape hatch."""

    payload = _build_openai_codex_image_payload(
        "a cat",
        {
            "n": 2,
            "model": "gpt-image-1",
            "output_format": "webp",
            "extra_options": {
                "n": 10,
                "model": "gpt-image-2",
                "output_format": "jpeg",
                "future_option": "kept",
            },
        },
    )

    tool = payload["tools"][0]
    assert tool == {
        "type": "image_generation",
        "output_format": "jpeg",
        "future_option": "kept",
    }


def test_build_openai_codex_payload_carries_source_images_for_editing() -> None:
    payload = _build_openai_codex_image_payload(
        "make the square blue while preserving its position",
        {},
        input_images=(
            ImageInput(filename="square.jpg", media_type="image/jpeg", data=b"source-one"),
            ImageInput(filename="style.png", media_type="image/png", data=b"source-two"),
        ),
    )

    content = payload["input"][0]["content"]
    assert content[0] == {
        "type": "input_text",
        "text": (
            "Use the image_generation tool to edit the provided image(s): "
            "make the square blue while preserving its position"
        ),
    }
    assert content[1:] == [
        {
            "type": "input_image",
            "image_url": (
                "data:image/jpeg;base64," + base64.b64encode(b"source-one").decode("ascii")
            ),
        },
        {
            "type": "input_image",
            "image_url": (
                "data:image/png;base64," + base64.b64encode(b"source-two").decode("ascii")
            ),
        },
    ]


def test_openai_codex_image_tool_key_constant_matches_verified_wire() -> None:
    assert _OPENAI_CODEX_IMAGE_TOOL_KEYS == (
        "output_format",
        "output_compression",
        "moderation",
        "background",
        "size",
        "quality",
    )


def test_parse_openai_codex_sse_extracts_final_image_and_usage() -> None:
    """The parser ignores progressive previews and reads the final image call
    plus image/tool usage from the completed Responses event."""

    result = _parse_openai_codex_image_response(
        _openai_codex_image_sse(b"webp-bytes", output_format="webp"),
        model="gpt-image-2",
        requested_output_format="webp",
    )

    assert result.images == (b"webp-bytes",)
    assert result.media_type == "image/webp"
    assert result.model == "gpt-image-2"
    assert result.usage == {
        "image_gen": {
            "input_tokens": 12,
            "output_tokens": 456,
            "image_tokens": 450,
            "total_tokens": 468,
        },
        "response": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
    }
    assert result.raw is not None
    call = result.raw["image_generation_calls"][0]
    assert call["revised_prompt"] == "a revised image prompt"
    assert call["size"] == "1536x1024"
    assert call["quality"] == "medium"


def test_parse_openai_codex_sse_without_final_image_is_retryable() -> None:
    with pytest.raises(ProviderError) as exc_info:
        _parse_openai_codex_image_response(
            _sse_event({"type": "response.completed", "response": {"status": "completed"}}),
            model="gpt-image-2",
        )

    assert exc_info.value.retryable is True


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
async def test_openai_image_edit_posts_multipart_source_images() -> None:
    b64_payload = base64.b64encode(b"edited-png").decode("ascii")
    route = respx.post("https://api.openai.com/v1/images/edits").mock(
        return_value=httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": b64_payload}]},
        )
    )
    client = _openai_image_client("gpt-image-1")

    result = await client.generate(
        "make it rainy",
        options={"size": "1024x1024", "quality": "high"},
        input_images=(
            ImageInput(filename="first.png", media_type="image/png", data=b"first-source"),
            ImageInput(filename="second.webp", media_type="image/webp", data=b"second-source"),
        ),
    )

    request = route.calls[0].request
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="model"' in body and b"gpt-image-1" in body
    assert b'name="prompt"' in body and b"make it rainy" in body
    assert b'name="image[]"; filename="first.png"' in body
    assert b'name="image[]"; filename="second.webp"' in body
    assert b"first-source" in body and b"second-source" in body
    assert result.images == (b"edited-png",)


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

    with pytest.raises(ProviderError):
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
@respx.mock
async def test_openai_subscription_image_generate_posts_codex_responses() -> None:
    """The subscription connection uses ``/codex/responses`` with Codex headers
    and decodes the final image_generation_call SSE item."""

    route = respx.post(OPENAI_CODEX_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            text=_openai_codex_image_sse(b"codex-webp", output_format="webp"),
            headers={"content-type": "text/event-stream"},
        )
    )
    client = _openai_subscription_image_client("gpt-image-2")

    result = await client.generate(
        "a cat",
        options={"size": "1024x1536", "quality": "low", "output_format": "webp", "n": 3},
    )

    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {_openai_subscription_access_token()}"
    assert request.headers["chatgpt-account-id"] == "account-123"
    for header, value in CODEX_EXTRA_HEADERS.items():
        assert request.headers[header] == value

    payload = json.loads(request.content)
    assert payload["model"] == _OPENAI_CODEX_IMAGE_CARRIER_MODEL
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["tools"][0] == {
        "type": "image_generation",
        "output_format": "webp",
        "size": "1024x1536",
        "quality": "low",
    }
    assert "n" not in payload["tools"][0]
    assert "size 1024x1536" in payload["input"][0]["content"][0]["text"]
    assert result.images == (b"codex-webp",)
    assert result.media_type == "image/webp"


@pytest.mark.asyncio
async def test_openai_subscription_image_generate_requires_account_header() -> None:
    client = _openai_subscription_image_client("gpt-image-2", credential="not-a-jwt")

    with pytest.raises(ProviderAuthError, match="reconnect"):
        await client.generate("a cat", options={})


@pytest.mark.asyncio
@respx.mock
async def test_openai_subscription_image_edit_posts_source_image() -> None:
    route = respx.post(OPENAI_CODEX_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            text=_openai_codex_image_sse(b"edited-image"),
            headers={"content-type": "text/event-stream"},
        )
    )
    client = _openai_subscription_image_client("gpt-image-2")

    result = await client.generate(
        "make it rainy",
        options={},
        input_images=(ImageInput(filename="photo.png", media_type="image/png", data=b"source"),),
    )

    payload = json.loads(route.calls[0].request.content)
    content = payload["input"][0]["content"]
    assert "edit the provided image" in content[0]["text"]
    assert content[1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(b"source").decode("ascii"),
    }
    assert result.images == (b"edited-image",)


@pytest.mark.asyncio
@respx.mock
async def test_openai_subscription_image_does_not_retry_missing_final_image() -> None:
    route = respx.post(OPENAI_CODEX_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            text=_sse_event({"type": "response.completed", "response": {"status": "completed"}}),
            headers={"content-type": "text/event-stream"},
        )
    )
    client = _openai_subscription_image_client("gpt-image-2")

    with pytest.raises(ProviderOutcomeUnknownError) as exc_info:
        await client.generate("a cat", options={})

    assert exc_info.value.retryable is False
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_provider_image_client_rejects_unknown_provider() -> None:
    """A provider using an unsupported task adapter surfaces an explicit
    ``ProviderError`` so the caller (ImageService) can map it to an
    ``ImageExecutionError``."""

    from core.providers.errors import ProviderError

    client = _openai_image_client("gpt-image-1")
    # Swap the Provider to an adapter whose image wire is not supported.
    client._provider = ProviderConfig(  # type: ignore[attr-defined]
        id="anthropic",
        name="Anthropic",
        adapter="anthropic",
        base_url="https://api.anthropic.com/v1",
        connections=[],
    )

    with pytest.raises(ProviderError, match="anthropic"):
        await client.generate("a cat", options={})


@pytest.mark.asyncio
@respx.mock
async def test_custom_openai_compatible_provider_uses_standard_image_wire() -> None:
    route = respx.post("http://127.0.0.1:8080/v1/images/generations").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(b"custom-image").decode("ascii")}]},
        )
    )
    provider = ProviderConfig(
        id="local-ai",
        name="Local AI",
        adapter="openai_compatible",
        base_url="http://127.0.0.1:8080/v1",
        connections=[],
        custom=True,
    )
    connection = ConnectionConfig(
        id="default",
        type="none",
        label="Default",
        auth=AuthConfig(header="", prefix=""),
    )
    client = ProviderImageClient(
        provider=provider,
        connection=connection,
        credential="",
        model_id="image-model",
    )

    result = await client.generate("a cat", options={})

    assert route.called
    assert result.images == (b"custom-image",)
