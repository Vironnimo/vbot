"""Model tasks: the OpenRouter and OpenAI image wires of ``ProviderImageClient``.

Every case goes through the public ``generate`` against a mocked endpoint:
the request the Provider receives, the images the caller gets back, and how
unusable responses fail. Generation may be billed, so only a documented
"not processed" status is retried.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx

from core.model_tasks.image_providers import ProviderImageClient
from core.model_tasks.image_types import ImageInput
from core.providers.errors import ProviderError, ProviderOutcomeUnknownError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from tests.core.model_tasks.image_providers_test_support import (
    OPENROUTER_IMAGES_URL,
    _openai_image_client,
    _openrouter_image_client,
    _unified_image_response,
)

OPENAI_GENERATIONS_URL = "https://api.openai.com/v1/images/generations"
OPENAI_EDITS_URL = "https://api.openai.com/v1/images/edits"
_OPENROUTER_MODEL = "black-forest-labs/flux.2-pro"
_OPENAI_MODEL = "gpt-image-1"


def _client(wire: str) -> ProviderImageClient:
    if wire == "openrouter":
        return _openrouter_image_client(_OPENROUTER_MODEL)
    return _openai_image_client(_OPENAI_MODEL)


def _url(wire: str) -> str:
    return OPENROUTER_IMAGES_URL if wire == "openrouter" else OPENAI_GENERATIONS_URL


def _model(wire: str) -> str:
    return _OPENROUTER_MODEL if wire == "openrouter" else _OPENAI_MODEL


_UNIFIED_OPTIONS = {
    "aspect_ratio": "16:9",
    "resolution": "2K",
    "size": "1K",
    "quality": "high",
    "output_format": "webp",
    "background": "transparent",
    "output_compression": 80,
    "n": 2,
    "seed": 12345,
}
_OPENAI_OPTIONS = {
    "n": 2,
    "size": "1024x1024",
    "quality": "auto",
    "background": "opaque",
    "moderation": "low",
    "output_format": "png",
    "output_compression": 80,
    "style": "vivid",
    "response_format": "b64_json",
}


@pytest.mark.parametrize(
    ("wire", "options", "expected_fields"),
    [
        # The Provider's own defaults apply to every option left unset.
        pytest.param("openrouter", {}, {}, id="openrouter-no-options"),
        pytest.param("openrouter", _UNIFIED_OPTIONS, _UNIFIED_OPTIONS, id="openrouter-every-key"),
        pytest.param(
            "openrouter",
            {"aspect_ratio": "1:1", "image_size": "1K", "strength": 0.5, "future": "ignored"},
            {"aspect_ratio": "1:1"},
            id="openrouter-unknown-and-legacy-keys-dropped",
        ),
        pytest.param(
            "openrouter",
            {
                "aspect_ratio": "",
                "resolution": "2K",
                "output_compression": 0,
                "seed": None,
                "provider_options": {},
                "extra_options": {},
            },
            {"resolution": "2K", "output_compression": 0},
            id="openrouter-empty-placeholders-dropped",
        ),
        pytest.param(
            "openrouter",
            {"n": 2, "provider_options": {"recraft": {"style": "vector_illustration"}}},
            {"n": 2, "provider": {"options": {"recraft": {"style": "vector_illustration"}}}},
            id="openrouter-provider-options-nested",
        ),
        pytest.param(
            "openrouter",
            {"aspect_ratio": "1:1", "extra_options": {"guidance": 3.5, "empty": ""}},
            {"aspect_ratio": "1:1", "guidance": 3.5},
            id="openrouter-extra-options-merged",
        ),
        pytest.param("openai", {}, {}, id="openai-no-options"),
        # OpenRouter-only fields never reach the OpenAI endpoint.
        pytest.param(
            "openai",
            {
                **_OPENAI_OPTIONS,
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "seed": 42,
                "provider_options": {"recraft": {}},
            },
            _OPENAI_OPTIONS,
            id="openai-every-key-and-no-openrouter-key",
        ),
        pytest.param(
            "openai",
            {"size": "1024x1024", "style": "", "output_format": "", "n": 1},
            {"size": "1024x1024", "n": 1},
            id="openai-empty-placeholders-dropped",
        ),
        pytest.param(
            "openai",
            {"size": "1024x1024", "extra_options": {"partial_images": 2}},
            {"size": "1024x1024", "partial_images": 2},
            id="openai-extra-options-merged",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_request_carries_only_the_documented_options_of_the_wire(
    wire: str, options: dict[str, Any], expected_fields: dict[str, Any]
) -> None:
    route = respx.post(_url(wire)).mock(
        return_value=httpx.Response(200, json=_unified_image_response(b"img"))
    )

    await _client(wire).generate("a cat", options=options)

    request = route.calls.last.request
    assert json.loads(request.content) == {
        "model": _model(wire),
        "prompt": "a cat",
        **expected_fields,
    }
    assert request.headers["Authorization"] == "Bearer sk-test"
    if wire == "openrouter":
        assert request.headers["X-Title"] == "vBot"


@pytest.mark.parametrize("wire", ["openrouter", "openai"])
@respx.mock
@pytest.mark.asyncio
async def test_extra_options_cannot_redirect_the_model(wire: str) -> None:
    route = respx.post(_url(wire)).mock(
        return_value=httpx.Response(200, json=_unified_image_response(b"img"))
    )

    with pytest.raises(ProviderError, match="model"):
        await _client(wire).generate(
            "a cat", options={"extra_options": {"model": "redirected-image-model"}}
        )

    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_sends_source_images_as_data_url_references() -> None:
    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        return_value=httpx.Response(200, json=_unified_image_response(b"edited"))
    )

    result = await _openrouter_image_client("openai/gpt-image-1").generate(
        "make it rainy",
        options={},
        input_images=(ImageInput(filename="photo.jpg", media_type="image/jpeg", data=b"src"),),
    )

    assert json.loads(route.calls.last.request.content)["input_references"] == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b"src").decode()},
        }
    ]
    assert result.images == (b"edited",)


@pytest.mark.parametrize(
    ("input_images", "file_field"),
    [
        pytest.param(
            (ImageInput(filename="first.png", media_type="image/png", data=b"first-source"),),
            b'name="image"; filename="first.png"',
            id="one-image",
        ),
        pytest.param(
            (
                ImageInput(filename="first.png", media_type="image/png", data=b"first-source"),
                ImageInput(filename="second.webp", media_type="image/webp", data=b"second-src"),
            ),
            b'name="image[]"; filename="second.webp"',
            id="several-images",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_openai_edits_source_images_as_multipart(
    input_images: tuple[ImageInput, ...], file_field: bytes
) -> None:
    route = respx.post(OPENAI_EDITS_URL).mock(
        return_value=httpx.Response(200, json=_unified_image_response(b"edited-png"))
    )

    result = await _openai_image_client(_OPENAI_MODEL).generate(
        "make it rainy",
        options={"quality": "high", "extra_options": {"output_format": "jpeg"}},
        input_images=input_images,
    )

    request = route.calls.last.request
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content
    for field, value in (
        (b"model", b"gpt-image-1"),
        (b"prompt", b"make it rainy"),
        (b"quality", b"high"),
        (b"output_format", b"jpeg"),
    ):
        assert b'name="' + field + b'"\r\n\r\n' + value in body
    assert file_field in body
    assert all(image.data in body for image in input_images)
    assert (result.images, result.media_type) == ((b"edited-png",), "image/jpeg")


@pytest.mark.parametrize(
    ("wire", "options", "body", "images", "media_type", "usage"),
    [
        pytest.param(
            "openrouter",
            {},
            _unified_image_response(b"hello", usage={"cost": 0.04}),
            (b"hello",),
            "image/png",
            {"cost": 0.04},
            id="openrouter-default-png-with-usage",
        ),
        pytest.param(
            "openrouter",
            {"n": 3},
            _unified_image_response(b"alpha", b"beta", b"gamma"),
            (b"alpha", b"beta", b"gamma"),
            "image/png",
            None,
            id="openrouter-several-images",
        ),
        # A vector entry names its own type; raster entries follow the request.
        pytest.param(
            "openrouter",
            {"output_format": "png"},
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(b"<svg/>").decode(),
                        "media_type": "image/svg+xml",
                    }
                ]
            },
            (b"<svg/>",),
            "image/svg+xml",
            None,
            id="openrouter-entry-media-type-wins",
        ),
        pytest.param(
            "openrouter",
            {"extra_options": {"output_format": "svg"}},
            _unified_image_response(b"<svg/>"),
            (b"<svg/>",),
            "image/svg+xml",
            None,
            id="openrouter-requested-format",
        ),
        pytest.param(
            "openai",
            {"n": 3},
            _unified_image_response(b"alpha", b"beta", b"gamma"),
            (b"alpha", b"beta", b"gamma"),
            "image/png",
            None,
            id="openai-several-images",
        ),
        pytest.param(
            "openai",
            {"output_format": "jpeg"},
            _unified_image_response(b"jpeg-bytes"),
            (b"jpeg-bytes",),
            "image/jpeg",
            None,
            id="openai-requested-format",
        ),
        pytest.param(
            "openai",
            {"extra_options": {"output_format": "webp"}},
            _unified_image_response(b"webp-bytes"),
            (b"webp-bytes",),
            "image/webp",
            None,
            id="openai-extra-options-format",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_generate_decodes_every_image_with_its_media_type(
    wire: str,
    options: dict[str, Any],
    body: dict[str, Any],
    images: tuple[bytes, ...],
    media_type: str,
    usage: dict[str, Any] | None,
) -> None:
    respx.post(_url(wire)).mock(return_value=httpx.Response(200, json=body))

    result = await _client(wire).generate("a cat", options=options)

    assert (result.images, result.media_type, result.model) == (images, media_type, _model(wire))
    assert result.usage == usage


@pytest.mark.parametrize(
    ("wire", "response", "message"),
    [
        pytest.param(
            "openrouter",
            httpx.Response(200, json={"created": 1, "data": []}),
            "no data",
            id="openrouter-empty-data",
        ),
        pytest.param(
            "openrouter",
            httpx.Response(200, json={"data": [{"b64_json": "not-base64!"}]}),
            "could not be decoded",
            id="openrouter-undecodable-image",
        ),
        pytest.param(
            "openrouter",
            httpx.Response(502, text="invalid upstream response"),
            "HTTP 502",
            id="openrouter-ambiguous-502",
        ),
        pytest.param(
            "openai",
            httpx.Response(200, json={"created": 1, "data": []}),
            "no data",
            id="openai-empty-data",
        ),
        pytest.param(
            "openai",
            httpx.Response(200, json={"data": [{"b64_json": "not-base64!"}]}),
            "could not be decoded",
            id="openai-undecodable-image",
        ),
        # Only inline bytes are decoded; the user must switch the format.
        pytest.param(
            "openai",
            httpx.Response(200, json={"data": [{"url": "https://example.com/x.png"}]}),
            "b64_json",
            id="openai-url-response",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_unusable_image_response_is_never_replayed(
    wire: str, response: httpx.Response, message: str
) -> None:
    route = respx.post(_url(wire)).mock(return_value=response)

    with pytest.raises(ProviderOutcomeUnknownError, match=message) as raised:
        await _client(wire).generate("a cat", options={})

    assert raised.value.retryable is False
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_retries_the_documented_not_processed_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    route = respx.post(OPENROUTER_IMAGES_URL).mock(
        side_effect=[
            httpx.Response(503, text="no provider available"),
            httpx.Response(200, json=_unified_image_response(b"img")),
        ]
    )

    result = await _client("openrouter").generate("a cat", options={})

    assert result.images == (b"img",)
    assert route.call_count == 2


def _connection(auth: AuthConfig) -> ConnectionConfig:
    return ConnectionConfig(id="default", type="none", label="Default", auth=auth)


@respx.mock
@pytest.mark.asyncio
async def test_custom_openai_compatible_provider_uses_the_standard_wire() -> None:
    route = respx.post("http://127.0.0.1:8080/v1/images/generations").mock(
        return_value=httpx.Response(200, json=_unified_image_response(b"custom-image"))
    )
    provider = ProviderConfig(
        id="local-ai",
        name="Local AI",
        adapter="openai_compatible",
        base_url="http://127.0.0.1:8080/v1",
        connections=[],
        custom=True,
    )
    client = ProviderImageClient(
        provider=provider,
        connection=_connection(AuthConfig(header="", prefix="")),
        credential="",
        model_id="image-model",
    )

    result = await client.generate("a cat", options={})

    assert route.call_count == 1
    assert result.images == (b"custom-image",)


@pytest.mark.asyncio
async def test_provider_without_an_image_wire_is_rejected() -> None:
    provider = ProviderConfig(
        id="anthropic",
        name="Anthropic",
        adapter="anthropic",
        base_url="https://api.anthropic.com/v1",
        connections=[],
    )
    client = ProviderImageClient(
        provider=provider,
        connection=_connection(AuthConfig(header="x-api-key", prefix="")),
        credential="sk-test",
        model_id="claude",
    )

    with pytest.raises(ProviderError, match="anthropic") as raised:
        await client.generate("a cat", options={})

    assert raised.value.retryable is False
